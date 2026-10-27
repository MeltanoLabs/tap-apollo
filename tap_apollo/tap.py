"""Apollo tap class."""

from __future__ import annotations

import sys

from singer_sdk import Tap
from singer_sdk import typing as th  # JSON schema typing helpers

from tap_apollo import streams

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class TapApollo(Tap):
    """Singer tap for Apollo."""

    name = "tap-apollo"
    package_name = "meltanolabs-tap-apollo"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "api_key",
            th.StringType(nullable=False),
            required=True,
            secret=True,  # Flag config as protected.
            title="API Key",
            description=(
                "Apollo API key. A master API key is required for some streams "
                "(e.g. users, emailer_messages)."
            ),
        ),
        th.Property(
            "start_date",
            th.DateTimeType(nullable=True),
            title="Start Date",
            description=(
                "Earliest update time to extract for incremental streams "
                "(contacts). Other streams are always fully replicated."
            ),
        ),
    ).to_dict()

    @override
    def discover_streams(self) -> list[streams.ApolloStream]:
        """Return a list of discovered streams.

        Returns:
            A list of discovered streams.
        """
        return [
            streams.ContactsStream(self),
            streams.AccountsStream(self),
            streams.OpportunitiesStream(self),
            streams.TasksStream(self),
            streams.PhoneCallsStream(self),
            streams.SequencesStream(self),
            streams.EmailerMessagesStream(self),
            streams.UsersStream(self),
            streams.EmailAccountsStream(self),
            streams.ListsStream(self),
            streams.CustomFieldsStream(self),
            streams.ContactStagesStream(self),
            streams.AccountStagesStream(self),
            streams.OpportunityStagesStream(self),
        ]


if __name__ == "__main__":
    TapApollo.cli()
