from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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
)

_LOGGER = logging.getLogger(__name__)

# After calling source_on, wait this long before checking for external override.
# Must exceed the BLE state-confirmation latency (~585 ms observed).
_BLE_CONFIRM = 1.2


def _get_opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entity = PwmFanEntity(
        hass,
        source_entity_id=config_entry.data[CONF_SOURCE_ENTITY],
        name=config_entry.title,
        entry_id=config_entry.entry_id,
        config_entry=config_entry,
    )
    async_add_entities([entity])
    config_entry.async_on_unload(
        config_entry.add_update_listener(entity.async_options_updated)
    )


class PwmFanEntity(FanEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        source_entity_id: str,
        name: str,
        entry_id: str,
        config_entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self._source_entity_id = source_entity_id
        self._config_entry = config_entry
        self._attr_name = name
        self._attr_unique_id = f"pwm_fan_{entry_id}"
        self._attr_is_on = False
        self._attr_percentage = 0
        self._attr_current_direction = "forward"
        self._pwm_task: asyncio.Task | None = None
        self._last_percentage: int = 100
        self._load_options(config_entry)

    def _load_options(self, entry: ConfigEntry) -> None:
        self._pwm_period = _get_opt(entry, CONF_PWM_PERIOD, DEFAULT_PWM_PERIOD)
        self._min_on_time = _get_opt(entry, CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME)
        self._min_off_time = _get_opt(entry, CONF_MIN_OFF_TIME, DEFAULT_MIN_OFF_TIME)
        self._gamma = _get_opt(entry, CONF_GAMMA, DEFAULT_GAMMA)
        self._ramp_up_duration = _get_opt(entry, CONF_RAMP_UP_DURATION, DEFAULT_RAMP_UP_DURATION)
        self._source_speed = int(_get_opt(entry, CONF_SOURCE_SPEED, DEFAULT_SOURCE_SPEED))
        self._pwm_threshold = int(_get_opt(entry, CONF_PWM_THRESHOLD, DEFAULT_PWM_THRESHOLD))

    @property
    def supported_features(self) -> FanEntityFeature:
        return (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.DIRECTION
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

    async def async_added_to_hass(self) -> None:
        if last_state := await self.async_get_last_state():
            self._attr_is_on = last_state.state == "on"
            if pct := last_state.attributes.get("percentage"):
                self._attr_percentage = int(pct)
                if int(pct) > 0:
                    self._last_percentage = int(pct)
            if direction := last_state.attributes.get("direction"):
                self._attr_current_direction = direction

        if self._attr_is_on and self._attr_percentage > 0:
            await self._apply_speed(self._attr_percentage, ramp_up=False)

    async def async_will_remove_from_hass(self) -> None:
        await self._stop_pwm_async()
        await self._source_off()

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        self._attr_is_on = True
        if percentage is not None:
            self._attr_percentage = percentage
        else:
            self._attr_percentage = self._last_percentage
        self._last_percentage = self._attr_percentage
        self.async_write_ha_state()
        await self._apply_speed(self._attr_percentage, ramp_up=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        await self._stop_pwm_async()
        self.async_write_ha_state()
        await self._source_off()
        self._attr_percentage = 0
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        self._attr_percentage = percentage
        self._last_percentage = percentage
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._apply_speed(percentage, ramp_up=False)

    async def async_set_direction(self, direction: str) -> None:
        self._attr_current_direction = direction
        await self.hass.services.async_call(
            "fan",
            "set_direction",
            {"entity_id": self._source_entity_id, "direction": direction},
        )
        self.async_write_ha_state()

    async def async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._load_options(entry)
        if self._attr_is_on:
            await self._apply_speed(self._attr_percentage, ramp_up=False)

    def _is_native_mode(self, pct: int) -> bool:
        return (
            self._pwm_threshold > 0
            and pct >= self._pwm_threshold
            and self._source_supports_speed()
        )

    async def _apply_speed(self, pct: int, ramp_up: bool = False) -> None:
        if self._is_native_mode(pct):
            await self._stop_pwm_async()
            await self._source_on(speed=pct)
        else:
            await self._start_pwm(ramp_up=ramp_up)

    async def _start_pwm(self, ramp_up: bool = False) -> None:
        await self._stop_pwm_async()
        self._pwm_task = self.hass.async_create_task(self._pwm_loop(ramp_up=ramp_up))

    async def _stop_pwm_async(self) -> None:
        if self._pwm_task and not self._pwm_task.done():
            self._pwm_task.cancel()
            try:
                await self._pwm_task
            except (asyncio.CancelledError, Exception):
                pass
        self._pwm_task = None

    def _source_supports_speed(self) -> bool:
        state = self.hass.states.get(self._source_entity_id)
        if state is None:
            return False
        return bool(state.attributes.get("supported_features", 0) & FanEntityFeature.SET_SPEED)

    async def _source_on(self, speed: int | None = None) -> None:
        service_data: dict[str, Any] = {"entity_id": self._source_entity_id}
        if self._source_supports_speed():
            service_data["percentage"] = speed if speed is not None else self._source_speed
        await self.hass.services.async_call("fan", "turn_on", service_data)

    async def _source_off(self) -> None:
        await self.hass.services.async_call(
            "fan", "turn_off", {"entity_id": self._source_entity_id}
        )

    def _calc_times(self, pct: float) -> tuple[float, float]:
        scale = self._pwm_threshold if self._pwm_threshold > 0 else 100
        duty = (pct / scale) ** self._gamma
        duty = min(duty, 1.0)
        on_time = max(duty * self._pwm_period, self._min_on_time)
        off_time = max((1.0 - duty) * self._pwm_period, self._min_off_time)
        return on_time, off_time

    async def _pwm_loop(self, ramp_up: bool = False) -> None:
        try:
            if ramp_up and self._ramp_up_duration > 0:
                await self._source_on(speed=100)
                await asyncio.sleep(self._ramp_up_duration)

            while True:
                pct = self._attr_percentage or 0

                if pct >= 100:
                    await self._source_on()
                    confirm = min(_BLE_CONFIRM, self._pwm_period)
                    await asyncio.sleep(confirm)
                    if self._source_is_off():
                        await self._handle_external_off()
                        return
                    await asyncio.sleep(self._pwm_period - confirm)
                elif pct <= 0:
                    await self._source_off()
                    await asyncio.sleep(self._pwm_period)
                else:
                    on_time, off_time = self._calc_times(pct)
                    await self._source_on()
                    confirm = min(_BLE_CONFIRM, on_time)
                    await asyncio.sleep(confirm)
                    if self._source_is_off():
                        await self._handle_external_off()
                        return
                    await asyncio.sleep(on_time - confirm)
                    await self._source_off()
                    await asyncio.sleep(off_time)

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("PWM loop error for %s", self._source_entity_id)
            try:
                await self._source_off()
            except Exception:
                pass

    def _source_is_off(self) -> bool:
        state = self.hass.states.get(self._source_entity_id)
        return state is not None and state.state == "off"

    async def _handle_external_off(self) -> None:
        _LOGGER.debug("External override on %s", self._source_entity_id)
        self._attr_is_on = False
        self._attr_percentage = 0
        self.async_write_ha_state()
        await self._source_off()
