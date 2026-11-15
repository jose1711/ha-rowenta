"""Cleaning map (gyro map) as an image entity."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RowentaConfigEntry
from .coordinator import RowentaCoordinator
from .entity import RowentaEntity
from .map_render import render_map

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(RowentaMap(coord) for coord in entry.runtime_data.coordinators.values())


class RowentaMap(RowentaEntity, ImageEntity):
    """PNG map rendered from the gyro map points."""

    _attr_translation_key = "map"
    _attr_content_type = "image/png"

    def __init__(self, coordinator: RowentaCoordinator) -> None:
        RowentaEntity.__init__(self, coordinator, "map")
        ImageEntity.__init__(self, coordinator.hass)
        self._cache_key: datetime | None = None
        self._cache: bytes | None = None

    @property
    def available(self) -> bool:
        # The last map is meaningful even while the robot is offline.
        return self.coordinator.last_update_success and self.coordinator.data.map_updated is not None

    @property
    def image_last_updated(self) -> datetime | None:
        return self.coordinator.data.map_updated

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"points": len(self.coordinator.data.map_points)}

    async def async_image(self) -> bytes | None:
        data = self.coordinator.data
        if data.map_updated is None:
            return None
        if data.map_updated != self._cache_key:
            self._cache = await self.hass.async_add_executor_job(render_map, list(data.map_points))
            self._cache_key = data.map_updated
        return self._cache
