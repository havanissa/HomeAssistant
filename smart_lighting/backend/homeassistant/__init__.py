"""Host interface of the Smart Lighting App: the subset of Home Assistant's Python API the lighting runtime uses.

The App runs the integration's own lighting modules (``backend/smart_occupancy``,
byte-identical copies). They are written against Home Assistant's Python API;
this package provides exactly the part of it they use, on top of the App's
websocket connection to Home Assistant, so no rule needs a second implementation:

=============================== ==============================================================
Module                          Provided by
=============================== ==============================================================
``core``                        states and contexts from ``get_states`` / ``state_changed``;
                                event bus; ``Context`` (ULIDs); service calls → host backend
``helpers.event``               asyncio timers; per-entity state change tracking
``helpers.storage``             JSON files in the App's data folder (Home Assistant's format)
``helpers.*_registry``          ``config/*_registry/list`` (refreshed on registry events)
``helpers.sun``                 astral, with Home Assistant's location (``get_config``)
``helpers.trigger``             ``validate_config`` and ``subscribe_trigger`` (device triggers)
``helpers.dispatcher``          in-process signals
``helpers.config_validation``   the validators the websocket handlers use
``components.mqtt``             ``mqtt/subscribe`` through Home Assistant (no broker access)
``components.device_automation`` ``device_automation/trigger/list``
``components.websocket_api``    decorators and a connection object for the App's command router
``util.dt``                     time zone of Home Assistant; parsing
=============================== ==============================================================

This is not Home Assistant and does not try to be one: anything outside this
subset is missing on purpose, and ``smart_lighting/tests/test_host_interface.py``
fails when the vendored modules import a name this package does not provide.
"""
