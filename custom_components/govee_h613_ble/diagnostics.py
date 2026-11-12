"""Diagnostics: what the integration and Home Assistant's Bluetooth see of the strip."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bleak_retry_connector import get_connected_devices
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import GoveeH613ConfigEntry


def _age(stamp: float | None, now: float) -> float | None:
    return None if stamp is None else round(now - stamp, 1)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeH613ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    device = entry.runtime_data
    now = time.monotonic()
    info = bluetooth.async_last_service_info(hass, device.address, connectable=True)

    scanners = []
    for scanner in bluetooth.async_current_scanners(hass):
        allocations = scanner.get_allocations()
        scanners.append(
            {
                "source": scanner.source,
                "name": scanner.name,
                "connectable": scanner.connectable,
                "scanning": scanner.scanning,
                "slots": allocations.slots if allocations else None,
                "free_slots": allocations.free if allocations else None,
                "allocated": allocations.allocated if allocations else None,
                "sees_strip": device.address in scanner.discovered_devices_and_advertisement_data,
            }
        )

    # What BlueZ itself thinks (Linux hosts only): a link it holds while we are
    # not connected is a leftover that blocks new connections.
    bluez_connected: list[str] | None = None
    if info is not None:
        try:
            async with asyncio.timeout(5):
                bluez_connected = [str(d.details) for d in await get_connected_devices(info.device)]
        except Exception as err:  # noqa: BLE001 - diagnostics must not fail
            bluez_connected = [f"error: {type(err).__name__}: {err}"]

    return {
        "entry": {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
        "device": {
            "model": device.model,
            "firmware": device.firmware,
            "keep_connected": device.keep_connected,
            "color_dialect": device.color_dialect.value,
            "brightness_scale": device.brightness_scale,
            "connected": device.is_connected,
            "connected_for_s": _age(device.connected_at, now) if device.is_connected else None,
            "connecting_for_s": _age(device.connecting_since, now),
            "available": device.available,
            "reachable": device.reachable,
            "last_error": device.last_error,
            "stats": dict(device.stats),
            "state": {
                "is_on": device.state.is_on,
                "brightness_level": device.state.brightness_level,
                "rgb": device.state.rgb,
                "kelvin": device.state.kelvin,
                "other_mode": device.state.other_mode,
            },
        },
        "bluetooth": {
            "last_advertisement": None
            if info is None
            else {
                "name": info.name,
                "rssi": info.rssi,
                "source": info.source,
                "connectable": info.connectable,
                "age_s": device.last_advertisement_age,
            },
            "learned_advertising_interval_s": bluetooth.async_get_learned_advertising_interval(
                hass, device.address
            ),
            "bluez_connected_devices": bluez_connected,
            "scanners": scanners,
        },
    }
