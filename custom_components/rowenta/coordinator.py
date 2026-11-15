"""State of a single robot (pushed over MQTT)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import Appliance, SebCloud, SebError
from .const import DEFAULT_START_METHOD, DOMAIN, MAP_REFRESH_INTERVAL

if TYPE_CHECKING:
    from . import RowentaConfigEntry
    from .mqtt import SebMqtt

_LOGGER = logging.getLogger(__name__)


ACTIVE_STATUSES = {"CLEANING", "DOCKING"}


@dataclass(slots=True)
class RobotData:
    """Last known robot state."""

    mqtt_connected: bool = False
    # The device shadow (schedules, firmware) has been received at least once.
    shadow_loaded: bool = False
    status: dict[str, Any] = field(default_factory=dict)
    reported: dict[str, Any] = field(default_factory=dict)
    desired: dict[str, Any] = field(default_factory=dict)
    history: dict[str, Any] = field(default_factory=dict)
    # Cleaning history (the robot keeps the last ~20), newest first; derived from `history`.
    cleanings: list[dict[str, Any]] = field(default_factory=list)
    map_points: list[dict[str, Any]] = field(default_factory=list)
    map_id: str | None = None
    map_updated: datetime | None = None

    @property
    def cleaning(self) -> bool:
        """Robot is moving (a paused cleaning does not count: nothing changes)."""
        return self.status.get("status") in ACTIVE_STATUSES and self.status.get("method") != "PAUSE"

    @property
    def schedules(self) -> list[dict[str, Any]]:
        """Schedules – like the app: pending `desired` first, otherwise `reported`."""
        desired = self.desired.get("schedules")
        if isinstance(desired, list):
            return desired
        return list(self.reported.get("schedules") or [])

    @property
    def robot_connected(self) -> bool | None:
        return (self.reported.get("status") or {}).get("connected")

    @property
    def available(self) -> bool:
        return self.mqtt_connected and bool(self.status) and self.robot_connected is not False


def _sorted_cleanings(history: dict[str, Any]) -> list[dict[str, Any]]:
    items = [i for i in history.get("datasHistory") or [] if isinstance(i, dict) and i.get("mapId")]
    return sorted(items, key=lambda i: i.get("startTimestamp") or 0, reverse=True)


class RowentaCoordinator(DataUpdateCoordinator[RobotData]):
    """Holds the state of one robot; data arrives over MQTT (no polling)."""

    config_entry: RowentaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RowentaConfigEntry, appliance: Appliance) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {appliance.name}",
            update_interval=None,
        )
        self.appliance = appliance
        self.mqtt: SebMqtt | None = None
        self.cloud: SebCloud | None = None
        self.start_method = DEFAULT_START_METHOD
        self.data = RobotData()
        self._map_fetching = False
        self._map_pending_force = False
        self._map_fetched_at = 0.0
        self.selected_map_id: str | None = None
        self._firmware: str | None = None

    async def _async_update_data(self) -> RobotData:
        # Manual refresh (e.g. homeassistant.update_entity) = request the status.
        if self.mqtt and self.mqtt.can_publish:
            try:
                self.mqtt.request_status(self.appliance)
            except ConnectionError:
                pass
        return self.data

    @callback
    def handle_message(self, kind: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        data = self.data
        if kind == "status":
            data = replace(data, status=payload)
        elif kind == "history":
            data = replace(data, history=payload, cleanings=_sorted_cleanings(payload))
        elif kind == "shadow/get/accepted":
            state = payload.get("state") or {}
            data = replace(
                data,
                shadow_loaded=True,
                reported=state.get("reported") or {},
                desired=state.get("desired") or {},
            )
        elif kind == "shadow/update/documents":
            state = (payload.get("current") or {}).get("state") or {}
            if "reported" not in state and "desired" not in state:
                return
            # `current` is the complete document: a missing `desired` means it was cleared.
            data = replace(
                data,
                shadow_loaded=True,
                reported=state.get("reported") or {},
                desired=state.get("desired") or {},
            )
        else:
            return
        old = self.data
        self._update_firmware(data.reported)
        if kind == "history" and self.selected_map_id is not None and not any(
            str(i.get("mapId")) == self.selected_map_id for i in data.cleanings
        ):
            # The selected cleaning dropped out of the robot's history: follow the latest again.
            self.selected_map_id = None
        self.async_set_updated_data(data)
        if kind == "history":
            self._request_map()
        if kind == "status":
            if old.cleaning and not data.cleaning and self.mqtt and self.mqtt.can_publish:
                # The cleaning just ended: refresh the history (last cleaning, totals) now.
                try:
                    self.mqtt.request_history(self.appliance)
                except ConnectionError:
                    pass  # the periodic history poll catches up
            self._maybe_refresh_map(old, data)

    # --- map ---

    @property
    def latest_map_id(self) -> str | None:
        map_id = self.data.status.get("mapId")
        return str(map_id) if map_id else None

    @property
    def target_map_id(self) -> str | None:
        """Map to display: the selected cleaning, otherwise the current/latest one."""
        return self.selected_map_id or self.latest_map_id

    @callback
    def select_map(self, map_id: str | None) -> None:
        """None = follow the latest cleaning."""
        self.selected_map_id = map_id
        self._request_map(force=True)

    @callback
    def _maybe_refresh_map(self, old: RobotData, new: RobotData) -> None:
        following_latest = self.selected_map_id is None
        due = following_latest and (
            (new.cleaning and time.monotonic() - self._map_fetched_at >= MAP_REFRESH_INTERVAL.total_seconds())
            or (old.cleaning and not new.cleaning)
        )
        self._request_map(force=due)

    @callback
    def _request_map(self, force: bool = False) -> None:
        map_id = self.target_map_id
        if not map_id or self.cloud is None:
            return
        if self._map_fetching:
            # Remember a forced refresh (e.g. end of cleaning) and run it afterwards.
            self._map_pending_force |= force
            return
        if not force and map_id == self.data.map_id:
            return
        self._map_fetching = True
        self.config_entry.async_create_background_task(
            self.hass, self._async_fetch_map(map_id), f"{DOMAIN}_map_{self.appliance.id}"
        )

    async def _async_fetch_map(self, map_id: str) -> None:
        assert self.cloud is not None
        try:
            result = await self.cloud.async_get_gyro_map(self.appliance, map_id)
        except SebError as err:
            _LOGGER.debug("Fetching the map failed: %s", err)
            result = None
        finally:
            self._map_fetching = False
            self._map_fetched_at = time.monotonic()
        if result is not None:
            points = result.get("map") if isinstance(result.get("map"), list) else []
            self.async_set_updated_data(
                replace(self.data, map_points=points, map_id=map_id, map_updated=dt_util.utcnow())
            )
        # A forced refresh may have been requested, or the selection changed meanwhile.
        force, self._map_pending_force = self._map_pending_force, False
        if force or self.target_map_id != map_id:
            self._request_map(force=force)

    @callback
    def _update_firmware(self, reported: dict[str, Any]) -> None:
        firmware = (reported.get("firmware") or {}).get("version")
        if not firmware or firmware == self._firmware:
            return
        dev_reg = dr.async_get(self.hass)
        device = next(
            (
                d
                for d in dr.async_entries_for_config_entry(dev_reg, self.config_entry.entry_id)
                if (DOMAIN, self.appliance.id) in d.identifiers
            ),
            None,
        )
        if device is None:
            return  # not registered yet, try again with the next message
        self._firmware = firmware
        if device.sw_version != firmware:
            dev_reg.async_update_device(device.id, sw_version=firmware)

    @callback
    def handle_connection(self, connected: bool) -> None:
        self.async_set_updated_data(replace(self.data, mqtt_connected=connected))

    def _require_mqtt(self) -> SebMqtt:
        if self.mqtt is None or not self.mqtt.can_publish:
            raise self._not_connected()
        return self.mqtt

    @staticmethod
    def _not_connected() -> HomeAssistantError:
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="not_connected")

    def set_schedules(self, schedules: list[dict[str, Any]]) -> None:
        """Write the full schedule list to the shadow `desired` state (as the app does)."""
        mqtt = self._require_mqtt()
        if not self.data.shadow_loaded:
            # The whole list is written at once: without the robot's current
            # schedules a write would delete them.
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="schedules_not_loaded")
        try:
            mqtt.update_shadow(self.appliance, {"schedules": schedules})
        except ConnectionError as err:
            raise self._not_connected() from err
        # Show optimistically; confirmation arrives in shadow/update/documents.
        self.async_set_updated_data(
            replace(self.data, desired={**self.data.desired, "schedules": schedules})
        )

    def send_command(self, command: dict[str, Any]) -> None:
        mqtt = self._require_mqtt()
        try:
            mqtt.send_command(self.appliance, command)
            # Confirmation arrives as a new status; request it right away.
            mqtt.request_status(self.appliance)
        except ConnectionError as err:
            raise self._not_connected() from err
