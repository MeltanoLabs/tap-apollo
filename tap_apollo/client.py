"""REST client handling, including ApolloStream base class."""

from __future__ import annotations

import json
import sys
from functools import cached_property
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from singer_sdk import SchemaDirectory, StreamSchema
from singer_sdk.authenticators import APIKeyAuthenticator
from singer_sdk.exceptions import RetriableAPIError
from singer_sdk.pagination import PageNumberPaginator, SinglePagePaginator
from singer_sdk.streams import RESTStream

from tap_apollo import schemas

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Generator

    import requests
    from singer_sdk.helpers.types import Context
    from singer_sdk.pagination import BaseAPIPaginator
    from singer_sdk.streams.rest import HTTPRequest, PageContext

SCHEMAS_DIR = SchemaDirectory(schemas)

# Apollo search endpoints only return the first 50,000 matches of a query:
# 100 records per page, up to 500 pages.
MAX_PER_PAGE = 100
MAX_PAGES = 500

# Seconds to wait for a response before retrying. Apollo normally answers in about a second,
# but occasionally a request hangs.
REQUEST_TIMEOUT = 60


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


class ApolloPaginator(PageNumberPaginator):
    """Page-number paginator for Apollo search endpoints."""

    def __init__(self, stream: ApolloStream) -> None:
        """Create a paginator that starts at page 1.

        Args:
            stream: The stream being paginated, used to parse records and log.
        """
        super().__init__(start_value=1)
        self.stream = stream

    @override
    def has_more(self, response: requests.Response) -> bool:
        """Return whether another page should be requested.

        Args:
            response: The API response object.

        Returns:
            True if there are more pages to fetch.
        """
        records = list(self.stream.parse_response(response))
        if not records or self.stream.stop_paginating(records):
            return False

        pagination = response.json().get("pagination") or {}
        total_pages = int(pagination.get("total_pages") or 0)
        if pagination and self.current_value >= total_pages:
            return False
        if not pagination and len(records) < MAX_PER_PAGE:
            return False

        if self.current_value >= MAX_PAGES:
            self.stream.logger.warning(
                "Stream '%s' reached Apollo's limit of %d pages (%d records). "
                "%s matching records were not extracted.",
                self.stream.name,
                MAX_PAGES,
                MAX_PAGES * MAX_PER_PAGE,
                pagination.get("total_entries", "Further"),
            )
            return False
        return True


