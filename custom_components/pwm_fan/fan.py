from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_BLOCKING_CALLS,
    CONF_GAMMA,
    CONF_MIN_OFF_TIME,
    CONF_MIN_ON_TIME,
    CONF_PWM_PERIOD,
    CONF_PWM_THRESHOLD,
    CONF_RAMP_UP_DURATION,
    CONF_REMOTE_OFF_ID,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_SPEED,
    DEFAULT_BLOCKING_CALLS,
    DEFAULT_GAMMA,
    DEFAULT_MIN_OFF_TIME,
    DEFAULT_MIN_ON_TIME,
    DEFAULT_PWM_PERIOD,
    DEFAULT_PWM_THRESHOLD,
    DEFAULT_RAMP_UP_DURATION,
    DEFAULT_SOURCE_SPEED,
)

# ha-ble-adv coordinator singleton key in hass.data
_BLE_ADV_COORD_KEY = "ble_adv/coordinator_unique_id"

_LOGGER = logging.getLogger(__name__)


def _get_opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    return entry.options.get(key, entry.data.get(key, default))


def _reconstruct_adv_raw(service_info: BluetoothServiceInfoBleak) -> bytes | None:
    """Rebuild raw advertisement bytes from HA's parsed Bluetooth service info.

    ha-ble-adv's BleAdvAdvertisement.FromRaw expects the original on-air bytes.
    HA's Bluetooth stack gives us parsed service/manufacturer data, so we
    reconstruct the minimal AD structure that the codec parser needs.
    Returns None if no usable AD data is present.
    """
    flags = bytes([0x02, 0x01, 0x02])
    for uuid_str, data in service_info.advertisement.service_data.items():
        try:
            short_uuid = int(uuid_str.replace("-", "")[:8], 16) & 0xFFFF
        except (ValueError, IndexError):
            continue
        uuid_bytes = bytes([short_uuid & 0xFF, (short_uuid >> 8) & 0xFF])
        payload = uuid_bytes + data
        ad = bytes([len(payload) + 1, 0x16]) + payload
        return flags + ad
    for company_id, data in service_info.advertisement.manufacturer_data.items():
        payload = bytes([company_id & 0xFF, (company_id >> 8) & 0xFF]) + data
        ad = bytes([len(payload) + 1, 0xFF]) + payload
        return flags + ad
    return None


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
        self._external_off_task: asyncio.Task | None = None
        self._last_percentage: int = 100
        # _source_should_be_on: we intend the source to be on (cleared on source_off / stop)
        # _ble_confirmed_on: BLE state machine reported "on" since last source_on call.
        # External-off detection only fires when both are True, which means:
        #   - we asked for on AND BLE confirmed it, so a state→"off" event is a real remote press,
        #     not the ~585 ms BLE glitch that occurs before the fan physically responds.
        self._source_should_be_on: bool = False
        self._ble_confirmed_on: bool = False
        self._load_options(config_entry)

    def _load_options(self, entry: ConfigEntry) -> None:
        self._pwm_period = _get_opt(entry, CONF_PWM_PERIOD, DEFAULT_PWM_PERIOD)
        self._min_on_time = _get_opt(entry, CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME)
        self._min_off_time = _get_opt(entry, CONF_MIN_OFF_TIME, DEFAULT_MIN_OFF_TIME)
        self._gamma = _get_opt(entry, CONF_GAMMA, DEFAULT_GAMMA)
        self._ramp_up_duration = _get_opt(entry, CONF_RAMP_UP_DURATION, DEFAULT_RAMP_UP_DURATION)
        self._source_speed = int(_get_opt(entry, CONF_SOURCE_SPEED, DEFAULT_SOURCE_SPEED))
        self._pwm_threshold = int(_get_opt(entry, CONF_PWM_THRESHOLD, DEFAULT_PWM_THRESHOLD))
        self._blocking_calls = bool(_get_opt(entry, CONF_BLOCKING_CALLS, DEFAULT_BLOCKING_CALLS))

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

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._source_entity_id, self._handle_source_state_change
            )
        )
        self.async_on_remove(
            async_register_callback(
                self.hass,
                self._handle_bluetooth,
                None,
                BluetoothScanningMode.PASSIVE,
            )
        )

        if self._attr_is_on and self._attr_percentage > 0:
            await self._apply_speed(self._attr_percentage, ramp_up=False)

    @callback
    def _handle_source_state_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if new_state.state == "on":
            self._ble_confirmed_on = True
            return
        if (
            new_state.state == "off"
            and self._ble_confirmed_on
            and self._source_should_be_on
            and self._attr_is_on
            and self._pwm_task is not None
            and not self._pwm_task.done()
        ):
            # BLE previously confirmed "on", but source is now "off" while we're in an
            # on-phase → genuine remote turn-off (not the post-source_on BLE glitch).
            self._ble_confirmed_on = False
            self._source_should_be_on = False
            if self._external_off_task is None or self._external_off_task.done():
                self._external_off_task = self.hass.async_create_task(
                    self._handle_external_off()
                )

    @callback
    def _handle_bluetooth(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Detect remote off command from raw BLE advertisements via HA Bluetooth.

        Registered as a passive Bluetooth callback. Reconstructs raw advertisement
        bytes from HA's parsed representation, decodes via the ha-ble-adv coordinator,
        and checks for a fan-off command matching the configured remote ID.
        Fires before ha-ble-adv updates entity state, bypassing the BLE glitch window.
        """
        if not self._attr_is_on:
            return
        coordinator = self.hass.data.get(_BLE_ADV_COORD_KEY)
        if coordinator is None:
            _LOGGER.debug("BLE: ha-ble-adv coordinator not found in hass.data")
            return
        raw = _reconstruct_adv_raw(service_info)
        if raw is None:
            return
        try:
            result = coordinator.decode_raw(raw.hex())
        except Exception:
            return
        if not result or len(result) < 4:
            return
        ent_attrs_repr: str = result[-1]
        conf_repr: str = result[-2]
        if "'on': False" not in ent_attrs_repr or "fan_" not in ent_attrs_repr:
            return
        m = re.search(r"id: 0x([0-9A-Fa-f]+)", conf_repr)
        if not m:
            return
        detected_id = m.group(1).upper()
        remote_off_id: str | None = _get_opt(self._config_entry, CONF_REMOTE_OFF_ID, None)
        if remote_off_id:
            if detected_id != remote_off_id.upper():
                return
        else:
            _LOGGER.info(
                "BLE fan-off detected (id=0x%s). "
                "Set remote_off_id=%s in PWM Fan options to enable direct detection.",
                detected_id, detected_id,
            )
            return
        _LOGGER.debug("Remote off via Bluetooth adv id=0x%s", detected_id)
        if self._external_off_task is None or self._external_off_task.done():
            self._ble_confirmed_on = False
            self._source_should_be_on = False
            self._external_off_task = self.hass.async_create_task(
                self._handle_external_off()
            )

    async def _handle_external_off(self) -> None:
        _LOGGER.debug("External override on %s", self._source_entity_id)
        self._attr_is_on = False
        self._attr_percentage = 0
        self.async_write_ha_state()
        await self._stop_pwm_async()
        await self._source_off()

    def _cancel_external_off_task(self) -> None:
        if self._external_off_task and not self._external_off_task.done():
            self._external_off_task.cancel()
        self._external_off_task = None

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_external_off_task()
        await self._stop_pwm_async()
        await self._source_off()

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        self._cancel_external_off_task()
        self._attr_is_on = True
        if percentage is not None:
            self._attr_percentage = percentage
        else:
            self._attr_percentage = self._last_percentage
        self._last_percentage = self._attr_percentage
        self.async_write_ha_state()
        await self._apply_speed(self._attr_percentage, ramp_up=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._cancel_external_off_task()
        self._attr_is_on = False
        await self._stop_pwm_async()
        self.async_write_ha_state()
        await self._source_off()
        self._attr_percentage = 0
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        self._cancel_external_off_task()
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
        self._source_should_be_on = False
        self._ble_confirmed_on = False

    def _source_supports_speed(self) -> bool:
        state = self.hass.states.get(self._source_entity_id)
        if state is None:
            return False
        return bool(state.attributes.get("supported_features", 0) & FanEntityFeature.SET_SPEED)

    async def _source_on(self, speed: int | None = None) -> None:
        self._source_should_be_on = True
        self._ble_confirmed_on = False  # reset; wait for BLE to confirm "on" before detecting external off
        service_data: dict[str, Any] = {"entity_id": self._source_entity_id}
        if self._source_supports_speed():
            service_data["percentage"] = speed if speed is not None else self._source_speed
        await self.hass.services.async_call("fan", "turn_on", service_data, blocking=self._blocking_calls)

    async def _source_on_full_speed(self) -> None:
        await self._source_on(speed=100)

    async def _source_on_pwm_speed(self) -> None:
        await self._source_on(speed=self._source_speed)

    async def _source_off(self) -> None:
        self._source_should_be_on = False
        self._ble_confirmed_on = False
        await self.hass.services.async_call(
            "fan", "turn_off", {"entity_id": self._source_entity_id}, blocking=self._blocking_calls
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
                await self._source_on_full_speed()
                await asyncio.sleep(self._ramp_up_duration)
                if self._attr_percentage > 0:
                    await self._source_on_pwm_speed()

            while True:
                pct = self._attr_percentage or 0

                if pct >= 100:
                    await self._source_on_full_speed()
                    await asyncio.sleep(self._pwm_period)
                elif pct <= 0:
                    await self._source_off()
                    await asyncio.sleep(self._pwm_period)
                else:
                    on_time, off_time = self._calc_times(pct)
                    await self._source_on_pwm_speed()
                    await asyncio.sleep(on_time)
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
