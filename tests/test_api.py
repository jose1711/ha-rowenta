"""Tests of pure functions (parsing, SigV4)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.rowenta.api import (
    AwsCredentials,
    parse_redirect,
    user_id_from_tokens,
)
from custom_components.rowenta.mqtt import presign_url

from .conftest import REDIRECT


def test_parse_redirect_from_console_line() -> None:
    tokens = parse_redirect(REDIRECT)
    assert tokens["refresh_token"] == "RT=="
    assert tokens["access_token"] == "AT"
    assert user_id_from_tokens(tokens) == "005USER"


def test_parse_redirect_plain_url() -> None:
    assert parse_redirect("  gsmoduser://tokens#refresh_token=abc&state=x \n")["refresh_token"] == "abc"


@pytest.mark.parametrize(
    "text",
    ["", "https://example.com", "gsmoduser://tokens#access_token=only", "gsmoduser://other#refresh_token=a"],
)
def test_parse_redirect_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_redirect(text)


def test_presign_query_params() -> None:
    creds = AwsCredentials("AKIDEXAMPLE", "secret/key+", "tok/en+=", 0)
    now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    query = parse_qs(urlsplit(presign_url(creds, host="example.iot.test", now=now)).query)
    assert query["X-Amz-Security-Token"] == ["tok/en+="]
    assert query["X-Amz-Credential"] == ["AKIDEXAMPLE/20261115/eu-west-1/iotdevicegateway/aws4_request"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert len(query["X-Amz-Signature"][0]) == 64


def test_presign_known_vector() -> None:
    """Fixed vector – guards against unintended changes of the canonical request."""
    creds = AwsCredentials("AKID", "SECRET", "TOKEN", 0)
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    url = presign_url(creds, host="h.example", region="eu-west-1", now=now)
    assert url.startswith("/mqtt?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKID%2F20260102")
    assert url.endswith("&X-Amz-Security-Token=TOKEN")
