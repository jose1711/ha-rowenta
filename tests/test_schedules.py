"""Schedule tests (switch + services)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.rowenta.const import DOMAIN
from custom_components.rowenta.schedules import apply_changes, to_ha

from .conftest import SHADOW, STATUS

VAC = "vacuum.x_plorer_serie_60"
SWITCH = "switch.x_plorer_serie_60_schedule_weekdays_09_00"


def test_to_ha_and_apply() -> None:
    raw = SHADOW["state"]["reported"]["schedules"][0]
    ha = to_ha(raw)
    assert ha == {
        "id": "x", "name": "Weekdays", "enabled": True, "start": "09:00",
        "days": ["mon", "wed", "sat"], "method": "smart_cleaning", "power": "normal",
    }
    changed = apply_changes(raw, days=["sun", "mon"], power="strong", enabled=False)
    assert changed["days"] == [1, 7]
    assert {"name": "power", "value": "STRONG"} in changed["features"]
    assert {"name": "method", "value": "SMART_CLEANING"} in changed["features"]
    assert changed["enabled"] is False and changed["id"] == "x"
    assert raw["enabled"] is True  # original unchanged
    new = apply_changes(None, name="New", method="wall_follow")
    assert new["id"] != "x" and new["name"] == "New" and new["days"] == [1, 2, 3, 4, 5]


async def _setup(hass, config_entry, fake_mqtt):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt = fake_mqtt.instances[0]
    mqtt.feed("status", STATUS)
    mqtt.feed("shadow/get/accepted", SHADOW)
    await hass.async_block_till_done()
    return mqtt


async def test_switch_toggle(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    state = hass.states.get(SWITCH)
    assert state.state == "on"
    assert state.attributes["days"] == ["mon", "wed", "sat"]

    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    assert mqtt.shadow_updates[-1]["schedules"][0]["enabled"] is False
    assert mqtt.shadow_updates[-1]["schedules"][0]["id"] == "x"
    # optimistic, immediately
    assert hass.states.get(SWITCH).state == "off"

    # robot confirms via reported and desired
    mqtt.feed("shadow/update/documents", {"current": {"state": {
        "desired": {"schedules": mqtt.shadow_updates[-1]["schedules"]},
        "reported": {**SHADOW["state"]["reported"], "schedules": mqtt.shadow_updates[-1]["schedules"]},
    }}})
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get("sensor.x_plorer_serie_60_active_schedules").state == "0"


async def test_schedule_services(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)

    resp = await hass.services.async_call(DOMAIN, "get_schedules", {"entity_id": VAC}, blocking=True, return_response=True)
    assert resp[VAC]["schedules"][0]["name"] == "Weekdays"

    # edit by name – changes only the given fields
    await hass.services.async_call(
        DOMAIN, "set_schedule",
        {"entity_id": VAC, "schedule": "Weekdays", "start": "10:30", "days": ["tue"]}, blocking=True,
    )
    sch = mqtt.shadow_updates[-1]["schedules"]
    assert len(sch) == 1 and sch[0]["start"] == "10:30" and sch[0]["days"] == [2]
    assert {"name": "method", "value": "SMART_CLEANING"} in sch[0]["features"]

    # add a new one
    resp = await hass.services.async_call(
        DOMAIN, "set_schedule",
        {"entity_id": VAC, "name": "Evening", "start": "19:00", "days": ["fri"], "method": "wall_follow", "power": "silent"},
        blocking=True, return_response=True,
    )
    new_id = resp[VAC]["schedule"]["id"]
    sch = mqtt.shadow_updates[-1]["schedules"]
    assert [s["name"] for s in sch] == ["Weekdays", "Evening"]
    await hass.async_block_till_done()
    assert hass.states.get("switch.x_plorer_serie_60_schedule_evening_19_00") is not None

    # delete by id
    await hass.services.async_call(DOMAIN, "delete_schedule", {"entity_id": VAC, "schedule": new_id}, blocking=True)
    assert [s["name"] for s in mqtt.shadow_updates[-1]["schedules"]] == ["Weekdays"]
    await hass.async_block_till_done()
    assert hass.states.get("switch.x_plorer_serie_60_schedule_evening_19_00") is None

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "delete_schedule", {"entity_id": VAC, "schedule": "neexistuje"}, blocking=True)


async def test_set_schedule_empty_key_adds_new(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    """The UI sends an empty string when the field is ticked but empty -> add a new one."""
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    await hass.services.async_call(
        DOMAIN, "set_schedule",
        {"entity_id": VAC, "schedule": "", "start": "07:15:00", "days": ["mon"], "enabled": True},
        blocking=True,
    )
    sch = mqtt.shadow_updates[-1]["schedules"]
    assert len(sch) == 2 and sch[1]["start"] == "07:15" and sch[1]["days"] == [1]
