# Automation Builder

A Home Assistant App for building automations without YAML: choose a device to control and give it rules of the
form **IF** something happens **AND/OR** conditions hold **THEN** do this.

- One controlled device, one native Home Assistant automation, any number of rules. Rules that fit together are
  combined; a rule that cannot share an automation safely gets its own, with the reason.
- Works for anything Home Assistant can control: lights, switches and relays, fans, covers, climate, locks, valves,
  helpers and more. You choose exactly which entities a device's rules control.
- Triggers: entity state, numeric threshold, time of day, sunrise/sunset, room occupancy. Conditions: state, number,
  time, combined with AND/OR.
- Occupancy is optional. A rule that uses an occupancy sensor gets automatic rechecks generated into its
  automation; a mood helper (`input_select`) is used like any other entity.
- Nothing reaches Home Assistant until you press **Deploy**: the automation is validated by Home Assistant, shown
  with its differences, deployed, and confirmed. An automation changed outside the App is never overwritten
  silently, and automations the App did not create are never touched.

It needs no companion integration and no other App. See the Documentation tab.
