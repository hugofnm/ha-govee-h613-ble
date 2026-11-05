"""BLE link to one Govee H613x strip.

Two connection modes:

* persistent (default): one connection held open; the power state is queried
  every few seconds (brightness and colour less often), which is also the
  keep-alive the strip expects. Commands land instantly and changes made with
  the control box or the remote show up in Home Assistant.
* on demand: connect for a command, disconnect after a short idle delay. Frees
  the adapter slot and lets the Govee app connect, but the state is only read
  back when Home Assistant talks to the strip.

Every write goes through one paced queue: the firmware silently drops frames
that arrive back-to-back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass
import logging
import time
from typing import Any

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from . import const, protocol

_LOGGER = logging.getLogger(__name__)

# Persistent mode: stay "available" this long after the last successful
# exchange, so a quick reconnect does not make the entity flap.
_RECONNECT_GRACE = 20.0

# A write item is a frame, or a pause in seconds before the next frame.
WriteItem = bytes | float


class DeviceNotFoundError(Exception):
    """No connectable Bluetooth adapter or proxy currently sees the strip."""


@dataclass(slots=True)
class GoveeState:
    """Last known state of the strip."""

    is_on: bool | None = None
    brightness_level: int | None = None  # raw firmware level
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = None  # set while the strip shows white
    other_mode: int | None = None  # scene / music / DIY mode byte, if any


class GoveeH613Device:
    """Connection manager and state holder for one strip."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        model: str,
        *,
        keep_connected: bool = const.DEFAULT_KEEP_CONNECTED,
        color_dialect: str = const.DEFAULT_COLOR_DIALECT,
        brightness_scale: str = const.DEFAULT_BRIGHTNESS_SCALE,
    ) -> None:
        """Initialise."""
        self.hass = hass
        self.address = address
        self.name = name
        self.model = model
        self.keep_connected = keep_connected
        self.color_dialect = protocol.ColorDialect(color_dialect)
        self._scale_setting = brightness_scale
        self._detected_scale: int | None = None
        self.state = GoveeState()
        self.firmware: str | None = None

        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._callbacks: list[Callable[[], None]] = []
        self._cancel_idle: CALLBACK_TYPE | None = None
        self._last_write = 0.0
        self._last_command = 0.0
        self._last_ok: float | None = None
        self._refresh_pending = True
        self._poll_paused_until = 0.0
        self._probes: list[list[str]] = []
        self._present = False
        self._logged_unavailable = False
        self._stopping = False

    # --- public state -----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return True while a BLE connection is open."""
        return self._client is not None and self._client.is_connected

    @property
    def available(self) -> bool:
        """Return True if commands are expected to reach the strip."""
        if self.is_connected:
            return True
        if self.keep_connected:
            return self._last_ok is not None and time.monotonic() - self._last_ok < _RECONNECT_GRACE
        return self._present

    @property
    def brightness_scale(self) -> int:
        """Top of the firmware's brightness range: 100 or 255."""
        if self._scale_setting in ("100", "255"):
            return int(self._scale_setting)
        return self._detected_scale or 100

    @property
    def brightness(self) -> int | None:
        """Brightness on Home Assistant's 0..255 scale."""
        if (level := self.state.brightness_level) is None:
            return None
        return protocol.level_to_ha(level, self.brightness_scale)

    @callback
    def async_register_callback(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``cb`` whenever the state or the availability changes."""
        self._callbacks.append(cb)

        @callback
        def _remove() -> None:
            self._callbacks.remove(cb)

        return _remove

    @callback
    def _async_fire(self) -> None:
        for cb in list(self._callbacks):
            cb()

    # --- lifecycle --------------------------------------------------------

    @callback
    def async_start_tracking(self) -> CALLBACK_TYPE:
        """Follow the strip's advertisements (used for on-demand availability)."""
        self._present = bluetooth.async_address_present(self.hass, self.address, connectable=True)

        @callback
        def _on_advertisement(
            _info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange,
        ) -> None:
            if not self._present:
                self._present = True
                self._async_fire()

        @callback
        def _on_unavailable(_info: bluetooth.BluetoothServiceInfoBleak) -> None:
            self._present = False
            self._async_fire()

        cancel_adv = bluetooth.async_register_callback(
            self.hass,
            _on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        cancel_unavail = bluetooth.async_track_unavailable(
            self.hass, _on_unavailable, self.address, connectable=True
        )

        @callback
        def _cancel() -> None:
            cancel_adv()
            cancel_unavail()

        return _cancel

    async def async_keepalive_loop(self) -> None:
        """Persistent mode: hold the link open and poll the state forever."""
        delay = const.RECONNECT_MIN_DELAY
        cycle = 0
        while not self._stopping:
            if time.monotonic() < self._poll_paused_until:
                await asyncio.sleep(0.2)
                continue

            full = cycle % const.FULL_REFRESH_EVERY == 0 or (
                self._refresh_pending
                and time.monotonic() - self._last_command > const.COMMAND_GRACE
            )
            queries = protocol.STATE_QUERIES if full else (protocol.QUERY_POWER,)
            try:
                await self._async_write(queries, command=False)
            except Exception as err:
                if not self._logged_unavailable:
                    _LOGGER.warning(
                        "%s: cannot reach the strip (%s), retrying in the background",
                        self.name,
                        err,
                    )
                    self._logged_unavailable = True
                _LOGGER.debug("%s: keep-alive failed", self.name, exc_info=True)
                await self._async_disconnect()
                self._async_fire()
                await asyncio.sleep(delay)
                delay = min(delay * 2, const.RECONNECT_MAX_DELAY)
                continue

            if full:
                self._refresh_pending = False
            if self._logged_unavailable:
                _LOGGER.info("%s: connection restored", self.name)
                self._logged_unavailable = False
            delay = const.RECONNECT_MIN_DELAY
            cycle += 1
            await asyncio.sleep(const.KEEPALIVE_INTERVAL)

    async def async_refresh(self) -> None:
        """Read the state once (connects if needed)."""
        await self._async_write(protocol.STATE_QUERIES, command=False)

    async def async_stop(self) -> None:
        """Disconnect for good."""
        self._stopping = True
        await self._async_disconnect()

    # --- commands ---------------------------------------------------------

    async def async_turn_on(
        self,
        *,
        brightness: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        kelvin: int | None = None,
        white_rgb: tuple[int, int, int] | None = None,
    ) -> None:
        """Turn on, optionally changing colour / white / brightness (0..255)."""
        white = kelvin is not None and white_rgb is not None
        changes = rgb is not None or white or brightness is not None

        items: list[WriteItem] = []
        # A plain "turn on" always sends power on. When only colour or brightness
        # change on a strip known to be on, skip it: re-powering makes the
        # firmware restore its saved state over the new one.
        if self.state.is_on is not True or not changes:
            items.append(protocol.cmd_power(True))
            if changes:
                items.append(const.POWER_ON_SETTLE)
        if rgb is not None:
            items.append(protocol.cmd_rgb(rgb, self.color_dialect))
        elif white:
            items.append(protocol.cmd_white(kelvin, white_rgb, self.color_dialect))
        if brightness is not None:
            level = protocol.ha_to_level(brightness, self.brightness_scale)
            items.append(protocol.cmd_brightness(level))

        await self._async_write(items, command=True)

        self.state.is_on = True
        if rgb is not None:
            self.state.rgb, self.state.kelvin, self.state.other_mode = rgb, None, None
        elif white:
            self.state.rgb, self.state.kelvin, self.state.other_mode = white_rgb, kelvin, None
        if brightness is not None:
            self.state.brightness_level = protocol.ha_to_level(brightness, self.brightness_scale)
        self._async_fire()

    async def async_turn_off(self) -> None:
        """Turn off."""
        await self._async_write([protocol.cmd_power(False)], command=True)
        self.state.is_on = False
        self._async_fire()

    async def async_send_raw(self, frame: bytes, listen: float) -> list[str]:
        """Send one raw frame and return every notification seen for ``listen`` s.

        Polling is paused meanwhile so the replies belong to this frame.
        """
        received: list[str] = []
        self._probes.append(received)
        self._poll_paused_until = time.monotonic() + listen + 5
        try:
            await self._async_write([frame], command=True)
            await asyncio.sleep(listen)
        finally:
            self._probes.remove(received)
            self._poll_paused_until = 0.0
        return received

    # --- connection -------------------------------------------------------

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_ensure_connected(self) -> BleakClientWithServiceCache:
        if (client := self._client) is not None and client.is_connected:
            self._async_reset_idle_timer()
            return client

        async with self._connect_lock:
            if (client := self._client) is not None and client.is_connected:
                self._async_reset_idle_timer()
                return client

            if (ble_device := self._ble_device()) is None:
                raise DeviceNotFoundError(
                    f"{self.address} is not seen by any connectable Bluetooth adapter"
                )

            _LOGGER.debug("%s: connecting", self.name)
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.name,
                disconnected_callback=self._on_disconnected,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device() or ble_device,
            )
            try:
                await client.start_notify(protocol.NOTIFY_CHAR_UUID, self._on_notification)
                # Ask what the strip is showing right away; the firmware once.
                first = [] if self.firmware else [protocol.QUERY_FIRMWARE]
                async with self._write_lock:
                    await self._async_send_locked(client, [*first, *protocol.STATE_QUERIES])
            except BLEAK_RETRY_EXCEPTIONS:
                await client.disconnect()
                raise

            _LOGGER.debug("%s: connected", self.name)
            self._client = client
            self._last_ok = time.monotonic()

        self._async_reset_idle_timer()
        self._async_fire()
        return client

    async def _async_send_locked(
        self, client: BleakClientWithServiceCache, items: Iterable[WriteItem]
    ) -> None:
        """Write frames with at least WRITE_GAP between them. Hold _write_lock."""
        for item in items:
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            if (wait := self._last_write + const.WRITE_GAP - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            _LOGGER.debug("%s: -> %s", self.name, item.hex())
            await client.write_gatt_char(protocol.WRITE_CHAR_UUID, item, response=False)
            self._last_write = time.monotonic()

    async def _async_write(self, items: Iterable[WriteItem], *, command: bool) -> None:
        """Write frames, reconnecting and retrying once on a BLE error."""
        items = list(items)
        for attempt in (1, 2):
            client = await self._async_ensure_connected()
            try:
                async with self._write_lock:
                    if command:
                        self._last_command = time.monotonic()
                    await self._async_send_locked(client, items)
            except BLEAK_RETRY_EXCEPTIONS:
                _LOGGER.debug("%s: write failed (attempt %s)", self.name, attempt)
                await self._async_disconnect()
                if attempt == 2:
                    raise
                continue
            self._last_ok = time.monotonic()
            if command:
                self._last_command = self._last_ok
                self._refresh_pending = True
            return

    async def _async_disconnect(self) -> None:
        self._async_cancel_idle_timer()
        async with self._connect_lock:
            client, self._client = self._client, None
            if client is None or not client.is_connected:
                return
            _LOGGER.debug("%s: disconnecting", self.name)
            with contextlib.suppress(*BLEAK_RETRY_EXCEPTIONS):
                await client.stop_notify(protocol.NOTIFY_CHAR_UUID)
            await client.disconnect()

    @callback
    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if self._client is not None and client is not self._client:
            return  # an old client we already replaced
        if self._client is not None:
            _LOGGER.debug("%s: disconnected unexpectedly", self.name)
        self._client = None
        self._async_cancel_idle_timer()
        self._async_fire()

    # --- on-demand idle timer ---------------------------------------------

    @callback
    def _async_reset_idle_timer(self) -> None:
        if self.keep_connected:
            return
        self._async_cancel_idle_timer()
        self._cancel_idle = async_call_later(
            self.hass, const.IDLE_DISCONNECT_DELAY, self._async_idle_timeout
        )

    @callback
    def _async_cancel_idle_timer(self) -> None:
        if self._cancel_idle is not None:
            self._cancel_idle()
            self._cancel_idle = None

    async def _async_idle_timeout(self, _now: Any) -> None:
        self._cancel_idle = None
        _LOGGER.debug("%s: idle, disconnecting", self.name)
        await self._async_disconnect()
        self._async_fire()

    # --- notifications ----------------------------------------------------

    @callback
    def _on_notification(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = bytes(data)
        _LOGGER.debug("%s: <- %s", self.name, raw.hex())
        for probe in self._probes:
            probe.append(raw.hex())

        reply = protocol.parse_reply(raw)
        if reply is None or isinstance(reply, protocol.AckReply):
            return

        if isinstance(reply, protocol.FirmwareReply):
            self.firmware = reply.version
            self._async_fire()
            return

        if (
            isinstance(reply, protocol.BrightnessReply)
            and self._scale_setting == "auto"
            and reply.level > 100
            and self._detected_scale != 255
        ):
            _LOGGER.info(
                "%s: brightness reported as %s, using a 0-255 scale", self.name, reply.level
            )
            self._detected_scale = 255

        # A reply to a query sent before a command describes the old state;
        # keep what we just commanded for a moment.
        if time.monotonic() - self._last_command < const.COMMAND_GRACE:
            return

        state = self.state
        before = (state.is_on, state.brightness_level, state.rgb, state.kelvin, state.other_mode)
        match reply:
            case protocol.PowerReply(is_on=is_on):
                state.is_on = is_on
            case protocol.BrightnessReply(level=level):
                state.brightness_level = level
            case protocol.ColorReply(rgb=rgb, kelvin=kelvin):
                # Some firmwares report 00 00 00 when the colour was set in
                # another dialect: that says nothing about what is shown.
                if rgb != (0, 0, 0) or kelvin:
                    state.rgb = rgb
                    state.kelvin = kelvin or None
                    state.other_mode = None
            case protocol.OtherModeReply(mode=mode):
                state.other_mode = mode
        after = (state.is_on, state.brightness_level, state.rgb, state.kelvin, state.other_mode)
        if after != before:
            self._async_fire()
