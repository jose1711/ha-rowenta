"""Sensors for a Rowenta robot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RowentaConfigEntry
from .const import ACCESSORIES, CLEANING_METHODS, ERROR_CODES, STATUSES
from .coordinator import RobotData, RowentaCoordinator
from .entity import RowentaEntity
from .history import cleaning_to_ha, lower, ms_to_dt
from .schedules import to_ha

PARALLEL_UPDATES = 0


def _accessory(data: RobotData, name: str) -> dict[str, Any] | None:
    items = data.status.get("accessories") or data.reported.get("accessories") or []
    return next((a for a in items if a.get("name") == name), None)


def _accessory_wear(name: str) -> Callable[[RobotData], Any]:
    def _get(data: RobotData) -> Any:
        acc = _accessory(data, name)
        if not acc or not acc.get("limit"):
            return None
        return round(100 * acc.get("current", 0) / acc["limit"])

    return _get


@dataclass(frozen=True, kw_only=True)
class RowentaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RobotData], Any]
    attrs_fn: Callable[[RobotData], dict[str, Any] | None] | None = None


SENSORS: tuple[RowentaSensorDescription, ...] = (
    RowentaSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.status.get("battery"),
    ),
    RowentaSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        value_fn=lambda d: lower(d.status.get("status")),
    ),
    RowentaSensorDescription(
        key="error",
        translation_key="error",
        device_class=SensorDeviceClass.ENUM,
        options=ERROR_CODES,
        value_fn=lambda d: lower(d.status.get("errorCode")),
    ),
    RowentaSensorDescription(
        key="cleaning_method",
        translation_key="cleaning_method",
        device_class=SensorDeviceClass.ENUM,
        options=CLEANING_METHODS,
        value_fn=lambda d: lower(d.status.get("method")),
    ),
    RowentaSensorDescription(
        key="cleaning_area",
        translation_key="cleaning_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.status.get("cleaningArea"),
    ),
    RowentaSensorDescription(
        key="cleaning_time",
        translation_key="cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.status.get("cleaningTime"),
    ),
    RowentaSensorDescription(
        key="cleaning_start",
        translation_key="cleaning_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: ms_to_dt(d.status.get("startTimestamp")),
    ),
    *(
        RowentaSensorDescription(
            key=f"{key}_wear",
            translation_key=f"{key}_wear",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_accessory_wear(name),
            attrs_fn=lambda d, name=name: _accessory(d, name),
        )
        for name, key in ACCESSORIES.items()
    ),
    RowentaSensorDescription(
        key="total_cleaning_area",
        translation_key="total_cleaning_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.history.get("totalCleaningArea"),
    ),
    RowentaSensorDescription(
        key="total_cleaning_time",
        translation_key="total_cleaning_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.history.get("totalCleaningDuration"),
    ),
    RowentaSensorDescription(
        key="total_cleanings",
        translation_key="total_cleanings",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.history.get("totalNumberOfClean"),
    ),
    RowentaSensorDescription(
        key="last_cleaning",
        translation_key="last_cleaning",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: ms_to_dt(d.cleanings[0].get("endTimestamp")) if d.cleanings else None,
        attrs_fn=lambda d: _last_cleaning_attrs(d),
    ),
    RowentaSensorDescription(
        key="schedules",
        translation_key="schedules",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: sum(1 for s in d.schedules if s.get("enabled")),
        attrs_fn=lambda d: {"schedules": _schedules(d)},
    ),
)


def _last_cleaning_attrs(data: RobotData) -> dict[str, Any] | None:
    items = [cleaning_to_ha(i) for i in data.cleanings]
    if not items:
        return None
    last = items[0]
    return {
        "start": last["start"],
        "area": last["area"],
        "duration": last["duration"],
        "method": last["method"],
        "error": last["error"],
        "cleanings": items,
    }


def _schedules(data: RobotData) -> list[dict[str, Any]]:
    return [to_ha(sch) for sch in data.schedules]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        RowentaSensor(coord, desc)
        for coord in entry.runtime_data.coordinators.values()
        for desc in SENSORS
    )


class RowentaSensor(RowentaEntity, SensorEntity):
    entity_description: RowentaSensorDescription
    # Large lists that change rarely – keep them out of the recorder database.
    _unrecorded_attributes = frozenset({"cleanings", "schedules"})

    def __init__(self, coordinator: RowentaCoordinator, description: RowentaSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value_fn(self.coordinator.data)
        if self.entity_description.device_class == SensorDeviceClass.ENUM and value not in (
            self.entity_description.options or []
        ):
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
