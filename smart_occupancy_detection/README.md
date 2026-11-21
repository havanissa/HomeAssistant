# Smart Occupancy Detection

A Home Assistant App that decides, room by room, whether a room is occupied - from the sensors you already
have - and publishes the result as native Home Assistant entities for your own automations.

It only detects. It never switches a light, moves a cover or calls any service.

- Motion (PIR), mmWave presence, door contacts, PC activity, bed sensors; several per room, shared between
  rooms when you want.
- Occupied, vacant or uncertain, with the reason, a confidence score and the last activity.
- An outage or an unavailable sensor never makes a room vacant.
- Three pages: Overview, Rooms, Settings.

Needs the small companion integration `smart_occupancy_detection` (in this repository, `custom_components/`)
for the native entities. See the Documentation tab for installation and use.
