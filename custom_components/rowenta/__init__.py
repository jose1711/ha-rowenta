"""Rowenta robot vacuums (Groupe SEB cloud, AWS IoT)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import voluptuous as vol
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .api import Appliance, SebAuthError, SebCloud, SebError
from .const import (
    CONF_PERIMETER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    HISTORY_POLL_INTERVAL,
    STATUS_POLL_INTERVAL,
    STATUS_POLL_INTERVAL_ACTIVE,
)
from .coordinator import RowentaCoordinator
from .mqtt import SebMqtt
from .schedules import DAYS, SCHEDULE_METHODS, SCHEDULE_POWERS

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VACUUM,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_SCHEDULE_FIELDS = {
    vol.Optional("name"): cv.string,
    vol.Optional("start"): cv.time,
    vol.Optional("days"): vol.All(cv.ensure_list, [vol.In(DAYS)], vol.Length(min=1)),
    vol.Optional("method"): vol.In(SCHEDULE_METHODS),
    vol.Optional("power"): vol.In(SCHEDULE_POWERS),
    vol.Optional("enabled"): cv.boolean,
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services (entity services on the vacuum)."""
    for name, schema, func, response in (
        ("get_schedules", {}, "async_get_schedules", SupportsResponse.ONLY),
        (
            "set_schedule",
            {vol.Optional("schedule"): cv.string, **_SCHEDULE_FIELDS},
            "async_set_schedule",
            SupportsResponse.OPTIONAL,
        ),
        ("delete_schedule", {vol.Required("schedule"): cv.string}, "async_delete_schedule", SupportsResponse.NONE),
    ):
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=VACUUM_DOMAIN,
            schema=schema,
            func=func,
            supports_response=response,
        )
    return True


@dataclass
class RowentaData:
    cloud: SebCloud
    mqtt: SebMqtt
    coordinators: dict[str, RowentaCoordinator]


type RowentaConfigEntry = ConfigEntry[RowentaData]


async def async_setup_entry(hass: HomeAssistant, entry: RowentaConfigEntry) -> bool:
    cloud = SebCloud(
        async_get_clientsession(hass), entry.data[CONF_REFRESH_TOKEN], entry.data[CONF_PERIMETER]
    )
    try:
        appliances = await cloud.async_get_appliances()
    except SebAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SebError as err:
        raise ConfigEntryNotReady(str(err)) from err

    @callback
    def _store_refresh_token(token: str) -> None:
        if token != entry.data.get(CONF_REFRESH_TOKEN):
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: token})

    cloud.on_refresh_token = _store_refresh_token
    _store_refresh_token(cloud.refresh_token)

    coordinators = {a.id: RowentaCoordinator(hass, entry, a) for a in appliances}

    @callback
    def _on_message(appl: Appliance, kind: str, payload: object) -> None:
        if coord := coordinators.get(appl.id):
            coord.handle_message(kind, payload)

    @callback
    def _on_connection(connected: bool) -> None:
        for coord in coordinators.values():
            coord.handle_connection(connected)

    @callback
    def _on_auth_failed() -> None:
        entry.async_start_reauth(hass)

    mqtt = SebMqtt(hass, cloud, appliances, _on_message, _on_connection, _on_auth_failed)
    for coord in coordinators.values():
        coord.mqtt = mqtt
        coord.cloud = cloud
    entry.runtime_data = RowentaData(cloud, mqtt, coordinators)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await mqtt.async_start()
    entry.async_on_unload(mqtt.async_stop)

    last_poll = 0.0

    @callback
    def _poll_status(_now: datetime) -> None:
        nonlocal last_poll
        if not mqtt.can_publish:
            return
        # More often while cleaning (live map), otherwise once per STATUS_POLL_INTERVAL.
        cleaning = any(c.data.cleaning for c in coordinators.values())
        if not cleaning and time.monotonic() - last_poll < STATUS_POLL_INTERVAL.total_seconds():
            return
        last_poll = time.monotonic()
        try:
            for coord in coordinators.values():
                if not coord.data.shadow_loaded:
                    # The initial shadow/get answer was lost: ask again.
                    mqtt.request_shadow(coord.appliance)
                mqtt.request_status(coord.appliance, listening=True)
        except ConnectionError as err:
            _LOGGER.debug("Status poll failed: %s", err)

    @callback
    def _poll_history(_now: datetime) -> None:
        if not mqtt.can_publish:
            return
        try:
            for coord in coordinators.values():
                mqtt.request_history(coord.appliance)
        except ConnectionError as err:
            _LOGGER.debug("History poll failed: %s", err)

    entry.async_on_unload(async_track_time_interval(hass, _poll_status, STATUS_POLL_INTERVAL_ACTIVE))
    entry.async_on_unload(async_track_time_interval(hass, _poll_history, HISTORY_POLL_INTERVAL))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RowentaConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
