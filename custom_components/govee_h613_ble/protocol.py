"""Govee H613x BLE wire protocol.

Pure Python, no Home Assistant import, so it can be unit-tested on its own.

Every frame is 20 bytes:

    byte 0       head: 0x33 = command, 0xAA = state query / state reply
    byte 1       command: 0x01 power, 0x04 brightness, 0x05 colour, 0x06 firmware
    bytes 2..18  payload, zero padded
    byte 19      XOR of bytes 0..18

Commands (written to characteristic ...2b11, write without response):

    33 01 01                    power on
    33 01 00                    power off
    33 04 <1..100>              brightness in percent (the app never sends 0)
    33 05 0D R G B 00 00        manual colour, RGB
    33 05 0D FF FF FF KH KL WR WG WB
                                white: Kelvin big-endian (KH KL), then the RGB
                                the strip should render for that white

Queries (same characteristic) and their replies (notified on ...2b10):

    AA 01        -> AA 01 <00|01>                 power state
    AA 04        -> AA 04 <0..100>                brightness in percent
    AA 05 01     -> AA 05 0D R G B KH KL ...      colour (Kelvin 0 = RGB mode)
    AA 06        -> AA 06 <ASCII firmware>        e.g. "2.04.00"

Sources: Beshelmek/govee_ble_lights (frame + checksum), hardcpp/GoveeBleMqttServer
(mode 0x0D and 0-100 brightness for H613A/H613D), gilcu3/govee-btled-H613B
(captured 0xAA replies on the sister model H613B).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

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


MODE_MANUAL = 0x0D  # colour mode used by the H613x family


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


def _byte(value: int, name: str) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} out of range: {value}")
    return value


# --- commands -------------------------------------------------------------


def cmd_power(on: bool) -> bytes:
    """Power on/off."""
    return build_frame(Head.COMMAND, Cmd.POWER, [0x01 if on else 0x00])


def cmd_brightness(percent: int) -> bytes:
    """Brightness 1..100 (the strip ignores anything above 100)."""
    percent = max(1, min(100, int(percent)))
    return build_frame(Head.COMMAND, Cmd.BRIGHTNESS, [percent])


def cmd_rgb(red: int, green: int, blue: int) -> bytes:
    """Static RGB colour."""
    rgb = [_byte(red, "red"), _byte(green, "green"), _byte(blue, "blue")]
    return build_frame(Head.COMMAND, Cmd.COLOR, [MODE_MANUAL, *rgb])


def cmd_white(kelvin: int, white_rgb: tuple[int, int, int]) -> bytes:
    """White at a colour temperature.

    The H613x is an RGB-only strip: it renders ``white_rgb``, the Kelvin value is
    what the app reads back to place its slider.
    """
    kelvin = max(KELVIN_MIN, min(KELVIN_MAX, int(kelvin)))
    wr, wg, wb = (_byte(c, "white") for c in white_rgb)
    return build_frame(
        Head.COMMAND,
        Cmd.COLOR,
        [MODE_MANUAL, 0xFF, 0xFF, 0xFF, kelvin >> 8, kelvin & 0xFF, wr, wg, wb],
    )


# --- queries --------------------------------------------------------------

QUERY_POWER = build_frame(Head.QUERY, Cmd.POWER)
QUERY_BRIGHTNESS = build_frame(Head.QUERY, Cmd.BRIGHTNESS)
QUERY_COLOR = build_frame(Head.QUERY, Cmd.COLOR, [0x01])
QUERY_FIRMWARE = build_frame(Head.QUERY, Cmd.FIRMWARE)

STATE_QUERIES: tuple[bytes, ...] = (QUERY_POWER, QUERY_BRIGHTNESS, QUERY_COLOR)


# --- replies --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerReply:
    """Reply to AA 01."""

    is_on: bool


@dataclass(frozen=True, slots=True)
class BrightnessReply:
    """Reply to AA 04."""

    percent: int


@dataclass(frozen=True, slots=True)
class ColorReply:
    """Reply to AA 05 01 while the strip is in manual colour mode."""

    rgb: tuple[int, int, int]
    kelvin: int  # 0 when the strip shows an RGB colour


@dataclass(frozen=True, slots=True)
class OtherModeReply:
    """Reply to AA 05 01 while a scene, music or DIY mode is active."""

    mode: int


@dataclass(frozen=True, slots=True)
class FirmwareReply:
    """Reply to AA 06."""

    version: str


Reply = PowerReply | BrightnessReply | ColorReply | OtherModeReply | FirmwareReply


def parse_reply(data: bytes) -> Reply | None:
    """Decode a notification, or return None if it is not a known state reply.

    The strip also echoes 0x33 command frames; those are ignored here.
    """
    if len(data) != FRAME_LEN or data[0] != Head.QUERY:
        return None
    if checksum(data[:-1]) != data[-1]:
        return None

    cmd = data[1]
    if cmd == Cmd.POWER:
        return PowerReply(is_on=data[2] == 0x01)
    if cmd == Cmd.BRIGHTNESS:
        return BrightnessReply(percent=min(100, data[2]))
    if cmd == Cmd.COLOR:
        if data[2] != MODE_MANUAL:
            return OtherModeReply(mode=data[2])
        kelvin = (data[6] << 8) | data[7]
        return ColorReply(rgb=(data[3], data[4], data[5]), kelvin=kelvin)
    if cmd == Cmd.FIRMWARE:
        raw = data[2:-1].split(b"\x00", 1)[0]
        try:
            version = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return FirmwareReply(version=version) if version else None
    return None


# --- brightness scale helpers ---------------------------------------------


def ha_to_percent(brightness: int) -> int:
    """Home Assistant 1..255 -> strip 1..100."""
    return max(1, min(100, round(brightness * 100 / 255)))


def percent_to_ha(percent: int) -> int:
    """Strip 0..100 -> Home Assistant 0..255."""
    return max(0, min(255, round(percent * 255 / 100)))
