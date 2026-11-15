"""Tests of schedule management through the options flow."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import SHADOW, STATUS

FORM = {
    "name": "Evening", "start": "19:30:00", "days": ["fri", "sun"],
    "method": "wall_follow", "power": "silent", "enabled": False,
}


async def _setup(hass, config_entry, fake_mqtt):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt = fake_mqtt.instances[0]
    mqtt.feed("status", STATUS)
    mqtt.feed("shadow/get/accepted", SHADOW)
    await hass.async_block_till_done()
    return mqtt


async def _menu(hass, config_entry, choice):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": choice})


async def test_add(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    result = await _menu(hass, config_entry, "add_schedule")
    assert result["step_id"] == "add_schedule"

    bad = await hass.config_entries.options.async_configure(result["flow_id"], {**FORM, "days": []})
    assert bad["errors"] == {"days": "no_days"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sch = mqtt.shadow_updates[-1]["schedules"]
    assert [s["name"] for s in sch] == ["Weekdays", "Evening"]
    new = sch[1]
    assert new["start"] == "19:30" and new["days"] == [5, 7] and new["enabled"] is False
    assert {"name": "method", "value": "WALL_FOLLOW"} in new["features"]
    assert {"name": "power", "value": "SILENT"} in new["features"]
    await hass.async_block_till_done()
    assert hass.states.get("switch.x_plorer_serie_60_schedule_evening_19_30").state == "off"


async def test_edit(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    result = await _menu(hass, config_entry, "edit_schedule")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"schedule": "x"})
    assert result["step_id"] == "edit_schedule_form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**FORM, "name": "Weekdays", "start": "08:00:00"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sch = mqtt.shadow_updates[-1]["schedules"]
    assert len(sch) == 1 and sch[0]["id"] == "x" and sch[0]["start"] == "08:00"


async def test_delete(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    mqtt = await _setup(hass, config_entry, fake_mqtt)
    result = await _menu(hass, config_entry, "delete_schedule")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"schedule": ["x"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mqtt.shadow_updates[-1]["schedules"] == []
