# Changelog

## 0.1.0

- First release: an independent App that observes rooms, learns their thermal behaviour and predicts room
  temperatures. It recommends only; it never controls a device.
- Publishes its results as native Home Assistant entities through the companion integration
  `smart_predictive_climate` (pairing, lease and sequence fencing; entities are unavailable while the App is away).
- Room-first set-up (area → devices → entity roles; nothing pre-selected); a deliberately chosen room sensor is
  required and kept apart from the thermostat's own measurement.
- Bounded, resumable Recorder backfill (up to 90 days) into the App's own observation store; gaps stay gaps.
- Interpretable first-order thermal model per room (heat loss, heating with delay and inertia, sun through the
  windows with 0° = South, night baseline, measured exchange with zone neighbours), held-out validation at
  1, 3 and 6 hours, confidence low/moderate/high, "preliminary" below 14 days, "unidentifiable" when the data
  cannot support a model.
- Predictions with an 80 % band, heating and cooling rates, warm-up time, recommended preheat start (cautious,
  at most 90 minutes earlier than expected), solar gain, comfort schedules with an optional away entity and
  opt-in occupancy pattern; "heating as now" follows the thermostat's setpoint.
- What-if simulation (hypothetical, sends nothing), thermal zones with door barriers, configuration backups,
  import/export, diagnostics; responsive interface in light and dark.
- Recommendations below the configured confidence are `unknown`, never "off".
