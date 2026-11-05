"""Constants for the Govee H613x BLE integration."""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "govee_h613_ble"

CONF_MODEL: Final = "model"
CONF_KEEP_CONNECTED: Final = "keep_connected"
DEFAULT_KEEP_CONNECTED: Final = True

# Models that share the 0x0D colour mode and 0-100 brightness.
# H613D: target of this integration. H613B: protocol captured by gilcu3.
# H613A: listed with the same mode by hardcpp/GoveeBleMqttServer.
SUPPORTED_MODELS: Final = ("H613D", "H613B", "H613A")

# Govee advertises "<prefix>_<model>_<last 4 hex of the MAC>", e.g. "ihoment_H613D_1A2B".
_NAME_RE: Final = re.compile(r"^(?:Govee|ihoment|GBK)_(H[0-9A-Z]{4})(?:_|$)", re.IGNORECASE)

# Persistent mode: how often the state is queried. The queries double as the
# keep-alive the Govee app sends, so the strip does not drop the link.
KEEPALIVE_INTERVAL: Final = 3.0
# On-demand mode: disconnect after this long without a command.
IDLE_DISCONNECT_DELAY: Final = 30.0
# Reconnect back-off in persistent mode.
RECONNECT_MIN_DELAY: Final = 2.0
RECONNECT_MAX_DELAY: Final = 60.0
# After a command, ignore state replies for this long so a reply already in
# flight cannot flip the entity back to the previous state.
COMMAND_GRACE: Final = 1.5


def model_from_name(name: str | None) -> str | None:
    """Return the supported model contained in an advertised name, else None."""
    if not name:
        return None
    if (match := _NAME_RE.match(name)) is None:
        return None
    model = match.group(1).upper()
    return model if model in SUPPORTED_MODELS else None
