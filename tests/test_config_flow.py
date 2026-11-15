"""Testy config flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.rowenta.api import SebAuthError, SebConnectionError, SebError
from custom_components.rowenta.const import (
    CONF_COUNTRY,
    CONF_PERIMETER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

USER_INPUT = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret", CONF_COUNTRY: "FR"}


@pytest.fixture
def mock_login() -> Generator[AsyncMock]:
    with patch(
        "custom_components.rowenta.config_flow.async_login",
        AsyncMock(return_value={"refresh_token": "RT==", "access_token": "AT"}),
    ) as m:
        yield m


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


async def test_full_flow(hass: HomeAssistant, mock_cloud, mock_market, mock_login, fake_mqtt) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "RT=="
    assert result["data"][CONF_PERIMETER] == "ROWENTA_FR"
    assert result["data"][CONF_EMAIL] == "user@example.com"
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == "005USER_ROWENTA_FR"
    mock_login.assert_awaited_once_with("user@example.com", "secret")


@pytest.mark.parametrize(
    ("exc", "error"),
    [(SebAuthError("x"), {"base": "invalid_auth"}), (SebConnectionError("x"), {"base": "cannot_connect"})],
)
async def test_login_errors(hass, mock_cloud, mock_market, mock_login, fake_mqtt, exc, error) -> None:
    result = await _start(hass)
    mock_login.side_effect = exc
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == error
    # corrected -> success
    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_appliances(hass, mock_cloud, mock_market, mock_login) -> None:
    mock_cloud.async_get_appliances.return_value = []
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_appliances"}


async def test_unsupported_country(hass: HomeAssistant, mock_market, mock_login) -> None:
    mock_market.side_effect = SebError("x")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_COUNTRY: "XX"})
    assert result["errors"] == {CONF_COUNTRY: "unsupported_country"}
    mock_login.assert_not_awaited()


async def test_market_connection_error(hass: HomeAssistant, mock_market, mock_login) -> None:
    mock_market.side_effect = SebConnectionError("HTTP 503")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}
    mock_login.assert_not_awaited()


async def test_already_configured(hass, mock_cloud, mock_market, mock_login, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_duplicate_detected_before_login(hass, mock_cloud, mock_market, mock_login, config_entry) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_EMAIL: "User@Example.com"}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    mock_login.assert_not_awaited()


async def test_reauth(hass, mock_cloud, mock_market, mock_login, fake_mqtt, config_entry) -> None:
    config_entry.add_to_hass(hass)
    mock_cloud.refresh_token = "NEW"
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_REFRESH_TOKEN] == "NEW"
    assert config_entry.data[CONF_EMAIL] == "user@example.com"


async def test_reauth_wrong_account(hass, mock_cloud, mock_market, mock_login, config_entry) -> None:
    config_entry.add_to_hass(hass)
    mock_cloud.user_id = "005OTHER"
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_account"
