"""Config flow for Rowenta."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    CountrySelector,
    CountrySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    SebAuthError,
    SebCloud,
    SebConnectionError,
    SebError,
    async_get_market,
    async_login,
)
from .const import (
    CONF_COUNTRY,
    CONF_MARKET,
    CONF_PERIMETER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from .options_flow import RowentaOptionsFlow

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
EMAIL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))


class RowentaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in to a Rowenta (Groupe SEB) account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RowentaOptionsFlow()

    async def _async_try_login(
        self, email: str, password: str, country: str
    ) -> tuple[dict[str, Any] | None, list[str], dict[str, str]]:
        """Return (entry data, robot names, errors)."""
        session = async_get_clientsession(self.hass)
        try:
            market = await async_get_market(session, country)
        except SebConnectionError:
            _LOGGER.exception("Error while loading the Rowenta market configuration")
            return None, [], {"base": "cannot_connect"}
        except SebError:
            return None, [], {CONF_COUNTRY: "unsupported_country"}
        try:
            tokens = await async_login(email, password)
            cloud = SebCloud(session, tokens["refresh_token"], market.perimeter)
            appliances = await cloud.async_get_appliances()
        except SebAuthError:
            return None, [], {"base": "invalid_auth"}
        except SebError:
            _LOGGER.exception("Error while logging in to the Rowenta cloud")
            return None, [], {"base": "cannot_connect"}
        if not appliances:
            return None, [], {"base": "no_appliances"}
        if not cloud.user_id:
            return None, [], {"base": "cannot_connect"}
        await self.async_set_unique_id(f"{cloud.user_id}_{market.perimeter}")
        data = {
            CONF_EMAIL: email,
            CONF_COUNTRY: country,
            CONF_MARKET: market.name,
            CONF_PERIMETER: market.perimeter,
            CONF_REFRESH_TOKEN: cloud.refresh_token,
        }
        return data, [a.name for a in appliances], {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Check for duplicates before logging in: every login issues a new refresh
            # token and the server may revoke older ones (including the configured entry's).
            email = user_input[CONF_EMAIL].strip().lower()
            for entry in self._async_current_entries():
                if (
                    str(entry.data.get(CONF_EMAIL, "")).strip().lower() == email
                    and entry.data.get(CONF_COUNTRY) == user_input[CONF_COUNTRY]
                ):
                    return self.async_abort(reason="already_configured")
            data, names, errors = await self._async_try_login(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], user_input[CONF_COUNTRY]
            )
            if data:
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Rowenta ({', '.join(names)})", data=data)

        country_default = {"default": self.hass.config.country} if self.hass.config.country else {}
        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                vol.Required(CONF_COUNTRY, **country_default): CountrySelector(CountrySelectorConfig()),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        email = entry.data.get(CONF_EMAIL, "")
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input.get(CONF_EMAIL, email)
            data, _names, errors = await self._async_try_login(
                email, user_input[CONF_PASSWORD], entry.data[CONF_COUNTRY]
            )
            if data:
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=email): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )
