"""SEB robot schedules (AWS shadow `schedules`)."""

from __future__ import annotations

import copy
import uuid
from datetime import time
from typing import Any

# SEB: 1 = Monday … 7 = Sunday
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Methods allowed in a schedule (note: SMALL_ROOM, not SMALL_ROOM_CLEAN as in commands)
SCHEDULE_METHODS = ["smart_cleaning", "auto_cleaning", "wall_follow", "small_room"]
SCHEDULE_POWERS = ["silent", "normal", "strong"]


def _features(raw: dict[str, Any]) -> dict[str, str]:
    return {
        str(f.get("name", "")).lower(): str(f.get("value", ""))
        for f in raw.get("features") or []
        if isinstance(f, dict)
    }


def to_ha(raw: dict[str, Any]) -> dict[str, Any]:
    """Schedule in HA form (attributes, service response)."""
    feats = _features(raw)
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "enabled": bool(raw.get("enabled")),
        "start": raw.get("start"),
        "days": [DAYS[d - 1] for d in sorted(raw.get("days") or []) if isinstance(d, int) and 1 <= d <= 7],
        "method": feats.get("method", "").lower() or None,
        "power": feats.get("power", "").lower() or None,
    }


def find(schedules: list[dict[str, Any]], key: str) -> int | None:
    """Index of a schedule by id or by (unique) name."""
    for i, sch in enumerate(schedules):
        if sch.get("id") == key:
            return i
    by_name = [i for i, sch in enumerate(schedules) if sch.get("name") == key]
    return by_name[0] if len(by_name) == 1 else None


def apply_changes(
    base: dict[str, Any] | None,
    *,
    name: str | None = None,
    start: time | str | None = None,
    days: list[str] | None = None,
    method: str | None = None,
    power: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Return a new (or modified) raw schedule; unknown fields are kept."""
    raw = copy.deepcopy(base) if base else {
        "id": str(uuid.uuid4()),
        "name": "Home Assistant",
        "start": "09:00",
        "days": [1, 2, 3, 4, 5],
        "features": [
            {"name": "method", "value": "SMART_CLEANING"},
            {"name": "power", "value": "NORMAL"},
        ],
        "enabled": True,
    }
    if name is not None:
        raw["name"] = name
    if start is not None:
        raw["start"] = start.strftime("%H:%M") if isinstance(start, time) else str(start)[:5]
    if days is not None:
        raw["days"] = sorted({DAYS.index(d) + 1 for d in days})
    if enabled is not None:
        raw["enabled"] = enabled
    for key, value in (("method", method), ("power", power)):
        if value is not None:
            _set_feature(raw, key, value.upper())
    return raw


def _set_feature(raw: dict[str, Any], name: str, value: str) -> None:
    """Set one feature in place; other features are left untouched."""
    features = raw.get("features")
    if not isinstance(features, list):
        features = raw["features"] = []
    for feat in features:
        if isinstance(feat, dict) and str(feat.get("name", "")).lower() == name:
            feat["value"] = value
            return
    features.append({"name": name, "value": value})
