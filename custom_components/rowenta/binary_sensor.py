"""Binary sensors for a Rowenta robot."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RowentaConfigEntry
from .coordinator import RowentaCoordinator
from .entity import RowentaEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        RowentaCloudConnected(coord) for coord in entry.runtime_data.coordinators.values()
    )


class RowentaCloudConnected(RowentaEntity, BinarySensorEntity):
    """Is the robot connected to the cloud (per AWS shadow)?"""

    _attr_translation_key = "cloud_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RowentaCoordinator) -> None:
        super().__init__(coordinator, "cloud_connected")

    @property
    def available(self) -> bool:
        # Meaningful even when the robot is offline – only our MQTT link matters.
        return self.coordinator.last_update_success and self.coordinator.data.mqtt_connected

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.robot_connected
