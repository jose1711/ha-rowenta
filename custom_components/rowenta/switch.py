"""Schedule switches (enable/disable a single schedule)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RowentaConfigEntry
from .const import DOMAIN
from .coordinator import RowentaCoordinator
from .entity import RowentaEntity
from .schedules import apply_changes, find, to_ha

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RowentaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coord in entry.runtime_data.coordinators.values():
        known: set[str] = set()

        @callback
        def _sync(coord: RowentaCoordinator = coord, known: set[str] = known) -> None:
            ids = {sch["id"] for sch in coord.data.schedules if sch.get("id")}
            if new := ids - known:
                known |= new
                async_add_entities(RowentaScheduleSwitch(coord, sid) for sid in sorted(new))
            # Remove deleted schedules from the entity registry too.
            if gone := known - ids:
                ent_reg = er.async_get(hass)
                for sid in gone:
                    unique_id = f"{coord.appliance.id}_schedule_{sid}"
                    if entity_id := ent_reg.async_get_entity_id("switch", DOMAIN, unique_id):
                        ent_reg.async_remove(entity_id)
                known -= gone

        _sync()
        entry.async_on_unload(coord.async_add_listener(_sync))


class RowentaScheduleSwitch(RowentaEntity, SwitchEntity):
    """Enable/disable one schedule."""

    _attr_translation_key = "schedule"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RowentaCoordinator, schedule_id: str) -> None:
        super().__init__(coordinator, f"schedule_{schedule_id}")
        self._schedule_id = schedule_id

    @property
    def _raw(self) -> dict[str, Any] | None:
        schedules = self.coordinator.data.schedules
        idx = find(schedules, self._schedule_id)
        return schedules[idx] if idx is not None else None

    @property
    def translation_placeholders(self) -> dict[str, str]:
        raw = self._raw or {}
        return {"name": str(raw.get("name") or self._schedule_id[:8]), "start": str(raw.get("start") or "")}

    @property
    def available(self) -> bool:
        return super().available and self._raw is not None

    @property
    def is_on(self) -> bool | None:
        raw = self._raw
        return bool(raw.get("enabled")) if raw else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._raw
        return to_ha(raw) if raw else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set_enabled(False)

    def _set_enabled(self, enabled: bool) -> None:
        schedules = list(self.coordinator.data.schedules)
        idx = find(schedules, self._schedule_id)
        if idx is None:
            return
        schedules[idx] = apply_changes(schedules[idx], enabled=enabled)
        self.coordinator.set_schedules(schedules)
