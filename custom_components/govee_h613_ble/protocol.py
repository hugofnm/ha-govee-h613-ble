"""Govee H613x BLE wire protocol.

Pure Python, no Home Assistant import, so it can be unit-tested on its own.

Every frame is 20 bytes:

    byte 0       head: 0x33 = command (or its ack), 0xAA = state query / reply
    byte 1       command: 0x01 power, 0x04 brightness, 0x05 colour, 0x06 firmware
    bytes 2..18  payload, zero padded
    byte 19      XOR of bytes 0..18

Commands (written to characteristic ...2b11, write without response). The strip
acknowledges each one with ``33 <cmd> 00``; the ack only means "received", a
frame the firmware does not understand is acked and then ignored.

    33 01 01 / 33 01 00          power on / off
    33 04 <level>                brightness, 0..100 or 0..255 depending on firmware

Colour, two dialects depending on the firmware:

    0x02  33 05 02 R G B                        H613D (tested), legacy RGB strips
    0x0D  33 05 0D R G B [KH KL WR WG WB]       H613A/H613B, newer firmwares

    For white, 0x0D carries the Kelvin (KH KL) and the RGB to render (WR WG WB);
    0x02 only gets the RGB. A firmware acks the other dialect's frame and ignores
    it. Never send the segmented RGBIC frame (33 05 15 ...) to an H613D: it turns
    the LEDs black until a valid colour frame is sent.

Queries and replies (notified on ...2b10):

    AA 01        -> AA 01 <00|01>                 power
    AA 04        -> AA 04 <level>                 brightness (raw level; H613D: 0..255)
    AA 05 01     -> AA 05 <mode> R G B [KH KL]    colour
    AA 06        -> AA 06 <ASCII firmware>        e.g. "2.04.00"

The firmware ignores frames written back-to-back: keep about 200 ms between
writes.

Sources: Beshelmek/govee_ble_lights (frame, checksum, 0x02, write pacing),
hardcpp/GoveeBleMqttServer (0x0D for H613A), gilcu3/govee-btled-H613B (0xAA
replies), and tests on a real H613D (0x02 colour, 0..255 brightness).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

FRAME_LEN = 20
PAYLOAD_LEN = FRAME_LEN - 3  # head + command + checksum

WRITE_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

KELVIN_MIN = 2000
KELVIN_MAX = 9000


class Head(IntEnum):
    """First byte of a frame."""

    COMMAND = 0x33
    QUERY = 0xAA


class Cmd(IntEnum):
    """Second byte of a frame."""

    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05
    FIRMWARE = 0x06


class ColorDialect(StrEnum):
    """How a firmware expects the colour command."""

    MODE_0D = "0x0d"
    MODE_02 = "0x02"


MODE_0D = 0x0D
MODE_02 = 0x02


def checksum(data: bytes) -> int:
    """XOR of every byte."""
    value = 0
    for byte in data:
        value ^= byte
    return value


def build_frame(head: int, cmd: int, payload: bytes | list[int] = b"") -> bytes:
    """Return a padded 20-byte frame with its checksum."""
    payload = bytes(payload)
    if len(payload) > PAYLOAD_LEN:
        raise ValueError(f"payload too long ({len(payload)} > {PAYLOAD_LEN})")
    body = bytes([head & 0xFF, cmd & 0xFF]) + payload
    body += bytes(FRAME_LEN - 1 - len(body))
    return body + bytes([checksum(body)])


def frame_from_hex(text: str) -> bytes:
    """Parse user-typed hex ("33 05 02 ff 00 00").

    Up to 19 bytes: padded and checksummed. Exactly 20 bytes: sent as typed.
    """
    raw = bytes.fromhex("".join(text.replace(":", " ").replace(",", " ").split()))
    if len(raw) == FRAME_LEN:
        return raw
    if not 2 <= len(raw) < FRAME_LEN:
        raise ValueError(f"expected 2 to 20 bytes, got {len(raw)}")
    return build_frame(raw[0], raw[1], raw[2:])


def _byte(value: int, name: str) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} out of range: {value}")
    return value


def _rgb(rgb: tuple[int, int, int]) -> list[int]:
    return [_byte(rgb[0], "red"), _byte(rgb[1], "green"), _byte(rgb[2], "blue")]


# --- commands -------------------------------------------------------------


def cmd_power(on: bool) -> bytes:
    """Power on/off."""
    return build_frame(Head.COMMAND, Cmd.POWER, [0x01 if on else 0x00])


def cmd_brightness(level: int) -> bytes:
    """Brightness as the firmware's raw level (never 0: that is what power off is for)."""
    return build_frame(Head.COMMAND, Cmd.BRIGHTNESS, [max(1, min(255, int(level)))])


