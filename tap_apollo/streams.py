"""Stream type classes for tap-apollo."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tap_apollo.client import ApolloStream

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from singer_sdk.helpers.types import Context


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Records you manage in Apollo


class ContactsStream(ApolloStream):
    """Contacts in your team's Apollo account.

    Apollo can only sort (not filter) contacts by update time, so results are
    requested newest first and pagination stops once records are older than the
    bookmark.
    """

    name = "contacts"
    path = "/contacts/search"
    http_method = "POST"
    records_jsonpath = "$.contacts[*]"
    paginate_in_body = True
    replication_key = "updated_at"
    is_sorted = False  # descending, so the bookmark is only final at the end

    @override
    def prepare_request_payload(
        self,
        context: Context | None,
        next_page_token: Any | None,
    ) -> dict:
        """Sort by most recently updated first.

        Returns:
            The JSON request body.
        """
        return {"sort_by_field": "contact_updated_at", "sort_ascending": False}

    def _is_stale(self, record: dict) -> bool:
        start = self.get_starting_timestamp(None)
        updated_at = _parse_timestamp(record.get(self.replication_key))
        return bool(start and updated_at and updated_at < start)

    @override
    def stop_paginating(self, records: list[dict]) -> bool:
        """Stop once a page ends with records older than the bookmark.

        Returns:
            True if the last record on the page predates the bookmark.
        """
        return self._is_stale(records[-1])

    @override
    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Skip records older than the bookmark on the last fetched page.

        Returns:
            The record, or ``None`` if it predates the bookmark.
        """
        return None if self._is_stale(row) else row


class AccountsStream(ApolloStream):
    """Accounts (companies) in your team's Apollo account."""

    name = "accounts"
    path = "/accounts/search"
    http_method = "POST"
    records_jsonpath = "$.accounts[*]"
    paginate_in_body = True

    @override
    def prepare_request_payload(
        self,
        context: Context | None,
        next_page_token: Any | None,
    ) -> dict:
        """Sort by creation time so pages are stable while paginating.

        Returns:
            The JSON request body.
        """
        return {"sort_by_field": "account_created_at", "sort_ascending": True}


class OpportunitiesStream(ApolloStream):
    """Deals (opportunities)."""

    name = "opportunities"
    path = "/opportunities/search"
    records_jsonpath = "$.opportunities[*]"


class TasksStream(ApolloStream):
    """Tasks assigned to users."""

    name = "tasks"
    path = "/tasks/search"
    http_method = "POST"
    records_jsonpath = "$.tasks[*]"


class PhoneCallsStream(ApolloStream):
    """Phone call records."""

    name = "phone_calls"
    path = "/phone_calls/search"
    records_jsonpath = "$.phone_calls[*]"


# Sequences and outreach


class SequencesStream(ApolloStream):
    """Sequences (emailer campaigns)."""

    name = "sequences"
    path = "/emailer_campaigns/search"
    http_method = "POST"
    records_jsonpath = "$.emailer_campaigns[*]"


class EmailerMessagesStream(ApolloStream):
    """Outreach emails created and sent by sequences."""

    name = "emailer_messages"
    path = "/emailer_messages/search"
    records_jsonpath = "$.emailer_messages[*]"


# Workspace configuration and reference data


class UsersStream(ApolloStream):
    """Users in your Apollo team."""

    name = "users"
    path = "/users/search"
    records_jsonpath = "$.users[*]"


class EmailAccountsStream(ApolloStream):
    """Email inboxes linked to Apollo."""

    name = "email_accounts"
    path = "/email_accounts"
    records_jsonpath = "$.email_accounts[*]"
    paginated = False


class ListsStream(ApolloStream):
    """Lists (labels) of contacts, accounts and sequences."""

    name = "lists"
    path = "/labels"
    records_jsonpath = "$[*]"
    paginated = False


class CustomFieldsStream(ApolloStream):
    """Custom field definitions."""

    name = "custom_fields"
    path = "/typed_custom_fields"
    records_jsonpath = "$.typed_custom_fields[*]"
    paginated = False


class ContactStagesStream(ApolloStream):
    """Contact stages."""

    name = "contact_stages"
    path = "/contact_stages"
    records_jsonpath = "$.contact_stages[*]"
    paginated = False


class AccountStagesStream(ApolloStream):
    """Account stages."""

    name = "account_stages"
    path = "/account_stages"
    records_jsonpath = "$.account_stages[*]"
    paginated = False


class OpportunityStagesStream(ApolloStream):
    """Deal (opportunity) stages."""

    name = "opportunity_stages"
    path = "/opportunity_stages"
    records_jsonpath = "$.opportunity_stages[*]"
    paginated = False
