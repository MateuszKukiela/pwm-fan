# PWM Fan

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that wraps any on/off fan entity and adds speed control via PWM (pulse-width modulation — cycling the fan on and off).

## Features

- **Speed control** — 0–100% via PWM duty cycle
- **Forward/reverse** — maps directly to the source entity's direction
- **On/off toggle**
- **State restoration** on HA restart
- Configurable PWM cycle period (default 15 s)

## Installation via HACS

1. In HACS → Integrations → three-dot menu → Custom repositories
2. Add `https://github.com/MateuszKukiela/pwm-fan` as an **Integration**
3. Install **PWM Fan**
4. Restart Home Assistant

## Setup

Settings → Devices & Services → Add Integration → **PWM Fan**

Pick your source fan entity, give the new entity a name, and optionally tune the PWM period.

## How it works

At a given speed percentage the integration cycles the underlying fan entity on and off within each PWM period:

```
50% speed, 15 s period → 7.5 s ON, 7.5 s OFF, repeat
30% speed, 15 s period → 4.5 s ON, 10.5 s OFF, repeat
100% speed             → always ON
0% / off               → always OFF
```

If ramp-up is enabled, source fan starts at 100% for ramp-up duration, then drops to configured ON-phase source speed. PWM loop then only toggles power on and off at that speed.
