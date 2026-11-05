"""Config flow, set-up and light entity, on a real Home Assistant with a fake strip."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from bleak.exc import BleakError
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_h613_ble import protocol
from custom_components.govee_h613_ble.const import (
    CONF_KEEP_CONNECTED,
    CONF_MODEL,
    DOMAIN,
    model_from_name,
)

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


async def _setup(hass: HomeAssistant, keep_connected: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Govee H613D 3456",
        data={CONF_ADDRESS: ADDRESS, CONF_MODEL: "H613D"},
        options={CONF_KEEP_CONNECTED: keep_connected},
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
    client.reply(reply("aa0436"))  # 54 %
    client.reply(reply("aa050d00ff00"))  # green
    client.reply(reply("aa06322e30342e3030"))  # firmware 2.04.00
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 138
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
async def test_commands(hass: HomeAssistant, connector: FakeConnector) -> None:
    await _setup(hass)
    client = connector.client
    client.writes.clear()

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": ENTITY_ID, "rgb_color": [255, 0, 0], "brightness": 128},
        blocking=True,
    )
    assert client.writes == [
        protocol.cmd_power(True),
        protocol.cmd_rgb(255, 0, 0),
        protocol.cmd_brightness(50),
    ]
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["rgb_color"] == (255, 0, 0)

    # hs colour from the UI colour wheel arrives as RGB
    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "hs_color": [240, 100]}, blocking=True
    )
    assert client.writes[1] == protocol.cmd_rgb(0, 0, 255)

    client.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": ENTITY_ID, "color_temp_kelvin": 2700}, blocking=True
    )
    white = client.writes[1]
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
