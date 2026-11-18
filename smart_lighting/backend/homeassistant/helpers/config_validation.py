"""The validators of ``homeassistant.helpers.config_validation`` that the lighting websocket handlers use."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

_ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")


def valid_entity_id(entity_id: str) -> bool:
    return bool(_ENTITY_ID.match(entity_id))


def string(value: Any) -> str:
    if value is None:
        raise vol.Invalid("string value is None")
    if isinstance(value, (list, dict)):
        raise vol.Invalid("value should be a string")
    return str(value)


def entity_id(value: Any) -> str:
    value = string(value).lower()
    if valid_entity_id(value):
        return value
    raise vol.Invalid(f"Entity ID {value} is an invalid entity ID")


def entity_ids(value: Any) -> list[str]:
    if value is None:
        raise vol.Invalid("Entity IDs cannot be None")
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    return [entity_id(v) for v in value]


def entity_domain(domain: str | list[str]) -> Any:
    domains = {domain} if isinstance(domain, str) else set(domain)

    def validate(value: Any) -> str:
        ent_id = entity_id(value)
        if ent_id.split(".", 1)[0] in domains:
            return ent_id
        raise vol.Invalid(f"Entity ID '{value}' does not belong to domain '{domain}'")

    return validate


def boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower().strip()
        if lowered in ("1", "true", "yes", "on", "enable"):
            return True
        if lowered in ("0", "false", "no", "off", "disable"):
            return False
    elif isinstance(value, (int, float)):
        return bool(value)
    raise vol.Invalid(f"invalid boolean value {value}")


positive_int = vol.All(vol.Coerce(int), vol.Range(min=0))
