"""Config flow for the IPTV proxy: asks for the Xtream login once and verifies it."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientTimeout
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DOMAIN, USER_AGENT

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)


class IptvProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip().rstrip("/")
            if not host.startswith(("http://", "https://")):
                host = "http://" + host
            data = {**user_input, CONF_HOST: host}
            try:
                session = async_get_clientsession(self.hass)
                async with session.get(
                    f"{host}/player_api.php",
                    params={"username": data[CONF_USERNAME], "password": data[CONF_PASSWORD]},
                    headers={"User-Agent": USER_AGENT},
                    timeout=ClientTimeout(total=20),
                ) as resp:
                    info = await resp.json(content_type=None)
                authed = str((info or {}).get("user_info", {}).get("auth")) == "1"
            except (ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                if not authed:
                    errors["base"] = "invalid_auth"
                else:
                    await self.async_set_unique_id(f"{host}|{data[CONF_USERNAME]}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title="IPTV proxy", data=data)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
