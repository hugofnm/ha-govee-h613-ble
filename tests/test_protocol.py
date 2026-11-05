"""Protocol tests against frames captured from real Govee strips.

Pure Python: imports protocol.py directly, no Home Assistant needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "govee_h613_ble" / "protocol.py"
_spec = importlib.util.spec_from_file_location("govee_h613_protocol", _PATH)
protocol = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protocol
_spec.loader.exec_module(protocol)


def h(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.replace(" ", ""))


# --- commands (reference frames from the reverse-engineering write-ups) -----


def test_power_frames() -> None:
    assert protocol.cmd_power(True) == h("3301 01" + "00" * 16 + "33")
    assert protocol.cmd_power(False) == h("3301 00" + "00" * 16 + "32")


def test_brightness_frame_and_clamp() -> None:
    assert protocol.cmd_brightness(100) == h("3304 64" + "00" * 16 + "53")
    # never 0 (the app does not send it), never above 100
    assert protocol.cmd_brightness(0)[2] == 1
    assert protocol.cmd_brightness(250)[2] == 100


def test_rgb_frame_uses_mode_0x0d() -> None:
    frame = protocol.cmd_rgb(255, 0, 0)
    assert frame[:6] == h("33 05 0d ff 00 00")
    assert frame[6:19] == bytes(13)
    assert frame[19] == protocol.checksum(frame[:19])


def test_white_frame_matches_app_capture() -> None:
    # Govee app, 2000 K: 33 05 0d ff ff ff 07 d0 ff 89 12 ...
    frame = protocol.cmd_white(2000, (0xFF, 0x89, 0x12))
    assert frame[:11] == h("33 05 0d ff ff ff 07 d0 ff 89 12")
    # out of range Kelvin is clamped to the app's 2000..9000 K
    assert protocol.cmd_white(12000, (1, 2, 3))[6:8] == h("2328")


@pytest.mark.parametrize("value", [-1, 256])
def test_rgb_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):
        protocol.cmd_rgb(value, 0, 0)


def test_every_frame_is_20_bytes_with_valid_checksum() -> None:
    frames = [
        protocol.cmd_power(True),
        protocol.cmd_brightness(42),
        protocol.cmd_rgb(1, 2, 3),
        protocol.cmd_white(4000, (255, 213, 161)),
        *protocol.STATE_QUERIES,
        protocol.QUERY_FIRMWARE,
    ]
    for frame in frames:
        assert len(frame) == 20
        assert protocol.checksum(frame[:19]) == frame[19]


def test_query_frames() -> None:
    assert h("aa01" + "00" * 17 + "ab") == protocol.QUERY_POWER
    assert h("aa04" + "00" * 17 + "ae") == protocol.QUERY_BRIGHTNESS
    assert h("aa0501" + "00" * 16 + "ae") == protocol.QUERY_COLOR


# --- replies captured on an H613B (same family) -----------------------------


def test_parse_power() -> None:
    assert protocol.parse_reply(h("aa0101" + "00" * 16 + "aa")) == protocol.PowerReply(True)
    assert protocol.parse_reply(h("aa0100" + "00" * 16 + "ab")) == protocol.PowerReply(False)


def test_parse_brightness() -> None:
    reply = protocol.parse_reply(h("aa0436" + "00" * 16 + "98"))
    assert reply == protocol.BrightnessReply(54)


def test_parse_colour_rgb_and_white() -> None:
    red = protocol.parse_reply(h("aa050dff0000" + "00" * 13 + "5d"))
    assert red == protocol.ColorReply(rgb=(255, 0, 0), kelvin=0)

    body = h("aa050dffffff07d0ff8912" + "00" * 8)
    white = protocol.parse_reply(body + bytes([protocol.checksum(body)]))
    assert white == protocol.ColorReply(rgb=(255, 255, 255), kelvin=2000)


def test_parse_other_mode() -> None:
    body = h("aa0505" + "00" * 16)
    assert protocol.parse_reply(body + bytes([protocol.checksum(body)])) == (
        protocol.OtherModeReply(mode=0x05)
    )


def test_parse_firmware() -> None:
    reply = protocol.parse_reply(h("aa06322e30342e3030" + "00" * 10 + "9a"))
    assert reply == protocol.FirmwareReply("2.04.00")


def test_parse_rejects_bad_frames() -> None:
    good = h("aa0101" + "00" * 16 + "aa")
    assert protocol.parse_reply(good[:-1] + b"\x00") is None  # bad checksum
    assert protocol.parse_reply(good[:10]) is None  # too short
    assert protocol.parse_reply(protocol.cmd_power(True)) is None  # command echo


def test_brightness_scale_round_trip() -> None:
    assert protocol.ha_to_percent(255) == 100
    assert protocol.ha_to_percent(1) == 1
    assert protocol.percent_to_ha(100) == 255
    for pct in range(1, 101):
        assert protocol.ha_to_percent(protocol.percent_to_ha(pct)) == pct
