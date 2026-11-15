"""Configure the monitored line and polling period."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN, INTERVALS, LINES, MODE_UNAVAILABLE, POWER_MODES
from .coordinator import fetch


async def settings_step(flow, step, defaults, user_input):
    errors = {}
    if user_input is not None:
        current_id = flow.config_entry.entry_id if step == "init" else None
        for entry in flow.hass.config_entries.async_entries(DOMAIN):
            settings = {**entry.data, **entry.options}
            if entry.entry_id != current_id and settings.get("line") == user_input["line"]:
                return flow.async_abort(reason="already_configured")
        try:
            await fetch(flow.hass, user_input["line"])
        except UpdateFailed:
            errors["base"] = "cannot_connect"
        else:
            if step == "init":
                flow.hass.config_entries.async_update_entry(
                    flow.config_entry, title=f"ESCO · {user_input['line']}"
                )
            # power_entity is stored explicitly (None when cleared) so that an options value
            # always overrides the one saved in the entry data.
            return flow.async_create_entry(
                title=f"ESCO · {user_input['line']}" if step == "user" else "",
                data={**user_input, "interval": int(user_input["interval"]),
                      "power_entity": user_input.get("power_entity") or None,
                      "power_mode": user_input.get("power_mode") or MODE_UNAVAILABLE})
    selected = user_input or defaults
    schema = vol.Schema({
        vol.Required("line", default=selected.get("line", LINES[0])): vol.In(LINES),
        vol.Required("interval", default=str(selected.get("interval", 30))):
            vol.In([str(value) for value in INTERVALS]),
        vol.Optional("power_entity", description={"suggested_value": selected.get("power_entity")}):
            selector.EntitySelector(),
        vol.Required("power_mode", default=selected.get("power_mode") or MODE_UNAVAILABLE):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=list(POWER_MODES), mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="power_mode")),
    })
    # Use a dropdown in the UI; store the selected number as an integer.
    return flow.async_show_form(step_id=step, data_schema=schema, errors=errors)


class EscoFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        return await settings_step(self, "user", {}, user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EscoOptions()


class EscoOptions(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return await settings_step(self, "init", defaults, user_input)
