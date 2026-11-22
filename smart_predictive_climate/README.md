# Smart Adaptive & Predictive Climate

A Home Assistant App that observes your rooms, learns how each one gains and loses heat, and predicts room
temperatures - from the sensors, the weather forecast and the heating you already have. The forecasts and
recommendations are published as native Home Assistant entities for your own dashboards and automations.

It only observes, learns and predicts. It never sets a thermostat, opens a valve or calls any service.

- Pages: Overview, Rooms, Thermal zones, Settings.
- When the App is not running, its entities are unavailable: an old forecast never looks like a recommendation.

Needs the small companion integration `smart_predictive_climate`, installed with HACS from
https://github.com/havanissa/smart-predictive-climate. See the Documentation tab for installation and use.
