"""Config flow, set-up and light entity, on a real Home Assistant with a fake strip."""

from __future__ import annotations

import asyncio
import itertools
import json
from unittest.mock import PropertyMock, patch

from bleak.exc import BleakError
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_h613_ble import protocol
from custom_components.govee_h613_ble.const import (
    CONF_BRIGHTNESS_SCALE,
    CONF_COLOR_DIALECT,
    CONF_KEEP_CONNECTED,
    CONF_MODEL,
    DOMAIN,
    model_from_name,
)
from custom_components.govee_h613_ble.device import GoveeH613Device
from custom_components.govee_h613_ble.diagnostics import async_get_config_entry_diagnostics

from .conftest import ADDRESS, FakeConnector, inject, service_info

ENTITY_ID = "light.govee_h613d_3456"


def reply(cmd: str) -> bytes:
    body = bytes.fromhex(cmd)
    body += bytes(19 - len(body))
    return body + bytes([protocol.checksum(body)])


def entry_device(hass: HomeAssistant):
    """The GoveeH613Device of the only config entry."""
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


async def settle(hass: HomeAssistant) -> None:
    """Let background tasks (the keep-alive loop) run a step."""
    for _ in range(5):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


def test_model_from_name() -> None:
    assert model_from_name("ihoment_H613D_3456") == "H613D"
    assert model_from_name("Govee_H613B_ABCD") == "H613B"
    assert model_from_name("GBK_H613A_0000") == "H613A"
    assert model_from_name("ihoment_H6199_3456") is None
    assert model_from_name(None) is None


# --- config flow -------------------------------------------------------------


