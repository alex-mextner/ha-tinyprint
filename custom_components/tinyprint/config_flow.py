"""Bluetooth discovery, manual setup, and font options."""

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .bridge_client import BridgeClient, BridgeError, BridgeSettings, normalize_bridge_url
from .bridge_wire import check_token_strength
from .const import (
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_TRANSPORT,
    DOMAIN,
    NAME,
    TRANSPORT_BRIDGE,
    TRANSPORT_NATIVE,
)
from .protocol import ADV_UUID


def normalize_address(address: str) -> str:
    address = address.strip().upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        raise ValueError("Invalid Bluetooth MAC address")
    return address


class TinyPrintConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TinyPrintOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                address = normalize_address(user_input["address"])
            except ValueError:
                errors["address"] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get("name") or NAME,
                    data={"address": address, "font_path": user_input.get("font_path", "")},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("address"): str,
                    vol.Optional("name", default=NAME): str,
                    vol.Optional("font_path", default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any]):
        try:
            normalize_address(user_input["address"])
        except KeyError, ValueError:
            return self.async_abort(reason="invalid_address")
        return await self.async_step_user(user_input)

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        name = discovery_info.name or ""
        if (
            not name.startswith(("X6h-", "x6h-"))
            or ADV_UUID not in discovery_info.service_uuids
            or not discovery_info.connectable
        ):
            return self.async_abort(reason="not_supported")
        self._address = normalize_address(discovery_info.address)
        self._name = name
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title=self._name, data={"address": self._address, "font_path": ""}
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._name},
            data_schema=vol.Schema({}),
        )


class TinyPrintOptionsFlow(config_entries.OptionsFlow):
    """Admin-only settings. The bridge URL and token exist nowhere else (not in data, services
    or documents), and the token is never shown back in the form."""

    def __init__(self, entry) -> None:
        self._entry = entry

    def _schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_TRANSPORT, default=current[CONF_TRANSPORT]): SelectSelector(
                    SelectSelectorConfig(
                        options=[TRANSPORT_NATIVE, TRANSPORT_BRIDGE],
                        translation_key=CONF_TRANSPORT,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_BRIDGE_URL, description={"suggested_value": current[CONF_BRIDGE_URL]}
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Optional(CONF_BRIDGE_TOKEN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
                ),
                vol.Optional("font_path", default=current["font_path"]): str,
            }
        )

    async def _validated_bridge(self, user_input: dict[str, Any], errors: dict[str, str]):
        stored = self._entry.options
        url = None
        try:
            url = normalize_bridge_url(user_input.get(CONF_BRIDGE_URL))
        except ValueError:
            errors[CONF_BRIDGE_URL] = "invalid_url"
            return None
        token = user_input.get(CONF_BRIDGE_TOKEN)
        if not token:
            # A stored token is only reused for the same bridge: never send it to a new address.
            try:
                same = normalize_bridge_url(stored.get(CONF_BRIDGE_URL)) == url
            except ValueError:
                same = False
            token = stored.get(CONF_BRIDGE_TOKEN) if same else None
        if not token:
            errors[CONF_BRIDGE_TOKEN] = "token_required"
            return None
        try:
            check_token_strength(token)
        except ValueError:
            errors[CONF_BRIDGE_TOKEN] = "invalid_token"
            return None
        try:
            await BridgeClient(
                async_get_clientsession(self.hass), BridgeSettings(url, token)
            ).status()
        except BridgeError as error:
            errors["base"] = "invalid_auth" if error.code == "unauthorized" else "cannot_connect"
            return None
        return url, token

    async def async_step_init(self, user_input=None):
        options = self._entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_TRANSPORT: user_input.get(CONF_TRANSPORT, TRANSPORT_NATIVE),
                "font_path": user_input.get("font_path", ""),
            }
            if data[CONF_TRANSPORT] == TRANSPORT_BRIDGE:
                if checked := await self._validated_bridge(user_input, errors):
                    data[CONF_BRIDGE_URL], data[CONF_BRIDGE_TOKEN] = checked
            if not errors:
                # Choosing native drops the bridge fields so no stale secret stays stored.
                return self.async_create_entry(title="", data=data)
        source = user_input if user_input is not None else options
        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(
                {
                    CONF_TRANSPORT: source.get(CONF_TRANSPORT, TRANSPORT_NATIVE),
                    CONF_BRIDGE_URL: source.get(CONF_BRIDGE_URL, ""),
                    "font_path": source.get(
                        "font_path", options.get("font_path", self._entry.data.get("font_path", ""))
                    ),
                }
            ),
            errors=errors,
        )
