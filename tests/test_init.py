"""Tests of setup, entities and commands."""

from __future__ import annotations

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.rowenta.api import SebAuthError, SebConnectionError
from custom_components.rowenta.const import DOMAIN
from custom_components.rowenta.vacuum import activity_from_status

from .conftest import APPLIANCE, HISTORY, SHADOW, STATUS

VAC = "vacuum.x_plorer_serie_60"


async def _setup(hass, config_entry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_states(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    # Without a status the robot is unavailable
    assert hass.states.get(VAC).state == "unavailable"

    mqtt = fake_mqtt.instances[0]
    mqtt.feed("status", STATUS)
    mqtt.feed("shadow/get/accepted", SHADOW)
    mqtt.feed("history", HISTORY)
    await hass.async_block_till_done()

    state = hass.states.get(VAC)
    assert state.state == VacuumActivity.CLEANING
    assert state.attributes["fan_speed"] == "strong"
    assert hass.states.get("sensor.x_plorer_serie_60_battery").state == "87"
    assert hass.states.get("sensor.x_plorer_serie_60_status").state == "cleaning"
    assert hass.states.get("sensor.x_plorer_serie_60_error").state == "normal"
    assert hass.states.get("sensor.x_plorer_serie_60_filter_wear").state == "25"
    assert hass.states.get("sensor.x_plorer_serie_60_total_cleanings").state == "296"
    schedules = hass.states.get("sensor.x_plorer_serie_60_active_schedules")
    assert schedules.state == "1"
    assert schedules.attributes["schedules"][0]["days"] == ["mon", "wed", "sat"]
    assert hass.states.get("binary_sensor.x_plorer_serie_60_cloud_connection").state == "on"

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, APPLIANCE.id)})
    assert device.sw_version == "VAC001_WIFI_2.7.1"

    # Robot offline per shadow -> vacuum unavailable, connectivity off
    mqtt.feed("shadow/update/documents", {"current": {"state": {"reported": {"status": {"connected": False}}}}})
    await hass.async_block_till_done()
    assert hass.states.get(VAC).state == "unavailable"
    assert hass.states.get("binary_sensor.x_plorer_serie_60_cloud_connection").state == "off"

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_commands(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    await _setup(hass, config_entry)
    mqtt = fake_mqtt.instances[0]
    mqtt.feed("status", {**STATUS, "status": "CHARGING", "method": "PAUSE", "power": "NORMAL"})
    mqtt.feed("shadow/get/accepted", SHADOW)
    await hass.async_block_till_done()
    assert hass.states.get(VAC).state == VacuumActivity.DOCKED

    async def call(service, **data):
        await hass.services.async_call("vacuum", service, {"entity_id": VAC, **data}, blocking=True)

    await call("start")
    await call("pause")
    await call("return_to_base")
    await call("locate")
    await call("clean_spot")
    await call("set_fan_speed", fan_speed="silent")
    await call("send_command", command="wall_follow", params={"power": "strong"})
    await call("send_command", command="raw", params={"beep": "ON"})
    assert mqtt.commands == [
        {"method": "SMART_CLEANING", "power": "NORMAL"},
        {"method": "PAUSE"},
        {"method": "BACK_TO_DOCK"},
        {"beep": "ON"},
        {"method": "SPIRAL", "power": "NORMAL"},
        {"power": "SILENT"},
        {"method": "WALL_FOLLOW", "power": "STRONG"},
        {"beep": "ON"},
    ]

    # A mode change via the select is used on start
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.x_plorer_serie_60_cleaning_mode", "option": "wall_follow"}, blocking=True,
    )
    await call("start")
    assert mqtt.commands[-1] == {"method": "WALL_FOLLOW", "power": "NORMAL"}

    with pytest.raises(ServiceValidationError):
        await call("set_fan_speed", fan_speed="turbo")
    with pytest.raises(ServiceValidationError):
        await call("send_command", command="explode")


@pytest.mark.parametrize(
    ("status", "method", "expected"),
    [
        ("CLEANING", "SMART_CLEANING", VacuumActivity.CLEANING),
        ("CLEANING", "BACK_TO_DOCK", VacuumActivity.RETURNING),
        ("CLEANING", "PAUSE", VacuumActivity.PAUSED),
        ("DOCKING", "BACK_TO_DOCK", VacuumActivity.RETURNING),
        ("CHARGING", "PAUSE", VacuumActivity.DOCKED),
        ("STANDBY", "PAUSE", VacuumActivity.IDLE),
        ("ERROR", "SMART_CLEANING", VacuumActivity.ERROR),
    ],
)
def test_activity_mapping(status, method, expected) -> None:
    assert activity_from_status({"status": status, "method": method}) is expected


async def test_setup_auth_failed(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mock_cloud.async_get_appliances.side_effect = SebAuthError("x")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(config_entry.async_get_active_flows(hass, {"reauth"}))


async def test_setup_not_ready(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mock_cloud.async_get_appliances.side_effect = SebConnectionError("x")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
