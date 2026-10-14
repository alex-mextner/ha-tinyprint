"""Bluetooth discovery, manual setup, and font options."""

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import callback

from .const import DOMAIN, NAME
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
    def __init__(self, entry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "font_path",
                        default=self._entry.options.get(
                            "font_path", self._entry.data.get("font_path", "")
                        ),
                    ): str,
                }
            ),
        )
