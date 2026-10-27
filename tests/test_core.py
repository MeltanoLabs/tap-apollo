"""Tests standard tap features using the built-in SDK tests library."""

import os

import pytest
from singer_sdk.testing import get_tap_test_class

from tap_apollo.tap import TapApollo

SAMPLE_CONFIG = {
    "api_key": os.environ.get("TAP_APOLLO_API_KEY", ""),
    "start_date": "2026-01-01T00:00:00Z",
}


# Run standard built-in tap tests from the SDK against the live API.
TestTapApollo = pytest.mark.skipif(
    not SAMPLE_CONFIG["api_key"],
    reason="TAP_APOLLO_API_KEY is not set",
)(
    get_tap_test_class(
        tap_class=TapApollo,
        config=SAMPLE_CONFIG,
    )
)
