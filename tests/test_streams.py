"""Offline tests for pagination and incremental logic, with a mocked Apollo API."""

from __future__ import annotations

import contextlib
import io
import json
from typing import cast

import pytest
import responses
from singer_sdk.exceptions import FatalAPIError

from tap_apollo.client import MAX_PAGES, REQUEST_TIMEOUT, ApolloStream
from tap_apollo.tap import TapApollo

BASE = "https://api.apollo.io/api/v1"


def make_tap(state: dict | None = None, **config: str) -> TapApollo:
    return TapApollo(config={"api_key": "test-key", **config}, state=state)


def get_records(tap: TapApollo, stream_name: str) -> list[dict]:
    """Sync one stream the way Meltano would.

    Returns:
        The records the stream emitted.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        tap.streams[stream_name].sync()
    messages = [json.loads(line) for line in buffer.getvalue().splitlines()]
    return [m["record"] for m in messages if m["type"] == "RECORD"]


def page(key: str, records: list[dict], page_num: int, total_pages: int) -> dict:
    return {
        key: records,
        "pagination": {
            "page": page_num,
            "per_page": 100,
            "total_entries": total_pages * 100,
            "total_pages": total_pages,
        },
    }


@responses.activate
def test_get_stream_paginates_with_query_params() -> None:
    for n in (1, 2):
        responses.get(
            f"{BASE}/opportunities/search",
            match=[responses.matchers.query_param_matcher({"page": str(n), "per_page": "100"})],
            json=page("opportunities", [{"id": f"o{n}"}], n, 2),
        )

    records = get_records(make_tap(), "opportunities")

    assert [r["id"] for r in records] == ["o1", "o2"]
    assert all(c.request.headers["x-api-key"] == "test-key" for c in responses.calls)


@responses.activate
def test_post_stream_paginates_in_body() -> None:
    for n in (1, 2, 3):
        responses.post(
            f"{BASE}/accounts/search",
            match=[
                responses.matchers.json_params_matcher(
                    {
                        "sort_by_field": "account_created_at",
                        "sort_ascending": True,
                        "page": n,
                        "per_page": 100,
                    },
                ),
            ],
            json=page("accounts", [{"id": f"a{n}"}], n, 3),
        )

    records = get_records(make_tap(), "accounts")

    assert [r["id"] for r in records] == ["a1", "a2", "a3"]


@responses.activate
def test_stops_on_empty_page_without_pagination_object() -> None:
    responses.get(
        f"{BASE}/emailer_messages/search",
        json={"emailer_messages": [{"id": f"m{i}"} for i in range(100)]},
    )
    responses.get(f"{BASE}/emailer_messages/search", json={"emailer_messages": []})

    records = get_records(make_tap(), "emailer_messages")

    assert len(records) == 100
    assert len(responses.calls) == 2


@responses.activate
def test_stops_at_apollo_page_cap(caplog: pytest.LogCaptureFixture) -> None:
    responses.get(
        f"{BASE}/users/search",
        json=page("users", [{"id": "u"}], 1, MAX_PAGES + 10),
    )

    records = get_records(make_tap(), "users")

    assert len(records) == MAX_PAGES
    assert "reached Apollo's limit" in caplog.text


@responses.activate
def test_unpaginated_stream_makes_one_request() -> None:
    responses.get(f"{BASE}/labels", json=[{"id": "l1"}, {"id": "l2"}])

    records = get_records(make_tap(), "lists")

    assert [r["id"] for r in records] == ["l1", "l2"]
    assert len(responses.calls) == 1


@responses.activate
def test_contacts_stop_at_bookmark() -> None:
    newer = [{"id": "c1", "updated_at": "2026-03-02T00:00:00.000Z"}]
    boundary = [
        {"id": "c2", "updated_at": "2026-03-01T12:00:00.000Z"},
        {"id": "c3", "updated_at": "2026-02-01T00:00:00.000Z"},
    ]
    responses.post(f"{BASE}/contacts/search", json=page("contacts", newer, 1, 5))
    responses.post(f"{BASE}/contacts/search", json=page("contacts", boundary, 2, 5))
    state = {
        "bookmarks": {
            "contacts": {
                "replication_key": "updated_at",
                "replication_key_value": "2026-03-01T00:00:00+00:00",
            },
        },
    }

    records = get_records(make_tap(state=state), "contacts")

    assert [r["id"] for r in records] == ["c1", "c2"]
    assert len(responses.calls) == 2  # pages 3-5 are never requested
    body = json.loads(cast("bytes", responses.calls[0].request.body))
    assert body["sort_by_field"] == "contact_updated_at"
    assert body["sort_ascending"] is False


@responses.activate
def test_retries_rate_limited_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ApolloStream, "_retry_after", staticmethod(lambda _: 0.0))
    responses.get(f"{BASE}/contact_stages", status=429, headers={"Retry-After": "1"})
    responses.get(f"{BASE}/contact_stages", json={"contact_stages": [{"id": "s1"}]})

    records = get_records(make_tap(), "contact_stages")

    assert [r["id"] for r in records] == ["s1"]


@responses.activate
def test_retries_apollo_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ApolloStream, "_retry_after", staticmethod(lambda _: 0.0))
    timeout = {"error": "Something's wrong with your API request. request timed out"}
    responses.post(f"{BASE}/accounts/search", status=422, json=timeout)
    responses.post(f"{BASE}/accounts/search", json=page("accounts", [{"id": "a1"}], 1, 1))

    records = get_records(make_tap(), "accounts")

    assert [r["id"] for r in records] == ["a1"]


@responses.activate
def test_other_422s_are_not_retried() -> None:
    responses.post(f"{BASE}/accounts/search", status=422, json={"error": "invalid sort field"})

    with pytest.raises(FatalAPIError):
        get_records(make_tap(), "accounts")


def test_request_timeout_is_short() -> None:
    stream = cast("ApolloStream", make_tap().streams["accounts"])
    assert stream.timeout == REQUEST_TIMEOUT


@responses.activate
def test_free_form_list_items_are_kept() -> None:
    contact = {
        "id": "c1",
        "updated_at": "2026-03-02T00:00:00.000Z",
        "contact_emails": [{"email": "a@example.com", "position": 0}],
    }
    responses.post(f"{BASE}/contacts/search", json=page("contacts", [contact], 1, 1))

    records = get_records(make_tap(), "contacts")

    assert records[0]["contact_emails"] == contact["contact_emails"]


@responses.activate
def test_mixed_type_fields_are_emitted_as_strings() -> None:
    contact = {
        "id": "c1",
        "updated_at": "2026-03-02T00:00:00.000Z",
        "email_source": "crm_csv",
        "email_unsubscribed": True,
        "contact_job_change_event": {"title": "CTO"},
        "merged_crm_ids": ["a", "b"],
        "hubspot_vid": 1.5,
    }
    responses.post(f"{BASE}/contacts/search", json=page("contacts", [contact], 1, 1))

    record = get_records(make_tap(), "contacts")[0]

    assert record["email_source"] == "crm_csv"
    assert record["email_unsubscribed"] == "true"
    assert json.loads(record["contact_job_change_event"]) == {"title": "CTO"}
    assert record["merged_crm_ids"] == '["a", "b"]'
    assert record["hubspot_vid"] == "1.5"


@responses.activate
def test_custom_field_values_are_kept() -> None:
    contact = {
        "id": "c1",
        "updated_at": "2026-03-02T00:00:00.000Z",
        "typed_custom_fields": {"0123456789abcdef01234567": "Gold"},
        "account": {"id": "a1", "typed_custom_fields": {"0123456789abcdef01234568": 5}},
    }
    responses.post(f"{BASE}/contacts/search", json=page("contacts", [contact], 1, 1))

    record = get_records(make_tap(), "contacts")[0]

    assert record["typed_custom_fields"] == contact["typed_custom_fields"]
    assert record["account"]["typed_custom_fields"] == contact["account"]["typed_custom_fields"]
