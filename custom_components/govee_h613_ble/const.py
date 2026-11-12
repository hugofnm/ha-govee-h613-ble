"""Constants for the Govee H613x BLE integration."""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "govee_h613_ble"

CONF_MODEL: Final = "model"
CONF_KEEP_CONNECTED: Final = "keep_connected"
DEFAULT_KEEP_CONNECTED: Final = True
CONF_COLOR_DIALECT: Final = "color_dialect"
CONF_BRIGHTNESS_SCALE: Final = "brightness_scale"  # "auto", "100" or "255"

# The H613x family. Firmwares differ in colour dialect and brightness scale,
# hence the options (CONF_COLOR_DIALECT, CONF_BRIGHTNESS_SCALE).
SUPPORTED_MODELS: Final = ("H613D", "H613B", "H613A")

# (colour dialect, brightness scale) used until the user changes the options.
# H613D: tested on a real strip. H613A/H613B: from the reference projects.
MODEL_DEFAULTS: Final[dict[str, tuple[str, str]]] = {
    "H613D": ("0x02", "255"),
    "H613B": ("0x0d", "auto"),
    "H613A": ("0x0d", "auto"),
}
FALLBACK_DEFAULTS: Final = ("0x0d", "auto")


def model_defaults(model: str) -> tuple[str, str]:
    """Default (colour dialect, brightness scale) for a model."""
    return MODEL_DEFAULTS.get(model, FALLBACK_DEFAULTS)


# Govee advertises "<prefix>_<model>_<last 4 hex of the MAC>", e.g. "ihoment_H613D_1A2B".
_NAME_RE: Final = re.compile(r"^(?:Govee|ihoment|GBK)_(H[0-9A-Z]{4})(?:_|$)", re.IGNORECASE)

# Persistent mode: how often the power state is queried. The queries double as
# the keep-alive the Govee app sends, so the strip does not drop the link.
KEEPALIVE_INTERVAL: Final = 3.0
# Brightness and colour are queried every Nth keep-alive (and after commands).
FULL_REFRESH_EVERY: Final = 4
# The firmware silently drops frames written back-to-back.
WRITE_GAP: Final = 0.2
# After powering on, let the strip restore its state before changing it.
POWER_ON_SETTLE: Final = 0.4
# On-demand mode: disconnect after this long without a command.
IDLE_DISCONNECT_DELAY: Final = 30.0
# Reconnect back-off in persistent mode, while Home Assistant still lists the
# strip as present. It restarts from the minimum when the strip reappears.
RECONNECT_MIN_DELAY: Final = 2.0
RECONNECT_MAX_DELAY: Final = 300.0
# A strip only accepts a connection while it advertises (it stops when another
# central, e.g. the Govee app, holds it, and when it is unpowered). No attempt is
# made while Home Assistant's Bluetooth considers it gone; its presence is
# checked this often (a dictionary lookup, no radio traffic).
PRESENCE_POLL_INTERVAL: Final = 1.0
# Attempts per connection (bleak-retry-connector's default is 4, plus transient
# errors on top).
CONNECT_ATTEMPTS: Final = 3
# Hard limit on one connection (all attempts): it must never hang for good.
CONNECT_TIMEOUT: Final = 90.0
# After a command, ignore state replies for this long so a reply to a query sent
# before the command cannot flip the entity back to the previous state.
COMMAND_GRACE: Final = 0.8


def model_from_name(name: str | None) -> str | None:
    """Return the supported model contained in an advertised name, else None."""
    if not name:
        return None
    if (match := _NAME_RE.match(name)) is None:
        return None
    model = match.group(1).upper()
    return model if model in SUPPORTED_MODELS else None
