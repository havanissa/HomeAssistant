# Smart Adaptive & Predictive Climate (companion integration)

Native Home Assistant entities for the **Smart Adaptive & Predictive Climate** App, which observes your rooms,
learns how each one gains and loses heat, and predicts room temperatures.

This integration only shows the App's results. It contains no model, registers no service and cannot control any
device. It accepts results only from the App it is paired with.

## Installation

1. Install the **Smart Adaptive & Predictive Climate** App (Settings → Apps → App store → ⋮ → Repositories → add
   `https://github.com/havanissa/HomeAssistant`, then install *Smart Adaptive & Predictive Climate*).
2. Install this integration with HACS: HACS → ⋮ → **Custom repositories** → add
   `https://github.com/havanissa/smart-predictive-climate`, type **Integration** → open
   *Smart Adaptive & Predictive Climate* → **Download** → restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Smart Adaptive & Predictive Climate* (nothing to fill in).
4. In the App: Settings → **Pair with Home Assistant**.
5. Check: `binary_sensor.spc_app_connected` is on, and every room the App publishes has a device with its entities
   (`sensor.spc_<room>_<key>`, `binary_sensor.spc_<room>_<key>`).

Updates come through HACS like any other integration. Pairing, rooms and entity ids are kept across updates,
restarts and reloads.

## Entities

The App decides which entities a room has (forecasts, times, recommendations); this integration creates them as
ordinary sensors and binary sensors, one device per room. `binary_sensor.spc_app_connected` tells whether the App
is connected and its results are fresh.

When the App is away (stopped, updating, not paired) every room entity is **unavailable**: an old forecast never
looks like a recommendation. They come back by themselves when the App publishes again.

## Compatibility

| Integration | App | Protocol |
|---|---|---|
| 0.1.x | 0.1.x | 1 |

The App checks the integration's version and protocol and says exactly what to update when they do not match.

## Source

Developed in [havanissa/HomeAssistant](https://github.com/havanissa/HomeAssistant)
(`custom_components/smart_predictive_climate`); this repository is published from there by a script for HACS.
Please report issues here; changes are made in the source repository.

License: MIT.
