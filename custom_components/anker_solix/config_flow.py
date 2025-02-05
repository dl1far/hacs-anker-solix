"""Adds config flow for Anker Solix."""

from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_COUNTRY_CODE,
    CONF_DELAY_TIME,
    CONF_EXCLUDE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import api_client
from .const import (
    ACCEPT_TERMS,
    ALLOW_TESTMODE,
    DOMAIN,
    ERROR_DETAIL,
    EXAMPLESFOLDER,
    INTERVALMULT,
    LOGGER,
    SHARED_ACCOUNT,
    TC_LINK,
    TERMS_LINK,
    TESTFOLDER,
    TESTMODE,
)
from .solixapi.api import ApiCategories, SolixDeviceType

# Define integration option limits and defaults
SCAN_INTERVAL_DEF = api_client.DEFAULT_UPDATE_INTERVAL
INTERVALMULT_DEF = api_client.DEFAULT_DEVICE_MULTIPLIER  # multiplier for scan interval
DELAY_TIME_DEF = api_client.api.SolixDefaults.REQUEST_DELAY_DEF

_SCAN_INTERVAL_MIN = 10 if ALLOW_TESTMODE else 30
_SCAN_INTERVAL_MAX = 600
_SCAN_INTERVAL_STEP = 10
_INTERVALMULT_MIN = 2
_INTERVALMULT_MAX = 60
_INTERVALMULT_STEP = 2
_DELAY_TIME_MIN = 0.0
_DELAY_TIME_MAX = 2.0
_DELAY_TIME_STEP = 0.1
_ALLOW_TESTMODE = bool(ALLOW_TESTMODE)


class AnkerSolixFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Anker Solix."""

    VERSION = 1

    def __init__(self) -> None:
        """Init the FlowHandler."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self.client: api_client.AnkerSolixApiClient = None
        self.testmode: bool = False
        self.testfolder: str = None
        self.examplesfolder: str = os.path.join(
            os.path.dirname(__file__), EXAMPLESFOLDER
        )
        # ensure folder for example json folders exists
        os.makedirs(self.examplesfolder, exist_ok=True)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AnkerSolixOptionsFlowHandler:
        """Get the options flow for this handler."""
        return AnkerSolixOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        cfg_schema = {
            vol.Required(
                CONF_USERNAME,
                default=(user_input or {}).get(CONF_USERNAME),
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.EMAIL, autocomplete="username"
                )
            ),
            vol.Required(
                CONF_PASSWORD,
                default=(user_input or {}).get(CONF_PASSWORD),
            ): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                ),
            ),
            vol.Required(
                CONF_COUNTRY_CODE,
                default=(user_input or {}).get(CONF_COUNTRY_CODE)
                or self.hass.config.country,
            ): selector.CountrySelector(
                selector.CountrySelectorConfig(),
            ),
            vol.Required(
                ACCEPT_TERMS,
                default=(user_input or {}).get(ACCEPT_TERMS, False),
            ): selector.BooleanSelector(),
        }
        placeholders[TERMS_LINK] = TC_LINK

        if user_input:
            if not user_input.get(ACCEPT_TERMS, ""):
                # Terms not accepted
                errors[ACCEPT_TERMS] = ACCEPT_TERMS
            else:
                account_user = user_input.get(CONF_USERNAME, "")
                try:
                    if await self.async_set_unique_id(account_user.lower()):
                        # abort if username already setup
                        self._abort_if_unique_id_configured()
                    else:
                        self.client = await self._authenticate_client(user_input)

                    # get first site data for account and verify nothing is shared with existing configuration
                    await self.client.api.update_sites()
                    if cfg_entry := await async_check_and_remove_devices(
                        self.hass,
                        user_input,
                        self.client.api.sites | self.client.api.devices,
                    ):
                        errors[CONF_USERNAME] = "duplicate_devices"
                        placeholders[CONF_USERNAME] = str(account_user)
                        placeholders[SHARED_ACCOUNT] = str(cfg_entry.title)
                    else:
                        self._data = user_input
                        # add some fixed configuration data
                        self._data[EXAMPLESFOLDER] = self.examplesfolder

                        # set initial options for the config entry
                        # options = {
                        #     CONF_SCAN_INTERVAL: SCAN_INTERVAL_DEF,
                        #     INTERVALMULT: INTERVALMULT_DEF,
                        #     CONF_DELAY_TIME: DELAY_TIME_DEF,
                        #     CONF_EXCLUDE: list(api_client.DEFAULT_EXCLUDE_CATEGORIES)
                        # }

                        # next step to configure initial options
                        return await self.async_step_user_options(user_options=None)

                except api_client.AnkerSolixApiClientAuthenticationError as exception:
                    LOGGER.warning(exception)
                    errors["base"] = "auth"
                    placeholders[ERROR_DETAIL] = str(exception)
                except api_client.AnkerSolixApiClientCommunicationError as exception:
                    LOGGER.error(exception)
                    errors["base"] = "connection"
                    placeholders[ERROR_DETAIL] = str(exception)
                except api_client.AnkerSolixApiClientRetryExceededError as exception:
                    LOGGER.exception(exception)
                    errors["base"] = "exceeded"
                    placeholders[ERROR_DETAIL] = str(exception)
                except (api_client.AnkerSolixApiClientError, Exception) as exception:  # pylint: disable=broad-except
                    LOGGER.exception(exception)
                    errors["base"] = "unknown"
                    placeholders[ERROR_DETAIL] = (
                        f"Exception {type(exception)}: {exception}"
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(cfg_schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_user_options(
        self, user_options: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_options:
            self._options = user_options
            if self._options.get(TESTFOLDER) or not self._options.get(TESTMODE):
                return self.async_create_entry(
                    title=self.client.api.nickname
                    if self.client and self.client.api
                    else self._data.get(CONF_USERNAME),
                    data=self._data,
                    options=self._options,
                )
            # Test mode enabled but no existing folder selected
            errors[TESTFOLDER] = "folder_invalid"

        return self.async_show_form(
            step_id="user_options",
            data_schema=vol.Schema(get_options_schema(user_options or self._options)),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _authenticate_client(
        self, user_input: dict
    ) -> api_client.AnkerSolixApiClient:
        """Validate credentials and return the api client."""
        client = api_client.AnkerSolixApiClient(
            user_input,
            session=async_create_clientsession(self.hass),
        )
        await client.authenticate(restart=True)
        return client


class AnkerSolixOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle options flow."""
        errors: dict[str, str] = {}
        placeholders: dict[
            str, str
        ] = {}  # NOTE: Passed option placeholder do not work with translation files, HASS Bug?

        if user_input:
            if user_input.get(TESTFOLDER) or not user_input.get(TESTMODE):
                return self.async_create_entry(title="", data=user_input)
            # Test mode enabled but no existing folder selected
            errors[TESTFOLDER] = "folder_invalid"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                get_options_schema(user_input or self.config_entry.options)
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


def get_options_schema(entry: dict | None = None) -> dict:
    """Create the options schema dictionary."""

    if entry is None:
        entry = {}
    schema = {
        vol.Optional(
            CONF_SCAN_INTERVAL,
            default=entry.get(
                CONF_SCAN_INTERVAL,
                SCAN_INTERVAL_DEF,
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=_SCAN_INTERVAL_MIN,
                max=_SCAN_INTERVAL_MAX,
                step=_SCAN_INTERVAL_STEP,
                unit_of_measurement="sec",
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
        vol.Optional(
            INTERVALMULT,
            default=entry.get(INTERVALMULT, INTERVALMULT_DEF),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=_INTERVALMULT_MIN,
                max=_INTERVALMULT_MAX,
                step=_INTERVALMULT_STEP,
                unit_of_measurement="updates",
                mode=selector.NumberSelectorMode.SLIDER,
            ),
        ),
        vol.Optional(
            CONF_DELAY_TIME,
            default=entry.get(CONF_DELAY_TIME, DELAY_TIME_DEF),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=_DELAY_TIME_MIN,
                max=_DELAY_TIME_MAX,
                step=_DELAY_TIME_STEP,
                unit_of_measurement="sec",
                mode=selector.NumberSelectorMode.SLIDER,
            ),
        ),
        vol.Optional(
            CONF_EXCLUDE,
            default=entry.get(
                CONF_EXCLUDE,
                list(api_client.DEFAULT_EXCLUDE_CATEGORIES),
            ),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(api_client.API_CATEGORIES),
                #mode="dropdown",
                #mode="list",
                sort=False,
                multiple=True,
                translation_key=CONF_EXCLUDE,
            )
        ),
    }
    if _ALLOW_TESTMODE:
        if not (jsonfolders := api_client.json_example_folders()):
            # Add empty element to ensure proper list validation
            jsonfolders = [""]
        jsonfolders.sort()
        schema.update(
            {
                vol.Optional(
                    TESTMODE,
                    default=entry.get(TESTMODE, False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    TESTFOLDER,
                    description={
                        "suggested_value": entry.get(
                            TESTFOLDER, next(iter(jsonfolders), "")
                        )
                    },
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=jsonfolders, mode="dropdown")
                ),
            }
        )
    return schema


async def async_check_and_remove_devices(
    hass: HomeAssistant, user_input: dict, apidata: dict, excluded: set | None = None
) -> config_entries.ConfigEntry | None:
    """Check if given user input with its initial apidata has shared devices with existing configuration.

    If there are none, remove devices of this config that are no longer available for the configuration.
    """

    obsolete_user_devs = {}
    # ensure device type is also excluded when subcategories are excluded to remove device entities with reload from registry
    if excluded:
        # Subcategories for System devices
        if {
            ApiCategories.site_price,
            SolixDeviceType.SOLARBANK.value,
            SolixDeviceType.INVERTER.value,
            SolixDeviceType.PPS.value,
            SolixDeviceType.POWERPANEL.value,
        } & excluded:
            excluded = excluded | {SolixDeviceType.SYSTEM.value}
        # Subcategories for Solarbank only
        if {
            ApiCategories.solarbank_energy,
            ApiCategories.solarbank_cutoff,
            ApiCategories.solarbank_fittings,
            ApiCategories.solarbank_solar_info,
        } & excluded:
            excluded = excluded | {SolixDeviceType.SOLARBANK.value}
        # Subcategories for all managed Devices
        if {
            ApiCategories.device_auto_upgrade,
        } & excluded:
            excluded = excluded | {
                SolixDeviceType.SOLARBANK.value,
                SolixDeviceType.INVERTER.value,
                SolixDeviceType.PPS.value,
                SolixDeviceType.POWERPANEL.value,
            }

    # get all device entries for a domain
    cfg_entries = hass.config_entries.async_entries(domain=DOMAIN)
    for cfg_entry in cfg_entries:
        device_entries = dr.async_entries_for_config_entry(
            dr.async_get(hass), cfg_entry.entry_id
        )
        for dev_entry in device_entries:
            if (
                username := str(user_input.get(CONF_USERNAME) or "").lower()
            ) and username != cfg_entry.unique_id:
                # config entry of another account
                if dev_entry.serial_number in apidata:
                    return cfg_entry
            # device is registered for same account, check if still used in coordinator data or excluded and add to obsolete list for removal
            elif dev_entry.serial_number not in apidata or (
                excluded
                and not {apidata.get(dev_entry.serial_number, {}).get("type")}
                - excluded
            ):
                obsolete_user_devs[dev_entry.id] = dev_entry.serial_number

    # Remove the obsolete device entries
    dev_registry = None
    for dev_id, serial in obsolete_user_devs.items():
        # ensure to obtain dev registry again if no longer available
        if dev_registry is None:
            dev_registry = dr.async_get(hass)
        dev_registry.async_remove_device(dev_id)
        # NOTE: removal of any underlying entities is handled by core
        LOGGER.info(
            "Removed device entry %s from registry for device %s due to excluded entities or unused device",
            dev_id,
            serial,
        )
    return None
