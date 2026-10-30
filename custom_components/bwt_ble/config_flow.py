from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import CONF_ADDRESS, DOMAIN


class BwtBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            address = address.upper()
            if self._async_address_exists(address):
                errors["base"] = "already_configured"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=address, data={CONF_ADDRESS: address})

        data_schema = vol.Schema({vol.Required(CONF_ADDRESS): str})
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    def _async_address_exists(self, address: str) -> bool:
        return any(entry.data.get(CONF_ADDRESS) == address for entry in self._async_current_entries())


class BwtBleOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.config_entry = entry

    async def async_step_init(self, user_input=None):
        return self.async_create_entry(title="", data={})


@callback
def async_get_options_flow(entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
    return BwtBleOptionsFlowHandler(entry)
