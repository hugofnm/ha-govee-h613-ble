"""Config flow: Bluetooth discovery, pick from a list, or enter a MAC by hand."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .const import (
    CONF_KEEP_CONNECTED,
    CONF_MODEL,
    DEFAULT_KEEP_CONNECTED,
    DOMAIN,
    SUPPORTED_MODELS,
    model_from_name,
)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_MANUAL = "manual"


def _title(model: str, address: str) -> str:
    """'Govee H613D 1A2B', like the advertised name."""
    return f"Govee {model} {address.replace(':', '')[-4:]}"


class GoveeH613ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, tuple[str, str]] = {}  # address -> (model, name)

    @staticmethod
    @callback
    def async_get_options_flow(_entry: Any) -> OptionsFlow:
        """Return the options flow."""
        return GoveeH613OptionsFlow()

    # --- automatic discovery ----------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """A matching strip was seen by Home Assistant's Bluetooth."""
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        if model_from_name(discovery_info.name) is None:
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered strip."""
        assert self._discovery is not None
        info = self._discovery
        model = model_from_name(info.name) or SUPPORTED_MODELS[0]
        if user_input is not None:
            return self._create(info.address, model)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": info.name, "address": info.address},
        )

    # --- user initiated ---------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a strip among the ones Home Assistant currently sees."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == _MANUAL:
                return await self.async_step_manual()
            await self.async_set_unique_id(address.upper(), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            model, _name = self._discovered[address]
            return self._create(address, model)

        configured = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            model = model_from_name(info.name)
            if model is None or info.address.upper() in configured:
                continue
            self._discovered[info.address] = (model, info.name)

        if not self._discovered:
            return await self.async_step_manual()

        options = [
            SelectOptionDict(value=address, label=f"{name} ({address})")
            for address, (_model, name) in self._discovered.items()
        ]
        options.append(SelectOptionDict(value=_MANUAL, label=_MANUAL))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                            translation_key="device",
                        )
                    )
                }
            ),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter the MAC address (e.g. while the strip is connected to the app)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper().replace("-", ":")
            if not _MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self._create(address, user_input[CONF_MODEL])

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(CONF_MODEL, default=SUPPORTED_MODELS[0]): vol.In(
                        list(SUPPORTED_MODELS)
                    ),
                }
            ),
            errors=errors,
        )

    @callback
    def _create(self, address: str, model: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_title(model, address),
            data={CONF_ADDRESS: address.upper(), CONF_MODEL: model},
        )


class GoveeH613OptionsFlow(OptionsFlow):
    """Connection mode."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_KEEP_CONNECTED, default=current): bool}),
        )
