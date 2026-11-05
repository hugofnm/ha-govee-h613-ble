"""Shared fixtures: a mocked Bluetooth stack and a fake Govee strip."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
import time
from typing import Any
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_get_advertisement_callback,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

# Import this repository's custom_components before Home Assistant puts its own
# testing config folder (with an empty custom_components package) on sys.path.
import custom_components.govee_h613_ble  # noqa: F401

ADDRESS = "A4:C1:38:12:34:56"
NAME = "ihoment_H613D_3456"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ from this repository."""


@pytest.fixture(autouse=True)
def auto_mock_bluetooth(mock_bluetooth: None) -> None:
    """Never touch a real Bluetooth adapter."""


@pytest.fixture(autouse=True)
def fast_writes() -> Generator[None]:
    """No write pacing in tests (test_write_pacing checks the real values)."""
    with (
        patch("custom_components.govee_h613_ble.const.WRITE_GAP", 0.0),
        patch("custom_components.govee_h613_ble.const.POWER_ON_SETTLE", 0.0),
    ):
        yield


@pytest.fixture
async def bluetooth_ready(hass: HomeAssistant) -> None:
    """Set up Home Assistant's Bluetooth integration."""
    assert await async_setup_component(hass, "bluetooth", {})
    await hass.async_block_till_done()


def service_info(name: str = NAME, address: str = ADDRESS) -> BluetoothServiceInfoBleak:
    """What Home Assistant sees when the strip advertises."""
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, None),
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def inject(hass: HomeAssistant, info: BluetoothServiceInfoBleak | None = None) -> None:
    """Feed an advertisement to Home Assistant's Bluetooth manager."""
    async_get_advertisement_callback(hass)(info or service_info())


class FakeClient:
    """Stands in for BleakClientWithServiceCache connected to a strip."""

    def __init__(self, disconnected_callback: Callable[[Any], None] | None) -> None:
        self.is_connected = True
        self.writes: list[bytes] = []
        self.times: list[float] = []
        self.responder: Callable[[bytes], list[bytes]] | None = None
        self.notify: Callable[[Any, bytearray], None] | None = None
        self.fail_writes: Exception | None = None
        self._disconnected_callback = disconnected_callback

    async def start_notify(self, _uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        self.notify = callback

    async def stop_notify(self, _uuid: str) -> None:
        pass

    async def write_gatt_char(self, _uuid: str, data: bytes, response: bool = False) -> None:
        if self.fail_writes is not None:
            raise self.fail_writes
        self.writes.append(bytes(data))
        self.times.append(time.monotonic())
        if self.responder is not None and self.notify is not None:
            for frame in self.responder(bytes(data)):
                asyncio.get_running_loop().call_soon(self.notify, None, bytearray(frame))

    async def disconnect(self) -> None:
        self.is_connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)

    def drop(self) -> None:
        """Simulate the strip going away."""
        self.is_connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)

    def reply(self, frame: bytes) -> None:
        """Simulate a notification from the strip."""
        assert self.notify is not None
        self.notify(None, bytearray(frame))


class FakeConnector:
    """Patches establish_connection and records every client it hands out."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.fail: Exception | None = None

    async def __call__(
        self, _cls: Any, _device: Any, _name: str, *, disconnected_callback=None, **_kw: Any
    ) -> FakeClient:
        if self.fail is not None:
            raise self.fail
        client = FakeClient(disconnected_callback)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakeClient:
        return self.clients[-1]


@pytest.fixture
def connector() -> Generator[FakeConnector]:
    """A fake strip reachable over BLE."""
    fake = FakeConnector()
    with patch("custom_components.govee_h613_ble.device.establish_connection", fake):
        yield fake