class ApolloStream(RESTStream):
    """Apollo stream class."""

    url_base = "https://api.apollo.io/api/v1"
    primary_keys = ("id",)
    schema = StreamSchema(SCHEMAS_DIR)

    #: Whether the endpoint is paginated with `page` and `per_page`.
    paginated = True

    #: Whether pagination parameters go in the JSON body rather than the query string.
    paginate_in_body = False

    extra_retry_statuses = (HTTPStatus.TOO_MANY_REQUESTS,)

    @override
    def validate_response(self, response: requests.Response) -> None:
        """Retry Apollo's server-side timeouts, which it reports as 422s.

        Args:
            response: A :class:`requests.Response` object.

        Raises:
            RetriableAPIError: If Apollo timed out handling the request.
        """
        if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY and "timed out" in response.text:
            raise RetriableAPIError(self.response_error_message(response), response)
        super().validate_response(response)

    @override
    @property
    def authenticator(self) -> APIKeyAuthenticator:
        """An authenticator object."""
        return APIKeyAuthenticator(
            key="x-api-key",
            value=self.config["api_key"],
            location="header",
        )

    @override
    @property
    def http_headers(self) -> dict:
        """A dictionary of HTTP headers."""
        return {"Cache-Control": "no-cache", "Content-Type": "application/json"}

    @override
    @property
    def timeout(self) -> int:
        """Apollo answers in about a second, so give up on a hung request after a minute.

        Returns:
            The request timeout in seconds.
        """
        return REQUEST_TIMEOUT

    @override
    def get_new_paginator(self) -> BaseAPIPaginator:
        """Create a new pagination helper instance.

        Returns:
            A page-number paginator, or a single-page one for unpaginated endpoints.
        """
        if self.paginated:
            return ApolloPaginator(self)
        return SinglePagePaginator()

    @override
    def get_http_request(self, *, page: PageContext[Any]) -> HTTPRequest:
        """Return a request object for this stream.

        Args:
            page: An object containing the stream partition or context dictionary,
                and the next page token if applicable.

        Returns:
            An HTTP request for this stream.
        """
        request = super().get_http_request(page=page)
        if self.paginated:
            paging = {"page": page.next_page_token, "per_page": MAX_PER_PAGE}
            if self.paginate_in_body:
                body = cast("dict[str, Any]", request.data or {})
                request.data = {**body, **paging}
            else:
                request.params.update(paging)
        return request

    def _fields_of_type(self, *types: str) -> frozenset[str]:
        """Top-level fields declared as one of the given types (and null).

        Returns:
            The field names.
        """
        fields = set()
        for name, prop in self.schema["properties"].items():
            declared = prop.get("type", [])
            kinds = set(declared if isinstance(declared, list) else [declared]) - {"null"}
            if kinds and kinds <= set(types):
                fields.add(name)
        return frozenset(fields)

    @cached_property
    def _string_fields(self) -> frozenset[str]:
        """Top-level fields declared as plain strings."""
        return self._fields_of_type("string")

    @cached_property
    def _number_fields(self) -> frozenset[str]:
        """Top-level fields declared as numbers or integers."""
        return self._fields_of_type("number", "integer")

    @override
    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """JSON-encode non-strings in string fields, and null text in number fields.

        Fields whose type varies are declared as strings, because some targets fail on
        columns that mix objects and scalars. A number field holds text such as
        "loading" while Apollo is still calculating it (a sequence's click rates): that
        is no value yet, so it is emitted as null rather than failing the load.

        Returns:
            The record.
        """
        for name in self._string_fields & row.keys():
            value = row[name]
            if value is not None and not isinstance(value, str):
                # The SDK parses JSON numbers as Decimal.
                row[name] = json.dumps(value, default=float)
        for name in self._number_fields & row.keys():
            if isinstance(row[name], str) and not _is_number(row[name]):
                row[name] = None
        return row

    def stop_paginating(self, records: list[dict]) -> bool:  # ruff: ignore[unused-method-argument, no-self-use]
        """Return whether pagination should stop early after this page.

        Args:
            records: The records parsed from the latest page.

        Returns:
            True to stop requesting further pages.
        """
        return False

    @override
    def backoff_wait_generator(self) -> Generator[float, None, None]:
        """Wait for Apollo's Retry-After header, or a fixed delay if it is absent.

        Returns:
            The wait generator.
        """
        return self.backoff_runtime(value=self._retry_after)

    @staticmethod
    def _retry_after(exc: Exception) -> float:
        response = getattr(exc, "response", None)
        if not isinstance(exc, RetriableAPIError) or response is None:
            return 10.0
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return float(retry_after)
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            return 60.0
        return 10.0

    @override
    def backoff_max_tries(self) -> int:
        """Apollo's rate limits are per minute/hour, so retry patiently.

        Returns:
            Number of max retries.
        """
        return 8

    @override
    def response_error_message(self, response: requests.Response) -> str:
        """Build an error message, with a hint for Apollo's plan/key restrictions.

        Args:
            response: A :class:`requests.Response` object.

        Returns:
            The error message.
        """
        msg = super().response_error_message(response)
        if response.status_code == HTTPStatus.FORBIDDEN:
            msg += (
                f". Your Apollo API key may not have access to the endpoint of stream "
                f"'{self.name}'. Use a master API key, give the key access to this "
                "endpoint, or deselect the stream."
            )
        return msg
