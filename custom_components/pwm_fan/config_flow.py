from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_GAMMA,
    CONF_MIN_OFF_TIME,
    CONF_MIN_ON_TIME,
    CONF_PWM_PERIOD,
    CONF_PWM_THRESHOLD,
    CONF_RAMP_UP_DURATION,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_SPEED,
    DEFAULT_GAMMA,
    DEFAULT_MIN_OFF_TIME,
    DEFAULT_MIN_ON_TIME,
    DEFAULT_PWM_PERIOD,
    DEFAULT_PWM_THRESHOLD,
    DEFAULT_RAMP_UP_DURATION,
    DEFAULT_SOURCE_SPEED,
    DOMAIN,
)

_PERIOD_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0.1, max=300, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
)
_TIME_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0.0, max=60, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
)
_RAMP_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0, max=30, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
)
_SPEED_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=100, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="%")
)
_THRESHOLD_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0, max=100, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="%")
)
_GAMMA_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0.1, max=2.0, step=0.05, mode=NumberSelectorMode.BOX)
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE_ENTITY): EntitySelector(EntitySelectorConfig(domain=FAN_DOMAIN)),
        vol.Optional("name"): TextSelector(),
        vol.Optional(CONF_PWM_THRESHOLD, default=DEFAULT_PWM_THRESHOLD): _THRESHOLD_SELECTOR,
        vol.Optional(CONF_PWM_PERIOD, default=DEFAULT_PWM_PERIOD): _PERIOD_SELECTOR,
        vol.Optional(CONF_MIN_ON_TIME, default=DEFAULT_MIN_ON_TIME): _TIME_SELECTOR,
        vol.Optional(CONF_MIN_OFF_TIME, default=DEFAULT_MIN_OFF_TIME): _TIME_SELECTOR,
        vol.Optional(CONF_GAMMA, default=DEFAULT_GAMMA): _GAMMA_SELECTOR,
        vol.Optional(CONF_RAMP_UP_DURATION, default=DEFAULT_RAMP_UP_DURATION): _RAMP_SELECTOR,
        vol.Optional(CONF_SOURCE_SPEED, default=DEFAULT_SOURCE_SPEED): _SPEED_SELECTOR,
    }
)


def _options_schema(entry: config_entries.ConfigEntry) -> vol.Schema:
    def _get(key, default):
        return entry.options.get(key, entry.data.get(key, default))

    return vol.Schema(
        {
            vol.Required(CONF_SOURCE_ENTITY, default=_get(CONF_SOURCE_ENTITY, "")): EntitySelector(EntitySelectorConfig(domain=FAN_DOMAIN)),
            vol.Optional(CONF_PWM_THRESHOLD, default=_get(CONF_PWM_THRESHOLD, DEFAULT_PWM_THRESHOLD)): _THRESHOLD_SELECTOR,
            vol.Optional(CONF_PWM_PERIOD, default=_get(CONF_PWM_PERIOD, DEFAULT_PWM_PERIOD)): _PERIOD_SELECTOR,
            vol.Optional(CONF_MIN_ON_TIME, default=_get(CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME)): _TIME_SELECTOR,
            vol.Optional(CONF_MIN_OFF_TIME, default=_get(CONF_MIN_OFF_TIME, DEFAULT_MIN_OFF_TIME)): _TIME_SELECTOR,
            vol.Optional(CONF_GAMMA, default=_get(CONF_GAMMA, DEFAULT_GAMMA)): _GAMMA_SELECTOR,
            vol.Optional(CONF_RAMP_UP_DURATION, default=_get(CONF_RAMP_UP_DURATION, DEFAULT_RAMP_UP_DURATION)): _RAMP_SELECTOR,
            vol.Optional(CONF_SOURCE_SPEED, default=_get(CONF_SOURCE_SPEED, DEFAULT_SOURCE_SPEED)): _SPEED_SELECTOR,
        }
    )


class PwmFanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            source_entity_id = user_input[CONF_SOURCE_ENTITY]

            await self.async_set_unique_id(source_entity_id)
            self._abort_if_unique_id_configured()

            name = (
                user_input.get("name")
                or source_entity_id.split(".")[1].replace("_", " ").title()
            )

            return self.async_create_entry(
                title=name,
                data={
                    CONF_SOURCE_ENTITY: source_entity_id,
                    CONF_PWM_THRESHOLD: user_input.get(CONF_PWM_THRESHOLD, DEFAULT_PWM_THRESHOLD),
                    CONF_PWM_PERIOD: user_input.get(CONF_PWM_PERIOD, DEFAULT_PWM_PERIOD),
                    CONF_MIN_ON_TIME: user_input.get(CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME),
                    CONF_MIN_OFF_TIME: user_input.get(CONF_MIN_OFF_TIME, DEFAULT_MIN_OFF_TIME),
                    CONF_RAMP_UP_DURATION: user_input.get(CONF_RAMP_UP_DURATION, DEFAULT_RAMP_UP_DURATION),
                    CONF_GAMMA: user_input.get(CONF_GAMMA, DEFAULT_GAMMA),
                    CONF_SOURCE_SPEED: user_input.get(CONF_SOURCE_SPEED, DEFAULT_SOURCE_SPEED),
                },
            )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

    @staticmethod
    def async_get_options_flow(config_entry):
        return PwmFanOptionsFlow(config_entry)


class PwmFanOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init", data_schema=_options_schema(self._config_entry)
        )
