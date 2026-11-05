"""Light entity for a Govee H613x strip."""

from __future__ import annotations

from typing import Any

from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_platform
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import color_temperature_to_rgb
import voluptuous as vol

from . import GoveeH613ConfigEntry
from .const import DOMAIN
from .device import DeviceNotFoundError, GoveeH613Device
from .protocol import KELVIN_MAX, KELVIN_MIN, frame_from_hex

# The device serialises its own writes; strips are independent of each other.
PARALLEL_UPDATES = 0

_BLE_ERRORS = (DeviceNotFoundError, *BLEAK_RETRY_EXCEPTIONS)

SERVICE_SEND_RAW = "send_raw"
ATTR_FRAME = "frame"
ATTR_LISTEN = "listen"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeH613ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the light."""
    async_add_entities([GoveeH613Light(entry.runtime_data)])

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SEND_RAW,
        {
            vol.Required(ATTR_FRAME): cv.string,
            vol.Optional(ATTR_LISTEN, default=1.0): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=5)
            ),
        },
        "async_send_raw",
        supports_response=SupportsResponse.OPTIONAL,
    )


class GoveeH613Light(LightEntity):
    """A Govee H613x RGB strip."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = KELVIN_MIN
    _attr_max_color_temp_kelvin = KELVIN_MAX

    def __init__(self, device: GoveeH613Device) -> None:
        """Initialise."""
        self._device = device
        self._attr_unique_id = dr.format_mac(device.address)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dr.format_mac(device.address))},
            connections={(CONNECTION_BLUETOOTH, device.address)},
            manufacturer="Govee",
            model=device.model,
            model_id=device.model,
            name=device.name,
        )
        self._reported_firmware: str | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the device."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.async_register_callback(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        firmware = self._device.firmware
        if (
            firmware
            and firmware != self._reported_firmware
            and self.registry_entry is not None
            and self.registry_entry.device_id is not None
        ):
            dr.async_get(self.hass).async_update_device(
                self.registry_entry.device_id, sw_version=firmware
            )
            self._reported_firmware = firmware
        self.async_write_ha_state()

    # --- state ------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True if the strip can be reached."""
        return self._device.available

    @property
    def is_on(self) -> bool | None:
        """Return True if the strip is on."""
        return self._device.state.is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness, 0..255."""
        return self._device.brightness

    @property
    def color_mode(self) -> ColorMode:
        """Return the active colour mode."""
        return ColorMode.COLOR_TEMP if self._device.state.kelvin else ColorMode.RGB

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the RGB colour."""
        return self._device.state.rgb

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the white temperature."""
        return self._device.state.kelvin

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the raw Govee mode while a scene or music mode runs."""
        if (mode := self._device.state.other_mode) is None:
            return None
        return {"govee_mode": f"0x{mode:02X}"}

    # --- commands ---------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, with optional brightness / colour / white."""
        rgb: tuple[int, int, int] | None = kwargs.get(ATTR_RGB_COLOR)
        kelvin: int | None = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        white_rgb = None
        if rgb is None and kelvin is not None:
            r, g, b = color_temperature_to_rgb(kelvin)
            white_rgb = (round(r), round(g), round(b))

        try:
            await self._device.async_turn_on(
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                rgb=tuple(rgb) if rgb is not None else None,
                kelvin=kelvin,
                white_rgb=white_rgb,
            )
        except _BLE_ERRORS as err:
            raise self._error(err) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        try:
            await self._device.async_turn_off()
        except _BLE_ERRORS as err:
            raise self._error(err) from err

    async def async_send_raw(self, frame: str, listen: float) -> ServiceResponse:
        """Developer tool: send one raw frame, return what the strip answers."""
        try:
            data = frame_from_hex(frame)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_frame",
                translation_placeholders={"frame": frame, "error": str(err)},
            ) from err
        try:
            received = await self._device.async_send_raw(data, listen)
        except _BLE_ERRORS as err:
            raise self._error(err) from err
        return {"sent": data.hex(" "), "received": [bytes.fromhex(h).hex(" ") for h in received]}

    def _error(self, err: Exception) -> HomeAssistantError:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_reach",
            translation_placeholders={
                "name": self._device.name,
                "error": str(err) or type(err).__name__,
            },
        )
