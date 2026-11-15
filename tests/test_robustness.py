"""Error handling, reconnect and state edge cases."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.rowenta.api import (
    AwsCredentials,
    SebAuthError,
    SebCloud,
    SebConnectionError,
    _a4j_submit,
    async_get_market,
)
from custom_components.rowenta.const import COGNITO_ENDPOINT, RCU_BASE, SEB_API
from custom_components.rowenta.diagnostics import async_get_config_entry_diagnostics
from custom_components.rowenta.mqtt import STABLE_CONNECTION, SebMqtt
from custom_components.rowenta.schedules import apply_changes

from .conftest import APPLIANCE, HISTORY, SHADOW, STATUS

TOKEN_URL = f"{RCU_BASE}/services/oauth2/token"


@pytest.mark.parametrize(
    "kwargs",
    [{"exc": TimeoutError()}, {"text": "<html>502</html>", "status": 200}, {"text": "[]", "status": 200}],
)
async def test_refresh_errors_become_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs
) -> None:
    aioclient_mock.post(TOKEN_URL, **kwargs)
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    with pytest.raises(SebConnectionError):
        await cloud.async_refresh_tokens()


async def test_cognito_and_profile_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT", "id_token": "IDT"})
    aioclient_mock.post(COGNITO_ENDPOINT, text="<html>bad gateway</html>", status=502)
    aioclient_mock.get(f"{SEB_API}/common-api/profiles/me", exc=TimeoutError())
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    with pytest.raises(SebConnectionError):
        await cloud.async_get_aws_credentials()
    with pytest.raises(SebConnectionError):
        await cloud.async_get_profile()


async def test_rotated_refresh_token_is_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT", "refresh_token": "RT2"})
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    seen: list[str] = []
    cloud.on_refresh_token = seen.append
    await cloud.async_refresh_tokens()
    assert cloud.refresh_token == "RT2" and seen == ["RT2"]


def _mqtt(hass: HomeAssistant, cloud) -> tuple[SebMqtt, list[bool]]:
    states: list[bool] = []
    return SebMqtt(hass, cloud, [APPLIANCE], MagicMock(), states.append, MagicMock()), states


async def test_stale_disconnect_is_ignored(hass: HomeAssistant) -> None:
    mqtt, states = _mqtt(hass, MagicMock())
    current, old = MagicMock(), MagicMock()
    mqtt._client = current
    mqtt._handle_connected(current)
    mqtt._handle_failure(old, "disconnect")
    assert mqtt.connected and mqtt._client is current and mqtt._timer is None
    # A real failure of the current client schedules exactly one retry.
    mqtt._handle_failure(current, "disconnect")
    mqtt._handle_failure(current, "disconnect")
    await hass.async_block_till_done()
    assert not mqtt.connected and mqtt._attempt == 1 and states == [True, False]
    mqtt._cancel_timer()


async def test_renewal_failure_keeps_connection(hass: HomeAssistant) -> None:
    cloud = MagicMock()
    cloud.async_get_aws_credentials = AsyncMock(side_effect=SebConnectionError("cognito"))
    mqtt, states = _mqtt(hass, cloud)
    current = MagicMock()
    mqtt._client = current
    mqtt._handle_connected(current)
    await mqtt._async_connect()
    assert mqtt.connected and mqtt._client is current and states == [True]
    assert mqtt._timer is not None  # retry scheduled
    mqtt._cancel_timer()


async def _setup(hass, config_entry, fake_mqtt):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return fake_mqtt.instances[0]


async def test_cleared_desired_falls_back_to_reported(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.feed("status", STATUS)
    stale = [{**SHADOW["state"]["reported"]["schedules"][0], "enabled": False}]
    mqtt.feed("shadow/get/accepted", {"state": {**SHADOW["state"], "desired": {"schedules": stale}}})
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    assert coord.data.schedules[0]["enabled"] is False
    # The robot applied the change and cleared desired: the full document has no desired.
    mqtt.feed("shadow/update/documents", {"current": {"state": {"reported": SHADOW["state"]["reported"]}}})
    assert coord.data.desired == {}
    assert coord.data.schedules[0]["enabled"] is True


async def test_forced_map_refresh_during_fetch(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    release = asyncio.Event()
    calls = 0

    async def _slow_map(appl, map_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        return {"mapId": map_id, "map": [{"index": calls, "x": 128, "y": 128, "type": 3}]}

    mock_cloud.async_get_gyro_map.side_effect = _slow_map
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.feed("status", {**STATUS, "mapId": 1})  # starts the (slow) first fetch
    await asyncio.sleep(0)
    mqtt.feed("status", {**STATUS, "mapId": 1, "status": "CHARGING"})  # end of cleaning while fetching
    release.set()
    await hass.async_block_till_done()
    assert calls == 2
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    assert coord.data.map_points[0]["index"] == 2


async def test_diagnostics_redact_email(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "email": "a@b.c"})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["email"] == "**REDACTED**"
    assert diag["entry"]["refresh_token"] == "**REDACTED**"


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (400, {"error": "invalid_grant", "error_description": "expired"}, SebAuthError),
        (400, {"error": "rate_limited"}, SebConnectionError),
        (401, {"error": "temporarily_unavailable"}, SebConnectionError),
    ],
)
async def test_only_rejected_token_is_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body, exc
) -> None:
    aioclient_mock.post(TOKEN_URL, json=body, status=status)
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    with pytest.raises(exc):
        await cloud.async_refresh_tokens()


async def test_market_with_null_fields(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{SEB_API}/common-api/v3/configurations/19UfOfJmK6365857UYNzjn427R6q1XFA",
        json={"markets": [
            None,
            {"rcuMarket": None, "name": None},
            {"rcuMarket": "DE", "name": "GS_DE"},
            {"rcuMarket": "FR", "name": "GS_FR", "profilePerimeter": "ROWENTA_FR"},
        ]},
    )
    market = await async_get_market(async_get_clientsession(hass), "fr")
    assert (market.name, market.perimeter) == ("GS_FR", "ROWENTA_FR")


async def test_backoff_reset_only_after_stable_connection(hass: HomeAssistant) -> None:
    mqtt, _states = _mqtt(hass, MagicMock())
    client = MagicMock()
    mqtt._client = client
    mqtt._attempt = 3
    mqtt._handle_connected(client)
    assert mqtt._attempt == 3  # dropped right after CONNACK -> backoff keeps growing
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STABLE_CONNECTION + 1))
    await hass.async_block_till_done()
    assert mqtt._attempt == 0
    mqtt._cancel_timer()


async def test_auth_failure_still_retries(hass: HomeAssistant) -> None:
    cloud = MagicMock()
    cloud.async_get_aws_credentials = AsyncMock(side_effect=SebAuthError("invalid_grant"))
    on_auth = MagicMock()
    mqtt = SebMqtt(hass, cloud, [APPLIANCE], MagicMock(), MagicMock(), on_auth)
    await mqtt._async_connect()
    on_auth.assert_called_once()
    assert mqtt._timer is not None
    mqtt._cancel_timer()


async def test_history_requested_when_cleaning_ends(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.request_history = MagicMock()
    mqtt.feed("status", STATUS)
    mqtt.request_history.assert_not_called()
    mqtt.feed("status", {**STATUS, "status": "CHARGING"})
    mqtt.request_history.assert_called_once()


async def test_selected_cleaning_dropped_from_history(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    item = {"mapId": 111, "startTimestamp": 1, "endTimestamp": 2}
    mqtt.feed("history", {**HISTORY, "datasHistory": [item]})
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    coord.select_map("111")
    assert coord.selected_map_id == "111"
    mqtt.feed("history", {**HISTORY, "datasHistory": [{**item, "mapId": 222}]})
    assert coord.selected_map_id is None


def test_apply_changes_keeps_other_features() -> None:
    raw = {
        "id": "x",
        "features": [
            {"name": "METHOD", "value": "SMART_CLEANING"},
            {"name": "power", "value": "NORMAL"},
            {"name": "extra", "value": 1},
        ],
    }
    out = apply_changes(raw, power="strong")
    assert out["features"] == [
        {"name": "METHOD", "value": "SMART_CLEANING"},
        {"name": "power", "value": "STRONG"},
        {"name": "extra", "value": 1},
    ]
    assert apply_changes(raw, enabled=False)["features"] == raw["features"]


PROFILE_URL = f"{SEB_API}/common-api/profiles/me"


async def test_rejected_request_is_not_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT", "id_token": "IDT"})
    aioclient_mock.get(PROFILE_URL, status=403)
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    with pytest.raises(SebConnectionError):
        await cloud.async_get_profile()
    # initial token + one forced refresh before the retry
    assert sum(1 for c in aioclient_mock.mock_calls if str(c[1]).startswith(TOKEN_URL)) == 2


async def test_revoked_token_detected_on_rejected_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT"})
    aioclient_mock.get(PROFILE_URL, status=401)
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    await cloud.async_refresh_tokens()
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json={"error": "invalid_grant"}, status=400)
    aioclient_mock.get(PROFILE_URL, status=401)
    with pytest.raises(SebAuthError):
        await cloud.async_get_profile()


async def test_cognito_not_authorized_is_retried(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT", "id_token": "IDT"})
    aioclient_mock.post(COGNITO_ENDPOINT, json={"__type": "NotAuthorizedException"}, status=400)
    cloud = SebCloud(async_get_clientsession(hass), "RT", "ROWENTA_FR")
    with pytest.raises(SebConnectionError):
        await cloud.async_get_aws_credentials()


def _creds_cloud() -> MagicMock:
    cloud = MagicMock()
    cloud.async_get_aws_credentials = AsyncMock(
        side_effect=lambda: AwsCredentials("AK", "SK", "ST", time.time() + 3600)
    )
    return cloud


async def test_fast_connect_failure_keeps_retry_timer(hass: HomeAssistant) -> None:
    """The renewal timer (~55 min) must not replace the retry scheduled by a fast failure."""
    cloud = _creds_cloud()
    mqtt, _states = _mqtt(hass, cloud)

    def _make_client(*_args, **_kwargs):
        client = MagicMock()
        # Fails immediately, before the connecting coroutine continues.
        client.connect_async.side_effect = lambda *a, **k: hass.loop.call_soon_threadsafe(
            mqtt._handle_failure, client, "connect failed"
        )
        return client

    with patch("custom_components.rowenta.mqtt.mqtt.Client", side_effect=_make_client):
        await mqtt._async_connect()
        await hass.async_block_till_done()
        assert mqtt._client is None and mqtt._attempt == 1
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
        await hass.async_block_till_done()
        assert cloud.async_get_aws_credentials.await_count == 2  # retried after 5 s
        await mqtt.async_stop()
    assert mqtt._timer is None


async def test_stop_during_connect_leaves_no_timer(hass: HomeAssistant) -> None:
    release = asyncio.Event()
    cloud = MagicMock()

    async def _slow_creds():
        await release.wait()
        return AwsCredentials("AK", "SK", "ST", time.time() + 3600)

    cloud.async_get_aws_credentials = _slow_creds
    mqtt, _states = _mqtt(hass, cloud)
    with patch("custom_components.rowenta.mqtt.mqtt.Client") as client_cls:
        connect = hass.async_create_task(mqtt._async_connect())
        await asyncio.sleep(0)
        stop = hass.async_create_task(mqtt.async_stop())
        await asyncio.sleep(0)
        release.set()
        await connect
        await stop
    client_cls.assert_not_called()
    assert mqtt._timer is None and mqtt._client is None


async def test_publish_is_never_silently_dropped(hass: HomeAssistant) -> None:
    mqtt, _states = _mqtt(hass, MagicMock())
    client = MagicMock()
    mqtt._client = client
    assert not mqtt.can_publish  # no CONNACK yet (e.g. hourly client swap)
    with pytest.raises(ConnectionError):
        mqtt.send_command(APPLIANCE, {"beep": "ON"})
    mqtt._handle_connected(client)
    client.publish.return_value.rc = 4  # MQTT_ERR_NO_CONN
    with pytest.raises(ConnectionError):
        mqtt.send_command(APPLIANCE, {"beep": "ON"})
    client.publish.return_value.rc = 0
    mqtt.send_command(APPLIANCE, {"beep": "ON"})
    mqtt._detach()


async def test_schedules_not_written_before_shadow(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.feed("status", STATUS)
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    with pytest.raises(HomeAssistantError) as err:
        coord.set_schedules([{"id": "new"}])
    assert err.value.translation_key == "schedules_not_loaded"
    assert mqtt.shadow_updates == []
    mqtt.feed("shadow/get/accepted", SHADOW)
    coord.set_schedules([{"id": "new"}])
    assert mqtt.shadow_updates == [{"schedules": [{"id": "new"}]}]


async def test_failed_publish_is_reported_and_not_shown(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.feed("status", STATUS)
    mqtt.feed("shadow/get/accepted", SHADOW)
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    mqtt.update_shadow = MagicMock(side_effect=ConnectionError("no conn"))
    mqtt.send_command = MagicMock(side_effect=ConnectionError("no conn"))
    with pytest.raises(HomeAssistantError):
        coord.set_schedules([])
    assert len(coord.data.schedules) == 1  # no optimistic update
    with pytest.raises(HomeAssistantError):
        coord.send_command({"method": "PAUSE"})


async def test_paused_robot_is_not_polled_as_cleaning(
    hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry
) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    mqtt.feed("status", {**STATUS, "mapId": 1})
    await hass.async_block_till_done()
    coord = config_entry.runtime_data.coordinators[APPLIANCE.id]
    assert coord.data.cleaning
    mqtt.feed("status", {**STATUS, "mapId": 1, "method": "PAUSE"})
    await hass.async_block_till_done()
    assert not coord.data.cleaning
    calls = mock_cloud.async_get_gyro_map.await_count
    coord._map_fetched_at = 0.0  # refresh interval elapsed
    mqtt.feed("status", {**STATUS, "mapId": 1, "method": "PAUSE"})
    await hass.async_block_till_done()
    assert mock_cloud.async_get_gyro_map.await_count == calls


async def test_refresh_token_sent_in_body(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT"})
    cloud = SebCloud(async_get_clientsession(hass), "SECRET-RT", "ROWENTA_FR")
    await cloud.async_refresh_tokens()
    _method, url, body, _headers = aioclient_mock.mock_calls[0]
    assert "SECRET-RT" not in str(url)
    assert body["refresh_token"] == "SECRET-RT"


A4J_PAGE = (
    '<form id="f" method="post" action="/Login">'
    '<input type="hidden"  id="com.salesforce.visualforce.ViewState" name="v" value="VS" />'
)


@pytest.mark.parametrize("status", [429, 503])
async def test_login_server_error_is_not_wrong_password(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status
) -> None:
    aioclient_mock.post(f"{RCU_BASE}/Login", status=status, text="busy")
    with pytest.raises(SebConnectionError):
        await _a4j_submit(async_get_clientsession(hass), A4J_PAGE, f"{RCU_BASE}/x", "f", "a")


async def test_login_missing_form_is_not_wrong_password(hass: HomeAssistant) -> None:
    with pytest.raises(SebConnectionError):
        await _a4j_submit(async_get_clientsession(hass), "<html></html>", f"{RCU_BASE}/x", "f", "a")


async def test_login_rejected_returns_none(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{RCU_BASE}/Login", status=200, text="<span>error</span>")
    assert await _a4j_submit(async_get_clientsession(hass), A4J_PAGE, f"{RCU_BASE}/x", "f", "a") is None
