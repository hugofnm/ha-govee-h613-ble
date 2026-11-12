# Govee H613x BLE for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/hugofnm/ha-govee-h613-ble/actions/workflows/validate.yml/badge.svg)](https://github.com/hugofnm/ha-govee-h613-ble/actions/workflows/validate.yml)

Local control of **Govee H613D** RGB LED strips (and the sister models H613A / H613B)
over **Bluetooth Low Energy**, straight from Home Assistant. No cloud, no Govee account,
no MQTT bridge.

## Features

- **Auto-discovery**: Home Assistant offers the strip as soon as it is in Bluetooth range
  (advertised as `ihoment_H613D_XXXX`, `Govee_H613D_XXXX` or `GBK_H613D_XXXX`), or add it
  from a list, or type its MAC address.
- **Light** entity: on/off, brightness, RGB colour, white (2000 – 9000 K).
- **Persistent link** (default): commands land instantly and changes made with the
  control box, the IR remote or the app are read back every 3 s.
- **On-demand mode** (option): connects only to send a command, disconnects after 30 s.
- Firmware version shown on the device page, English and French UI.
- Works with the host's adapter (e.g. the Raspberry Pi 5's built-in Bluetooth) and with
  **ESPHome Bluetooth proxies** set to `active: true`.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/hugofnm/ha-govee-h613-ble`,
   category **Integration**.
2. Download **Govee H613x BLE**, then restart Home Assistant.

### Manual

Copy `custom_components/govee_h613_ble` into `<config>/custom_components/` and restart.

Requires Home Assistant 2025.3 or newer with the Bluetooth integration working.

## Setup

1. **Close the Govee Home app** (or turn off the phone's Bluetooth): the strip accepts a
   single BLE connection and stops advertising while the app holds it.
2. Settings → Devices & services: accept the discovered **Govee H613D** card, or
   **Add integration → Govee H613x BLE**.

### Options

| Option | Default | Effect |
| --- | --- | --- |
| Keep the Bluetooth connection open | on | Instant commands and live state; uses one connection slot of the adapter/proxy permanently, and the Govee app cannot connect meanwhile. Turn off to connect only when sending a command. |
| Colour protocol | H613D: `0x02` · H613A/B: `0x0D` | Colour frame the firmware understands. If colours do nothing, try the other one (or probe with **Send raw frame** below). |
| Brightness scale | H613D: `0-255` · H613A/B: auto | `0-100` or `0-255`. Auto switches to 0-255 as soon as the strip reports a level above 100. |

### Finding the right frames: Send raw frame

Developer tools → Actions → **Govee H613x BLE: Send raw frame**, pick the light, tick
*return response*:

```yaml
action: govee_h613_ble.send_raw
target:
  entity_id: light.govee_h613d_aa36
data:
  frame: "33 05 02 ff 00 00"   # checksum added for you
```

The response lists what the strip answered (`33 05 00` is only an ack: the firmware
acks frames it then ignores, so watch the strip itself).

> Do not send the segmented RGBIC frame (`33 05 15 …`) to an H613x: the LEDs go black and
> stay black, even after power off/on, until a valid colour frame is sent.

## Supported models

| Model | Status | Notes |
| --- | --- | --- |
| H613D | Tested on a real strip | colour `33 05 02`, brightness 0–255 |
| H613B | Protocol captured by gilcu3 on real hardware | colour `33 05 0D`, brightness 0–100 |
| H613A | Reported by hardcpp | colour `33 05 0D`, brightness 0–100 |

Another firmware revision may use the other dialect: switch it in the options.

## Protocol cheat sheet

20-byte frames, last byte = XOR of the 19 others, written without response to
`00010203-0405-0607-0809-0a0b0c0d2b11`; replies are notified on `…2b10`.

| Purpose | Frame |
| --- | --- |
| Power on / off | `33 01 01` / `33 01 00` |
| Brightness | `33 04 <level>` (0–100 or 0–255 depending on firmware) |
| RGB | `33 05 02 RR GG BB` (H613D) · `33 05 0D RR GG BB` (H613A/B) |
| White | `33 05 0D FF FF FF <K hi> <K lo> WR WG WB`; in `0x02`, the white's RGB |
| Query power / brightness / colour / firmware | `AA 01` / `AA 04` / `AA 05 01` / `AA 06` |

Every command is acked with `33 <cmd> 00`, even one the firmware ignores. Frames written
back-to-back are dropped: the integration keeps 200 ms between writes. The full
description lives in [`protocol.py`](custom_components/govee_h613_ble/protocol.py).

## Troubleshooting

Enable debug logs to see every frame sent (`->`) and received (`<-`):

```yaml
logger:
  logs:
    custom_components.govee_h613_ble: debug
```

- *Not discovered*: check that the Govee app is closed and that the strip appears under
  Settings → Bluetooth → Advertisement monitor.
- *"not heard by any Bluetooth adapter"*: Home Assistant's Bluetooth no longer lists the
  strip (unpowered, out of range, or held by the Govee app: it then stops advertising). The
  integration waits without touching the radio and reconnects as soon as it is heard again.
- *"cannot reach the strip"* while the strip is heard: connection attempts fail. They are
  retried with a back-off of up to 5 minutes. **Download diagnostics** (device page → ⋮)
  shows the last error, the RSSI, the free connection slots of each adapter and the links
  BlueZ itself holds. A link left in BlueZ by a previous Home Assistant process blocks new
  connections; since 0.1.2 it is closed automatically before each connection. By hand, on
  the host: `bluetoothctl disconnect <MAC>`.
- *Weak signal*: below about −85 dBm the link is unreliable. Move the strip or the host,
  prefer Ethernet over Wi-Fi on a Raspberry Pi (Wi-Fi and Bluetooth share one chip), or add
  an [ESPHome Bluetooth proxy](https://esphome.io/projects/?type=bluetooth) near it.
- *"Failed to cancel connection … Bad file descriptor"* from `bleak.backends.bluezdbus`:
  BlueZ logs it after a failed connection attempt. Before 0.1.1 the integration retried
  blindly and could produce thousands of them.

## Limitations

- No scenes, music mode, DIY effects or segments (the strip still reports when one is
  active, as the `govee_mode` attribute).
- In on-demand mode, state changes made outside Home Assistant are only seen at the next
  connection.

## Brand images

Home Assistant 2026.3+ shows images placed in `custom_components/govee_h613_ble/brand/`
(`icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, optional `dark_` variants). This
repository ships none: Govee's logo is Govee's. To use the official Govee images that Home
Assistant's brands repository already serves for its Govee integrations:

```bash
cd custom_components/govee_h613_ble && mkdir -p brand
for f in icon icon@2x logo logo@2x; do
  curl -fsSL -o "brand/$f.png" \
    "https://raw.githubusercontent.com/home-assistant/brands/master/core_brands/govee/$f.png"
done
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest            # protocol frames + config flow + light on a real HA core
ruff check . && ruff format --check .
```

The tests run a real Home Assistant core with a fake strip; they never touch a radio.

## Credits

Protocol knowledge from [Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights)
(frame format, checksum, command bytes), [hardcpp/GoveeBleMqttServer](https://github.com/hardcpp/GoveeBleMqttServer)
(mode `0x0D` and 0–100 brightness for H613A/H613D) and
[gilcu3/govee-btled-H613B](https://github.com/gilcu3/govee-btled-H613B) (captured `0xAA`
state replies). No code was copied.

Not affiliated with or endorsed by Govee. MIT licence.