def cmd_rgb(rgb: tuple[int, int, int], dialect: ColorDialect = ColorDialect.MODE_0D) -> bytes:
    """Static RGB colour."""
    r, g, b = _rgb(rgb)
    mode = MODE_02 if dialect is ColorDialect.MODE_02 else MODE_0D
    payload = [mode, r, g, b]
    return build_frame(Head.COMMAND, Cmd.COLOR, payload)


def cmd_white(
    kelvin: int,
    white_rgb: tuple[int, int, int],
    dialect: ColorDialect = ColorDialect.MODE_0D,
) -> bytes:
    """White at a colour temperature (rendered with RGB on these strips)."""
    kelvin = max(KELVIN_MIN, min(KELVIN_MAX, int(kelvin)))
    wr, wg, wb = _rgb(white_rgb)
    kh, kl = kelvin >> 8, kelvin & 0xFF
    if dialect is ColorDialect.MODE_02:
        return cmd_rgb(white_rgb, dialect)
    payload = [MODE_0D, 0xFF, 0xFF, 0xFF, kh, kl, wr, wg, wb]
    return build_frame(Head.COMMAND, Cmd.COLOR, payload)


# --- queries --------------------------------------------------------------

QUERY_POWER = build_frame(Head.QUERY, Cmd.POWER)
QUERY_BRIGHTNESS = build_frame(Head.QUERY, Cmd.BRIGHTNESS)
QUERY_COLOR = build_frame(Head.QUERY, Cmd.COLOR, [0x01])
QUERY_FIRMWARE = build_frame(Head.QUERY, Cmd.FIRMWARE)

STATE_QUERIES: tuple[bytes, ...] = (QUERY_POWER, QUERY_BRIGHTNESS, QUERY_COLOR)


# --- replies --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AckReply:
    """``33 <cmd> 00``: the strip received a command (not that it applied it)."""

    cmd: int


@dataclass(frozen=True, slots=True)
class PowerReply:
    """Reply to AA 01."""

    is_on: bool


@dataclass(frozen=True, slots=True)
class BrightnessReply:
    """Reply to AA 04, raw level (0..100 or 0..255 depending on firmware)."""

    level: int


@dataclass(frozen=True, slots=True)
class ColorReply:
    """Reply to AA 05 01 in a manual colour mode (0x0D or 0x02)."""

    mode: int
    rgb: tuple[int, int, int]
    kelvin: int  # 0 when the strip shows an RGB colour


@dataclass(frozen=True, slots=True)
class OtherModeReply:
    """Reply to AA 05 01 while a scene, music, DIY or segment mode is active."""

    mode: int


@dataclass(frozen=True, slots=True)
class FirmwareReply:
    """Reply to AA 06."""

    version: str


Reply = AckReply | PowerReply | BrightnessReply | ColorReply | OtherModeReply | FirmwareReply


def parse_reply(data: bytes) -> Reply | None:
    """Decode a notification, or return None if it is not a known frame."""
    if len(data) != FRAME_LEN or checksum(data[:-1]) != data[-1]:
        return None
    if data[0] == Head.COMMAND:
        return AckReply(cmd=data[1])
    if data[0] != Head.QUERY:
        return None

    cmd = data[1]
    if cmd == Cmd.POWER:
        return PowerReply(is_on=data[2] == 0x01)
    if cmd == Cmd.BRIGHTNESS:
        return BrightnessReply(level=data[2])
    if cmd == Cmd.COLOR:
        mode = data[2]
        if mode == MODE_0D:
            kelvin = (data[6] << 8) | data[7]
            return ColorReply(mode=mode, rgb=(data[3], data[4], data[5]), kelvin=kelvin)
        if mode == MODE_02:
            return ColorReply(mode=mode, rgb=(data[3], data[4], data[5]), kelvin=0)
        return OtherModeReply(mode=mode)
    if cmd == Cmd.FIRMWARE:
        raw = data[2:-1].split(b"\x00", 1)[0]
        try:
            version = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return FirmwareReply(version=version) if version else None
    return None


# --- brightness scale helpers ---------------------------------------------


def ha_to_level(brightness: int, scale: int) -> int:
    """Home Assistant 1..255 -> firmware 1..scale."""
    return max(1, min(scale, round(brightness * scale / 255)))


def level_to_ha(level: int, scale: int) -> int:
    """Firmware 0..scale -> Home Assistant 0..255."""
    return max(0, min(255, round(min(level, scale) * 255 / scale)))
