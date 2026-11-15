"""Selects: cleaning mode used by vacuum.start, cleaning shown on the map."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RowentaConfigEntry
from .const import START_METHODS
from .coordinator import RowentaCoordinator
from .entity import RowentaEntity
from .history import cleaning_label

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coords = entry.runtime_data.coordinators.values()
    async_add_entities(
        [RowentaStartMethod(coord) for coord in coords] + [RowentaMapSelect(coord) for coord in coords]
    )


class RowentaStartMethod(RowentaEntity, SelectEntity, RestoreEntity):
    """Local setting – nothing is sent to the robot until a cleaning starts."""

    _attr_translation_key = "start_method"
    _attr_options = START_METHODS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RowentaCoordinator) -> None:
        super().__init__(coordinator, "start_method")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in START_METHODS:
            self.coordinator.start_method = last.state

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str:
        return self.coordinator.start_method

    async def async_select_option(self, option: str) -> None:
        self.coordinator.start_method = option
        self.async_write_ha_state()


LATEST = "latest"


class RowentaMapSelect(RowentaEntity, SelectEntity):
    """Which cleaning to show on the map (latest or one from the history)."""

    _attr_translation_key = "map_cleaning"

    def __init__(self, coordinator: RowentaCoordinator) -> None:
        super().__init__(coordinator, "map_cleaning")

    def _options(self) -> dict[str, str]:
        """label -> mapId (labels must be unique)."""
        result: dict[str, str] = {}
        for item in self.coordinator.data.cleanings:
            label = cleaning_label(item)
            while label in result:
                label += " "
            result[label] = str(item["mapId"])
        return result

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def options(self) -> list[str]:
        return [LATEST, *self._options()]

    @property
    def current_option(self) -> str:
        selected = self.coordinator.selected_map_id
        if selected is None:
            return LATEST
        for label, map_id in self._options().items():
            if map_id == selected:
                return label
        return LATEST

    async def async_select_option(self, option: str) -> None:
        self.coordinator.select_map(None if option == LATEST else self._options().get(option))
        self.async_write_ha_state()
