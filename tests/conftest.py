"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rowenta.api import Appliance, Market
from custom_components.rowenta.const import (
    CONF_COUNTRY,
    CONF_MARKET,
    CONF_PERIMETER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

APPLIANCE = Appliance(
    id="00000000-1111-2222-3333-444444444444",
    thing_type="VAC001",
    name="X-Plorer Serie 60",
    appliance_model="APPLIANCE_1092",
)
REDIRECT = (
    "Failed to launch 'gsmoduser://tokens#access_token=AT&refresh_token=RT%3D%3D"
    "&id=https%3A%2F%2Faccount.groupeseb.com%2Fid%2F00DORG%2F005USER&id_token=IDT&state=STATE'"
    " because the scheme does not have a registered handler."
)
ENTRY_DATA = {
    CONF_REFRESH_TOKEN: "RT==",
    CONF_PERIMETER: "ROWENTA_FR",
    CONF_MARKET: "GS_FR",
    CONF_COUNTRY: "FR",
}
STATUS = {
    "cleaningArea": 12,
    "endTimestamp": -1,
    "status": "CLEANING",
    "method": "SMART_CLEANING",
    "errorCode": "NORMAL",
    "accessories": [
        {"current": 14, "limit": 100, "name": "SIDE-BRUSH", "alert": False},
        {"current": 20, "limit": 100, "name": "MAIN-BRUSH", "alert": False},
        {"current": 50, "limit": 200, "name": "FILTER", "alert": True},
    ],
    "totalCleaningTime": None,
    "power": "STRONG",
    "startTimestamp": 1794681184738,
    "battery": 87,
    "currentTimestamp": 1794753099951,
    "mapId": 1794656519936,
    "cleaningTime": 9,
}
SHADOW = {
    "state": {
        "reported": {
            "status": {"connected": True, "busy": False, "updating": False},
            "firmware": {"version": "VAC001_WIFI_2.7.1"},
            "schedules": [
                {
                    "start": "09:00",
                    "features": [
                        {"value": "SMART_CLEANING", "name": "method"},
                        {"value": "NORMAL", "name": "power"},
                    ],
                    "days": [3, 1, 6],
                    "id": "x",
                    "name": "Weekdays",
                    "enabled": True,
                }
            ],
        }
    }
}
HISTORY = {"totalCleaningArea": 4920, "totalCleaningDuration": 8356, "totalNumberOfClean": 296}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_cloud() -> Generator[MagicMock]:
    """SebCloud without network access."""
    with (
        patch("custom_components.rowenta.SebCloud", autospec=True) as cls,
        patch("custom_components.rowenta.config_flow.SebCloud", new=cls),
    ):
        cloud = cls.return_value
        cloud.async_get_appliances = AsyncMock(return_value=[APPLIANCE])
        cloud.async_get_gyro_map = AsyncMock(return_value={"mapId": 0, "map": []})
        cloud.refresh_token = "RT=="
        cloud.user_id = "005USER"
        yield cloud


@pytest.fixture
def mock_market() -> Generator[AsyncMock]:
    with patch(
        "custom_components.rowenta.config_flow.async_get_market",
        AsyncMock(return_value=Market("GS_FR", "ROWENTA_FR")),
    ) as m:
        yield m


class FakeMqtt:
    """SebMqtt stand-in: records commands, messages are injected manually."""

    instances: list[FakeMqtt] = []

    def __init__(self, hass, cloud, appliances, on_message, on_connection, on_auth_failed):
        self.appliances = appliances
        self.on_message = on_message
        self.on_connection = on_connection
        self.on_auth_failed = on_auth_failed
        self.connected = False
        self.commands: list[dict] = []
        self.shadow_updates: list[dict] = []
        self.status_requests = 0
        FakeMqtt.instances.append(self)

    @property
    def can_publish(self):
        return self.connected

    def request_shadow(self, appl):
        self.shadow_requests = getattr(self, "shadow_requests", 0) + 1

    async def async_start(self):
        self.connected = True
        self.on_connection(True)

    async def async_stop(self):
        self.connected = False

    def request_status(self, appl, listening=False):
        self.status_requests += 1

    def request_history(self, appl):
        pass

    def update_shadow(self, appl, desired):
        self.shadow_updates.append(desired)

    def send_command(self, appl, command):
        self.commands.append(command)

    def feed(self, kind, payload, appl=APPLIANCE):
        self.on_message(appl, kind, payload)


@pytest.fixture
def fake_mqtt() -> Generator[type[FakeMqtt]]:
    FakeMqtt.instances = []
    with patch("custom_components.rowenta.SebMqtt", FakeMqtt):
        yield FakeMqtt


@pytest.fixture
def config_entry():
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN, data=dict(ENTRY_DATA), unique_id="005USER_ROWENTA_FR", title="Rowenta"
    )
