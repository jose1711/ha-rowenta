"""Options flow: schedule management (Settings → Rowenta → Configure)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .schedules import (
    DAYS,
    SCHEDULE_METHODS,
    SCHEDULE_POWERS,
    apply_changes,
    find,
    to_ha,
)

if TYPE_CHECKING:
    from .coordinator import RowentaCoordinator

DAY_SHORT = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
DEFAULT_SCHEDULE = {
    "name": "Schedule",
    "start": "09:00:00",
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "method": "smart_cleaning",
    "power": "normal",
    "enabled": True,
}


def _select(options: list[str], key: str, multiple: bool = False) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            translation_key=key,
            multiple=multiple,
            mode=SelectSelectorMode.LIST if multiple else SelectSelectorMode.DROPDOWN,
        )
    )


def _schedule_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("name", default=defaults["name"]): TextSelector(),
            vol.Required("start", default=defaults["start"]): TimeSelector(),
            vol.Required("days", default=defaults["days"]): _select(DAYS, "day", multiple=True),
            vol.Required("method", default=defaults["method"]): _select(SCHEDULE_METHODS, "schedule_method"),
            vol.Required("power", default=defaults["power"]): _select(SCHEDULE_POWERS, "schedule_power"),
            vol.Required("enabled", default=defaults["enabled"]): BooleanSelector(),
        }
    )


def _label(sch: dict[str, Any]) -> str:
    ha = to_ha(sch)
    days = ", ".join(DAY_SHORT.get(d, d) for d in ha["days"])
    state = "" if ha["enabled"] else " (off)"
    return f"{ha['name']} – {ha['start']} {days}{state}"


class RowentaOptionsFlow(OptionsFlow):
    """Add, edit and delete robot schedules."""

    def __init__(self) -> None:
        self._robot_id: str | None = None
        self._schedule_id: str | None = None

    # --- helpers ---

    def _coordinators(self) -> dict[str, RowentaCoordinator]:
        return self.config_entry.runtime_data.coordinators

    def _coordinator(self) -> RowentaCoordinator:
        assert self._robot_id
        return self._coordinators()[self._robot_id]

    def _save(self, schedules: list[dict[str, Any]]) -> dict[str, str]:
        try:
            self._coordinator().set_schedules(schedules)
        except HomeAssistantError as err:
            return {"base": err.translation_key or "not_connected"}
        return {}

    def _done(self) -> ConfigFlowResult:
        # Schedules live in the robot; the entry options stay unchanged.
        return self.async_create_entry(data=dict(self.config_entry.options))

    # --- steps ---

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        coords = self._coordinators()
        if len(coords) == 1:
            self._robot_id = next(iter(coords))
            return await self.async_step_menu()
        if user_input is not None:
            self._robot_id = user_input["robot"]
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("robot"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=rid, label=c.appliance.name)
                                for rid, c in coords.items()
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["add_schedule"]
        if self._coordinator().data.schedules:
            options += ["edit_schedule", "delete_schedule"]
        return self.async_show_menu(step_id="menu", menu_options=options)

    async def async_step_add_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input["days"]:
                errors["days"] = "no_days"
            else:
                schedules = list(self._coordinator().data.schedules)
                schedules.append(apply_changes(None, **user_input))
                if not (errors := self._save(schedules)):
                    return self._done()
        return self.async_show_form(
            step_id="add_schedule",
            data_schema=self.add_suggested_values_to_schema(_schedule_schema(DEFAULT_SCHEDULE), user_input or {}),
            errors=errors,
        )

    def _pick_schema(self, multiple: bool = False) -> vol.Schema:
        options = [
            SelectOptionDict(value=sch["id"], label=_label(sch))
            for sch in self._coordinator().data.schedules
            if sch.get("id")
        ]
        return vol.Schema(
            {
                vol.Required("schedule"): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=multiple,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )

    async def async_step_edit_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._schedule_id = user_input["schedule"]
            return await self.async_step_edit_schedule_form()
        return self.async_show_form(step_id="edit_schedule", data_schema=self._pick_schema())

    async def async_step_edit_schedule_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schedules = list(self._coordinator().data.schedules)
        idx = find(schedules, self._schedule_id or "")
        if idx is None:
            return self.async_abort(reason="schedule_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input["days"]:
                errors["days"] = "no_days"
            else:
                schedules[idx] = apply_changes(schedules[idx], **user_input)
                if not (errors := self._save(schedules)):
                    return self._done()
        current = to_ha(schedules[idx])
        defaults = {
            "name": current["name"] or "",
            "start": f"{current['start'] or '09:00'}:00",
            "days": current["days"],
            "method": current["method"] if current["method"] in SCHEDULE_METHODS else "smart_cleaning",
            "power": current["power"] if current["power"] in SCHEDULE_POWERS else "normal",
            "enabled": current["enabled"],
        }
        return self.async_show_form(
            step_id="edit_schedule_form",
            data_schema=self.add_suggested_values_to_schema(_schedule_schema(defaults), user_input or {}),
            description_placeholders={"schedule": _label(schedules[idx])},
            errors=errors,
        )

    async def async_step_delete_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            remove = set(user_input["schedule"])
            schedules = [s for s in self._coordinator().data.schedules if s.get("id") not in remove]
            if not (errors := self._save(schedules)):
                return self._done()
        return self.async_show_form(
            step_id="delete_schedule", data_schema=self._pick_schema(multiple=True), errors=errors
        )
