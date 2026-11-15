"""Diagnostics (without tokens, e-mail and identifiers)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from . import RowentaConfigEntry
from .const import CONF_REFRESH_TOKEN

TO_REDACT = {
    CONF_REFRESH_TOKEN,
    CONF_EMAIL,
    "id",
    "functionalId",
    "rcuAssetId",
    "identifier",
    "clientId",
    "mapId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RowentaConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "mqtt_connected": data.mqtt.connected,
        "robots": [
            {
                "name": coord.appliance.name,
                "thing_type": coord.appliance.thing_type,
                "model": coord.appliance.appliance_model,
                "start_method": coord.start_method,
                "state": async_redact_data(asdict(coord.data), TO_REDACT),
            }
            for coord in data.coordinators.values()
        ],
    }
