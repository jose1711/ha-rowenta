"""Testy mapy."""

from __future__ import annotations

import io
from http import HTTPStatus

from homeassistant.core import HomeAssistant
from PIL import Image

from custom_components.rowenta.map_render import COLORS, render_map

from .conftest import SHADOW, STATUS

POINTS = [
    {"index": 0, "x": 128, "y": 128, "type": 3},
    {"index": 1, "x": 129, "y": 128, "type": 1},
    {"index": 2, "x": 130, "y": 128, "type": 2},
    {"index": 3, "x": 130, "y": 129, "type": 3},
]


def test_render_map() -> None:
    png = render_map(POINTS)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.mode == "RGBA"
    # 3×2 cells + padding of 2 on each side, cell max 24 px
    assert img.size == (7 * 24, 6 * 24)
    colors = {c for _, c in img.getcolors(10000)}
    assert COLORS["obstacle"] in colors and COLORS["cleaned"] in colors and COLORS["robot"] in colors


def test_robot_at_last_position() -> None:
    img = Image.open(io.BytesIO(render_map(POINTS)))
    # last type-3 point is (130, 129): column 130-126=4, row 131-129=2 (padding 2, y up)
    assert img.getpixel((4 * 24 + 12, 2 * 24 + 12)) == COLORS["robot"]


def test_render_empty_map() -> None:
    img = Image.open(io.BytesIO(render_map([])))
    assert img.size[0] > 0


def test_out_of_grid_points_are_ignored() -> None:
    bogus = [{"index": 9, "x": 65535, "y": 128, "type": 1}, {"index": 10, "x": 128, "y": -5, "type": 1}, None]
    small = Image.open(io.BytesIO(render_map(POINTS)))
    img = Image.open(io.BytesIO(render_map([*POINTS, *bogus])))
    assert img.size == small.size


async def test_map_entity(hass: HomeAssistant, hass_client, mock_cloud, fake_mqtt, config_entry) -> None:
    mock_cloud.async_get_gyro_map.return_value = {"mapId": 1, "map": POINTS}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt = fake_mqtt.instances[0]
    assert hass.states.get("image.x_plorer_serie_60_map").state == "unavailable"

    mqtt.feed("shadow/get/accepted", SHADOW)
    mqtt.feed("status", {**STATUS, "mapId": 1})
    await hass.async_block_till_done()
    mock_cloud.async_get_gyro_map.assert_awaited_once()
    state = hass.states.get("image.x_plorer_serie_60_map")
    assert state.state != "unavailable" and state.attributes["points"] == 4

    client = await hass_client()
    resp = await client.get("/api/image_proxy/image.x_plorer_serie_60_map")
    assert resp.status == HTTPStatus.OK
    assert (await resp.read()).startswith(b"\x89PNG")

    # same mapId, robot not cleaning -> no new download
    mqtt.feed("status", {**STATUS, "mapId": 1, "status": "CHARGING"})
    await hass.async_block_till_done()
    # ...but the end of cleaning (CLEANING -> CHARGING) fetches the final map
    assert mock_cloud.async_get_gyro_map.await_count == 2
    mqtt.feed("status", {**STATUS, "mapId": 1, "status": "CHARGING"})
    await hass.async_block_till_done()
    assert mock_cloud.async_get_gyro_map.await_count == 2


HISTORY_ITEMS = {
    "totalCleaningArea": 100, "totalCleaningDuration": 200, "totalNumberOfClean": 5,
    "datasHistory": [
        {"startTimestamp": 1794655149726, "mapId": 1794655149726, "endTimestamp": 1794656144576,
         "cleaningArea": 13, "cleaningDuration": 16,
         "status": {"method": "SMART_CLEANING", "errorCode": "NORMAL", "power": "NORMAL", "battery": 49, "status": "CLEANING"}},
        {"startTimestamp": 1794644998016, "mapId": 1794644998016, "endTimestamp": 1794645678596,
         "cleaningArea": 9, "cleaningDuration": 11,
         "status": {"method": "SMART_CLEANING", "errorCode": "BUMPER_ERROR", "power": "NORMAL", "battery": 20, "status": "CLEANING"}},
    ],
}
SELECT = "select.x_plorer_serie_60_show_cleaning_on_map"


async def test_history_and_map_select(hass: HomeAssistant, mock_cloud, fake_mqtt, config_entry) -> None:
    await hass.config.async_set_time_zone("Europe/Paris")
    mock_cloud.async_get_gyro_map.return_value = {"map": POINTS}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt = fake_mqtt.instances[0]
    mqtt.feed("shadow/get/accepted", SHADOW)
    mqtt.feed("history", HISTORY_ITEMS)
    mqtt.feed("status", {**STATUS, "status": "CHARGING", "mapId": 1794655149726})
    await hass.async_block_till_done()

    last = hass.states.get("sensor.x_plorer_serie_60_last_cleaning")
    assert last.state.startswith("2026-11-")
    assert last.attributes["area"] == 13 and len(last.attributes["cleanings"]) == 2
    assert last.attributes["cleanings"][1]["error"] == "bumper_error"

    select = hass.states.get(SELECT)
    assert select.state == "latest"
    options = select.attributes["options"]
    assert options[0] == "latest" and options[1].endswith("13 m² · 16 min") and options[2].endswith("⚠")
    assert mock_cloud.async_get_gyro_map.await_args.args[1] == "1794655149726"

    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": options[2]}, blocking=True)
    await hass.async_block_till_done()
    assert mock_cloud.async_get_gyro_map.await_args.args[1] == "1794644998016"
    assert hass.states.get(SELECT).state == options[2]

    # a new status does not change the map while a past cleaning is selected
    count = mock_cloud.async_get_gyro_map.await_count
    mqtt.feed("status", {**STATUS, "status": "CHARGING", "mapId": 1794655149726})
    await hass.async_block_till_done()
    assert mock_cloud.async_get_gyro_map.await_count == count

    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": "latest"}, blocking=True)
    await hass.async_block_till_done()
    assert mock_cloud.async_get_gyro_map.await_args.args[1] == "1794655149726"
