"""Base entity class."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RowentaCoordinator


class RowentaEntity(CoordinatorEntity[RowentaCoordinator]):
    """Entity bound to one robot."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RowentaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        appl = coordinator.appliance
        self._attr_unique_id = f"{appl.id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, appl.id)},
            manufacturer="Rowenta",
            name=appl.name,
            # The profile has no readable model name (only an internal code like APPLIANCE_1092).
            model="X-plorer",
            model_id=appl.thing_type,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.available
