"""Govee H613x LED strips over Bluetooth Low Energy."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .const import (
    CONF_BRIGHTNESS_SCALE,
    CONF_COLOR_DIALECT,
    CONF_KEEP_CONNECTED,
    CONF_MODEL,
    DEFAULT_KEEP_CONNECTED,
    DOMAIN,
    model_defaults,
)
from .device import GoveeH613Device

PLATFORMS: list[Platform] = [Platform.LIGHT]

type GoveeH613ConfigEntry = ConfigEntry[GoveeH613Device]


async def async_setup_entry(hass: HomeAssistant, entry: GoveeH613ConfigEntry) -> bool:
    """Set up a strip from a config entry."""
    model = entry.data[CONF_MODEL]
    default_dialect, default_scale = model_defaults(model)
    device = GoveeH613Device(
        hass,
        address=entry.data[CONF_ADDRESS],
        name=entry.title,
        model=model,
        keep_connected=entry.options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED),
        color_dialect=entry.options.get(CONF_COLOR_DIALECT, default_dialect),
        brightness_scale=entry.options.get(CONF_BRIGHTNESS_SCALE, default_scale),
    )
    entry.runtime_data = device

    entry.async_on_unload(device.async_start_tracking())

    async def _on_stop(_event: Event) -> None:
        await device.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))
    options = dict(entry.options)

    async def _async_entry_updated(hass: HomeAssistant, updated: GoveeH613ConfigEntry) -> None:
        """Reload on option changes only.

        Home Assistant also updates the entry on its own (Bluetooth discovery
        keys) and on renames: neither needs the link to be torn down.
        """
        device.name = updated.title
        if dict(updated.options) != options:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Connecting can take several seconds and the strip may be unpowered:
    # never block the setup on it.
    if device.keep_connected:
        entry.async_create_background_task(
            hass, device.async_keepalive_loop(), f"{DOMAIN} keep-alive {device.address}"
        )
    else:
        entry.async_create_background_task(
            hass, _async_initial_refresh(device), f"{DOMAIN} refresh {device.address}"
        )
    return True


async def _async_initial_refresh(device: GoveeH613Device) -> None:
    """On-demand mode: read the state once at start-up, best effort."""
    try:
        await device.async_refresh()
    except Exception:  # noqa: BLE001 - the strip may simply be off or out of range
        return


async def async_unload_entry(hass: HomeAssistant, entry: GoveeH613ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