@pytest.mark.usefixtures("bluetooth_ready")
async def test_discovery_from_manifest_matcher(hass: HomeAssistant) -> None:
    """An advertisement named ihoment_H613D_* starts our flow on its own."""
    inject(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_BLUETOOTH

    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Govee H613D 3456"
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"}


async def test_discovery_rejects_other_models(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info("ihoment_H6199_3456")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


@pytest.mark.usefixtures("bluetooth_ready")
async def test_user_picks_from_list(hass: HomeAssistant) -> None:
    inject(hass)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "H613D"
    # the discovery flow started by the advertisement is gone too
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.usefixtures("bluetooth_ready")
async def test_user_manual_entry(hass: HomeAssistant) -> None:
    """Nothing advertising (e.g. the Govee app holds the strip): type the MAC."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "manual"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "not a mac", CONF_MODEL: "H613D"}
    )
    assert result["errors"] == {CONF_ADDRESS: "invalid_mac"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: " a4-c1-38-12-34-56 ", CONF_MODEL: "H613D"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"}


async def test_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"}
    ).add_to_hass(hass)
    with patch("custom_components.govee_h613_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- set-up and light --------------------------------------------------------


async def _setup(
    hass: HomeAssistant, keep_connected: bool = True, **options: str
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Govee H613D 3456",
        data={CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"},
        options={CONF_KEEP_CONNECTED: keep_connected, **options},
    )
    entry.add_to_hass(hass)
    inject(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    return entry


@pytest.mark.usefixtures("bluetooth_ready")
async def test_persistent_mode_reads_state(
    hass: HomeAssistant, connector: FakeConnector, device_registry: dr.DeviceRegistry
) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    client = connector.client
    # firmware once, then the three state queries
    assert client.writes[:4] == [protocol.QUERY_FIRMWARE, *protocol.STATE_QUERIES]

    client.reply(reply("aa0101"))  # on
    client.reply(reply("aa0480"))  # 128 on the H613D's 0-255 scale
    client.reply(reply("aa050d00ff00"))  # green
    client.reply(reply("aa06322e30342e3030"))  # firmware 2.04.00
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128
    assert state.attributes["rgb_color"] == (0, 255, 0)
    assert state.attributes["color_mode"] == "rgb"

    device = device_registry.async_get_device(identifiers={(DOMAIN, ADDRESS.lower())})
    assert device.manufacturer == "Govee"
    assert device.model == "H613D"
    assert device.sw_version == "2.04.00"

    # a change made with the control box shows up on the next poll
    client.reply(reply("aa0100"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not client.is_connected


@pytest.mark.usefixtures("bluetooth_ready")
async def test_commands_0x0d_firmware(hass: HomeAssistant, connector: FakeConnector) -> None:
    """H613A/H613B-style firmware: 0x0D colour, 0-100 brightness."""
    await _setup(hass, **{CONF_COLOR_DIALECT: "0x0d", CONF_BRIGHTNESS_SCALE: "100"})
    client = connector.client
    client.writes.clear()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": ENTITY_ID, "rgb_color": [255, 0, 0], "brightness": 128},
        blocking=True,
    )
    # state unknown: power on first, then colour and brightness (0-100 scale)
    assert client.writes == [
        protocol.cmd_power(True),
        protocol.cmd_rgb((255, 0, 0)),
        protocol.cmd_brightness(50),
    ]
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == (255, 0, 0)

    # already on: a colour change is sent alone (re-powering would make the
    # firmware restore its saved colour over the new one)
    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "hs_color": [240, 100]}, blocking=True
    )
    assert client.writes == [protocol.cmd_rgb((0, 0, 255))]

    # a plain "turn on" always sends power on
    client.writes.clear()
    await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY_ID}, blocking=True)
    assert client.writes == [protocol.cmd_power(True)]

    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "color_temp_kelvin": 2700}, blocking=True
    )
    (white,) = client.writes
    assert white[:6] == bytes.fromhex("33050dffffff")
    assert (white[6] << 8 | white[7]) == 2700
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 2700

    client.writes.clear()
    await hass.services.async_call("light", "turn_off", {"entity_id": ENTITY_ID}, blocking=True)
    assert client.writes == [protocol.cmd_power(False)]
    assert hass.states.get(ENTITY_ID).state == STATE_OFF

    # a stale "on" reply already in flight must not flip the state back
    client.reply(reply("aa0101"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


@pytest.mark.usefixtures("bluetooth_ready")
async def test_reconnects_after_a_failed_write(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    await _setup(hass)
    first = connector.client
    first.fail_writes = BleakError("ATT error")

    await hass.services.async_call("light", "turn_off", {"entity_id": ENTITY_ID}, blocking=True)

    assert len(connector.clients) == 2
    assert connector.client.writes[-1] == protocol.cmd_power(False)


@pytest.mark.usefixtures("bluetooth_ready")
async def test_unreachable_raises(hass: HomeAssistant, connector: FakeConnector) -> None:
    await _setup(hass)
    connector.client.fail_writes = BleakError("ATT error")
    connector.fail = BleakError("out of range")

    with pytest.raises(HomeAssistantError, match="Cannot reach"):
        await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY_ID}, blocking=True)


@pytest.mark.usefixtures("bluetooth_ready")
async def test_unavailable_when_link_lost(hass: HomeAssistant, connector: FakeConnector) -> None:
    await _setup(hass)
    assert hass.states.get(ENTITY_ID).state != STATE_UNAVAILABLE

    with patch("custom_components.govee_h613_ble.device._RECONNECT_GRACE", 0):
        connector.fail = BleakError("gone")
        connector.client.drop()
        await hass.async_block_till_done()
        assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    # back in range: the keep-alive loop reconnects on its own
    connector.fail = None
    await entry_device(hass).async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("bluetooth_ready")
async def test_on_demand_mode_disconnects_when_idle(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    entry = await _setup(hass, keep_connected=False)
    client = connector.client  # initial state read
    assert client.writes[1:4] == list(protocol.STATE_QUERIES)

    device = entry.runtime_data
    await device._async_idle_timeout(None)  # what the 30 s timer calls
    assert not client.is_connected
    # still usable: it advertises again and the next command reconnects
    assert hass.states.get(ENTITY_ID).state != STATE_UNAVAILABLE
    await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY_ID}, blocking=True)
    assert len(connector.clients) == 2


@pytest.mark.usefixtures("bluetooth_ready")
async def test_options_switch_mode(hass: HomeAssistant, connector: FakeConnector) -> None:
    entry = await _setup(hass)
    assert entry.runtime_data.keep_connected

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_KEEP_CONNECTED: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.keep_connected


# --- behaviour seen on a real H613D ------------------------------------------

# Notifications from the first field log (Hugo's strip, 2026-11-05).
REAL_ACK_POWER = bytes.fromhex("3301000000000000000000000000000000000032")
REAL_ACK_COLOR = bytes.fromhex("3305000000000000000000000000000000000036")
REAL_BRIGHTNESS = bytes.fromhex("aa04ff0000000000000000000000000000000051")
REAL_COLOR = bytes.fromhex("aa050d00000000000000000000000000000000a2")


@pytest.mark.usefixtures("bluetooth_ready")
async def test_real_h613d_replies(hass: HomeAssistant, connector: FakeConnector) -> None:
    await _setup(hass)
    client = connector.client
    client.reply(reply("aa0101"))
    client.reply(REAL_BRIGHTNESS)
    client.reply(REAL_COLOR)
    client.reply(REAL_ACK_COLOR)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    # 0xFF > 100: the firmware counts 0-255, so this is full brightness
    assert state.attributes["brightness"] == 255
    # "00 00 00" says nothing about what the strip shows: not taken as black
    assert state.attributes["rgb_color"] is None

    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "brightness": 128}, blocking=True
    )
    assert client.writes == [protocol.cmd_brightness(128)]


@pytest.mark.usefixtures("bluetooth_ready")
async def test_h613d_defaults(hass: HomeAssistant, connector: FakeConnector) -> None:
    """What worked on a real H613D: 0x02 colour, 0-255 brightness."""
    entry = await _setup(hass)
    assert entry.runtime_data.color_dialect is protocol.ColorDialect.MODE_02
    assert entry.runtime_data.brightness_scale == 255
    client = connector.client
    client.writes.clear()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": ENTITY_ID, "rgb_color": [255, 0, 0], "brightness": 64},
        blocking=True,
    )
    # the exact frames tested by hand on the strip
    assert client.writes == [
        protocol.cmd_power(True),
        protocol.frame_from_hex("33 05 02 ff 00 00"),
        protocol.frame_from_hex("33 04 40"),
    ]

    # white is rendered as RGB in the 0x02 dialect
    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "color_temp_kelvin": 2700}, blocking=True
    )
    (white,) = client.writes
    assert white[:3] == bytes.fromhex("330502")
    assert hass.states.get(ENTITY_ID).attributes["color_temp_kelvin"] == 2700


@pytest.mark.usefixtures("bluetooth_ready")
async def test_options_override_model_defaults(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    await _setup(hass, **{CONF_COLOR_DIALECT: "0x0d", CONF_BRIGHTNESS_SCALE: "100"})
    client = connector.client
    client.writes.clear()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": ENTITY_ID, "rgb_color": [255, 0, 0], "brightness": 255},
        blocking=True,
    )
    assert client.writes == [
        protocol.cmd_power(True),
        protocol.frame_from_hex("33 05 0d ff 00 00"),
        protocol.cmd_brightness(100),
    ]


async def test_write_pacing(hass: HomeAssistant, connector: FakeConnector, bluetooth_ready) -> None:
    """The real gaps: 200 ms between frames, 400 ms more after powering on."""
    await _setup(hass)
    client = connector.client
    with (
        patch("custom_components.govee_h613_ble.const.WRITE_GAP", 0.2),
        patch("custom_components.govee_h613_ble.const.POWER_ON_SETTLE", 0.4),
    ):
        start = len(client.times)
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": ENTITY_ID, "rgb_color": [1, 2, 3], "brightness": 10},
            blocking=True,
        )
    times = client.times[start:]
    assert len(times) == 3
    assert times[1] - times[0] >= 0.4
    assert times[2] - times[1] >= 0.2


@pytest.mark.usefixtures("bluetooth_ready")
async def test_send_raw(hass: HomeAssistant, connector: FakeConnector) -> None:
    await _setup(hass)
    connector.client.responder = lambda frame: (
        [REAL_COLOR] if frame == protocol.QUERY_COLOR else [REAL_ACK_COLOR]
    )

    response = await hass.services.async_call(
        DOMAIN,
        "send_raw",
        {"entity_id": ENTITY_ID, "frame": "aa 05 01", "listen": 0.2},
        blocking=True,
        return_response=True,
    )
    assert response[ENTITY_ID] == {
        "sent": protocol.QUERY_COLOR.hex(" "),
        "received": [REAL_COLOR.hex(" ")],
    }

    response = await hass.services.async_call(
        DOMAIN,
        "send_raw",
        {"entity_id": ENTITY_ID, "frame": "33:05:02:ff:00:00", "listen": 0.2},
        blocking=True,
        return_response=True,
    )
    assert response[ENTITY_ID]["sent"].startswith("33 05 02 ff 00 00")
    assert response[ENTITY_ID]["received"] == [REAL_ACK_COLOR.hex(" ")]

    with pytest.raises(ServiceValidationError, match="Invalid frame"):
        await hass.services.async_call(
            DOMAIN,
            "send_raw",
            {"entity_id": ENTITY_ID, "frame": "zz"},
            blocking=True,
            return_response=True,
        )


# --- reconnection only while Home Assistant hears the strip -------------------


async def _wait_for(predicate, limit: float = 2.0) -> None:
    """Let background tasks run until ``predicate()`` holds."""
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _presence(value: bool):
    """Pretend Home Assistant's Bluetooth lists the strip as present or gone."""
    return patch.object(GoveeH613Device, "reachable", new_callable=PropertyMock, return_value=value)


@pytest.mark.usefixtures("bluetooth_ready")
async def test_no_attempt_until_the_strip_is_heard(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    """Never heard since start-up (unpowered): no connection attempt at all."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Govee H613D 3456",
        data={CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()

    assert connector.attempts == 0
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    # powered on: the first advertisement brings it in
    inject(hass)
    await _wait_for(lambda: connector.attempts == 1)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("bluetooth_ready")
async def test_strip_gone_means_no_attempts(hass: HomeAssistant, connector: FakeConnector) -> None:
    """Govee app or power cut: once Home Assistant stops listing it, nothing is tried."""
    entry = await _setup(hass)
    device = entry.runtime_data
    with _presence(False):
        connector.client.drop()
        await asyncio.sleep(0.3)
        assert connector.attempts == 1  # only the first, successful connection
        assert not device.is_connected

    # heard again: reconnects right away
    await _wait_for(lambda: device.is_connected)
    assert connector.attempts == 2


@pytest.mark.usefixtures("bluetooth_ready")
async def test_failures_back_off_while_strip_is_heard(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    """Heard but refusing connections: retries spaced out, not hammered."""
    with patch("custom_components.govee_h613_ble.const.RECONNECT_MIN_DELAY", 0.05):
        entry = await _setup(hass)
        device = entry.runtime_data
        connector.fail = BleakError("timeout")
        connector.client.drop()
        await _wait_for(lambda: connector.attempts >= 5)
    gaps = [b - a for a, b in itertools.pairwise(connector.attempt_times[1:])]
    assert all(later > earlier * 1.5 for earlier, later in itertools.pairwise(gaps))

    # it disappears, then comes back: the back-off restarts from the minimum
    with _presence(False):
        before = connector.attempts
        await asyncio.sleep(0.5)
        assert connector.attempts == before
    connector.fail = None
    await _wait_for(lambda: device.is_connected, limit=1.0)


@pytest.mark.usefixtures("bluetooth_ready")
async def test_command_fails_fast_when_strip_is_gone(
    hass: HomeAssistant, connector: FakeConnector
) -> None:
    await _setup(hass, keep_connected=False)
    await entry_device(hass)._async_idle_timeout(None)
    attempts = connector.attempts

    with _presence(False), pytest.raises(HomeAssistantError, match="not heard"):
        await hass.services.async_call("light", "turn_on", {"entity_id": ENTITY_ID}, blocking=True)
    assert connector.attempts == attempts


@pytest.mark.usefixtures("bluetooth_ready")
async def test_diagnostics(hass: HomeAssistant, connector: FakeConnector) -> None:
    entry = await _setup(hass)
    connector.client.reply(reply("aa06322e30342e3030"))
    connector.fail = BleakError("le-connection-abort-by-local")
    with patch("custom_components.govee_h613_ble.const.RECONNECT_MIN_DELAY", 10):
        connector.client.drop()
        await _wait_for(lambda: entry.runtime_data.stats["connect_failures"] == 1)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag)  # what "Download diagnostics" does
    assert diag["device"]["firmware"] == "2.04.00"
    assert diag["device"]["stats"] == {"connects": 1, "connect_failures": 1, "link_losses": 1}
    assert diag["device"]["last_error"] == "BleakError: le-connection-abort-by-local"
    assert diag["device"]["reachable"] is True
    assert diag["bluetooth"]["last_advertisement"]["rssi"] == -60
    assert diag["bluetooth"]["last_advertisement"]["connectable"] is True
