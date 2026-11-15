"""Vacuum entity for Rowenta X-plorer (SEB VAC001)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RowentaConfigEntry
from .const import CLEANING_METHODS, DOMAIN, FAN_SPEEDS
from .coordinator import RowentaCoordinator
from .entity import RowentaEntity
from .schedules import apply_changes, find, to_ha

PARALLEL_UPDATES = 0

RETURNING_METHODS = {"BACK_TO_DOCK", "CHARGE_GO"}


def activity_from_status(status: dict[str, Any]) -> VacuumActivity | None:
    state = status.get("status")
    method = status.get("method")
    if state is None:
        return None
    if state == "ERROR":
        return VacuumActivity.ERROR
    if state == "CLEANING":
        if method in RETURNING_METHODS:
            return VacuumActivity.RETURNING
        if method == "PAUSE":
            return VacuumActivity.PAUSED
        return VacuumActivity.CLEANING
    if state == "DOCKING":
        return VacuumActivity.RETURNING
    if state == "CHARGING":
        return VacuumActivity.DOCKED
    # STANDBY, STOP, UPGRADING
    return VacuumActivity.IDLE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        RowentaVacuum(coord) for coord in entry.runtime_data.coordinators.values()
    )


class RowentaVacuum(RowentaEntity, StateVacuumEntity):
    """Robot vacuum."""

    _attr_name = None
    _attr_translation_key = "robot"
    _attr_fan_speed_list = FAN_SPEEDS
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.CLEAN_SPOT
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.STATE
    )

    def __init__(self, coordinator: RowentaCoordinator) -> None:
        super().__init__(coordinator, "vacuum")

    @property
    def _status(self) -> dict[str, Any]:
        return self.coordinator.data.status

    @property
    def activity(self) -> VacuumActivity | None:
        return activity_from_status(self._status)

    @property
    def fan_speed(self) -> str | None:
        power = self._status.get("power")
        return power.lower() if isinstance(power, str) else None

    def _power(self) -> str:
        return (self.fan_speed or "normal").upper()

    async def async_start(self) -> None:
        method = self.coordinator.start_method.upper()
        self.coordinator.send_command({"method": method, "power": self._power()})

    async def async_pause(self) -> None:
        self.coordinator.send_command({"method": "PAUSE"})

    async def async_stop(self, **kwargs: Any) -> None:
        # SEB has no separate stop; the app sends PAUSE.
        self.coordinator.send_command({"method": "PAUSE"})

    async def async_return_to_base(self, **kwargs: Any) -> None:
        self.coordinator.send_command({"method": "BACK_TO_DOCK"})

    async def async_clean_spot(self, **kwargs: Any) -> None:
        self.coordinator.send_command({"method": "SPIRAL", "power": self._power()})

    async def async_locate(self, **kwargs: Any) -> None:
        self.coordinator.send_command({"beep": "ON"})

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        if fan_speed not in FAN_SPEEDS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_fan_speed",
                translation_placeholders={"fan_speed": fan_speed},
            )
        self.coordinator.send_command({"power": fan_speed.upper()})

    # --- schedules (rowenta.* services) ---

    async def async_get_schedules(self) -> ServiceResponse:
        return {"schedules": [to_ha(sch) for sch in self.coordinator.data.schedules]}

    async def async_set_schedule(self, schedule: str | None = None, **changes: Any) -> ServiceResponse:
        schedules = list(self.coordinator.data.schedules)
        if schedule is None or not schedule.strip():
            raw = apply_changes(None, **changes)
            schedules.append(raw)
        else:
            idx = find(schedules, schedule)
            if idx is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="schedule_not_found",
                    translation_placeholders={"schedule": schedule},
                )
            raw = schedules[idx] = apply_changes(schedules[idx], **changes)
        self.coordinator.set_schedules(schedules)
        return {"schedule": to_ha(raw)}

    async def async_delete_schedule(self, schedule: str) -> None:
        schedules = list(self.coordinator.data.schedules)
        idx = find(schedules, schedule)
        if idx is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="schedule_not_found",
                translation_placeholders={"schedule": schedule},
            )
        del schedules[idx]
        self.coordinator.set_schedules(schedules)

    async def async_send_command(
        self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any
    ) -> None:
        """`command: raw` sends `params` as JSON; otherwise `command` is a cleaning method."""
        if command == "raw":
            if not isinstance(params, dict):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="raw_needs_dict"
                )
            self.coordinator.send_command(params)
            return
        if command.lower() not in CLEANING_METHODS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_command",
                translation_placeholders={"command": command},
            )
        payload: dict[str, Any] = {"method": command.upper()}
        if isinstance(params, dict) and "power" in params:
            payload["power"] = str(params["power"]).upper()
        self.coordinator.send_command(payload)
