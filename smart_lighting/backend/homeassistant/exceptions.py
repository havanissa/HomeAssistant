"""Exceptions of the host interface (names as in Home Assistant)."""


class HomeAssistantError(Exception):
    """A request to Home Assistant failed (or was refused)."""


class ServiceNotFound(HomeAssistantError):
    """The service does not exist."""


class Unauthorized(HomeAssistantError):
    """Not allowed."""
