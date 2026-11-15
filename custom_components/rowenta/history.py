"""Cleaning history (MQTT topic `history`, field `datasHistory`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util


def ms_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt_util.utc_from_timestamp(value / 1000)


def lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) else None


def cleaning_to_ha(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status") or {}
    start, end = ms_to_dt(item.get("startTimestamp")), ms_to_dt(item.get("endTimestamp"))
    return {
        "map_id": str(item.get("mapId")),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "area": item.get("cleaningArea"),
        "duration": item.get("cleaningDuration"),
        "method": lower(status.get("method")),
        "power": lower(status.get("power")),
        "error": lower(status.get("errorCode")),
        "battery": status.get("battery"),
    }


def cleaning_label(item: dict[str, Any]) -> str:
    """E.g. "2026-11-14 19:33 · 13 m² · 16 min" (HA local time)."""
    start = ms_to_dt(item.get("startTimestamp"))
    when = dt_util.as_local(start).strftime("%Y-%m-%d %H:%M") if start else str(item.get("mapId"))
    parts = [when]
    if (area := item.get("cleaningArea")) is not None:
        parts.append(f"{area} m²")
    if (duration := item.get("cleaningDuration")) is not None:
        parts.append(f"{duration} min")
    error = (item.get("status") or {}).get("errorCode")
    if error and error != "NORMAL":
        parts.append("⚠")
    return " · ".join(parts)
