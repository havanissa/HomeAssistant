"""YAML in and out.

* :func:`dump` writes the native Home Assistant YAML of an archive (block style, keys in their original order,
  unicode kept). It never emits Python-specific tags: an archive is plain YAML that Home Assistant reads.
* :func:`load` reads YAML back for verification (plain safe loading, no custom tags).
* :func:`load_tolerant` reads a Home Assistant YAML file (``automations.yaml``, an uploaded automation file) and
  turns its custom tags (``!secret``, ``!include``, ``!input`` ...) into :class:`Tagged` markers instead of
  failing, so that the caller can report them. Nothing is ever resolved or included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

MAX_YAML_BYTES = 8 * 1024 * 1024
#: The most nodes a document may expand to (aliases counted every time they are used): a guard against
#: "billion laughs" documents, whose aliases share objects that every later traversal would expand.
MAX_NODES = 500_000


@dataclass(frozen=True)
class Tagged:
    tag: str
    value: Any

    def __str__(self) -> str:
        return f"{self.tag} {self.value}"


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str)


def dump(value: Any) -> str:
    return yaml.dump(value, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False,
                     width=4096, indent=2)


def _check_expansion(node: yaml.Node | None) -> None:
    count = 0
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise ValueError("The YAML document expands to too many values (aliases)")
        if isinstance(current, yaml.SequenceNode):
            stack.extend(current.value)
        elif isinstance(current, yaml.MappingNode):
            for key, value in current.value:
                stack.append(key)
                stack.append(value)


def load(text: str) -> Any:
    """Plain safe loading (for archive files), with the expansion guard."""
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise ValueError("The YAML file is too large")
    _check_expansion(yaml.compose(text, Loader=yaml.SafeLoader))
    return yaml.safe_load(text)


class _TolerantLoader(yaml.SafeLoader):
    pass


def _tagged(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Tagged:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    return Tagged("!" + tag_suffix, value)


_TolerantLoader.add_multi_constructor("!", _tagged)


def load_tolerant(text: str) -> Any:
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise ValueError("The YAML file is too large")
    _check_expansion(yaml.compose(text, Loader=_TolerantLoader))  # noqa: S506 - a SafeLoader subclass
    return yaml.load(text, Loader=_TolerantLoader)  # noqa: S506 - a SafeLoader subclass


def find_tags(value: Any, path: str = "") -> list[str]:
    """Paths of every custom tag in a tolerant-loaded value."""
    out: list[str] = []
    if isinstance(value, Tagged):
        out.append(f"{path or '/'} ({value.tag})")
    elif isinstance(value, dict):
        for key, item in value.items():
            out.extend(find_tags(item, f"{path}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.extend(find_tags(item, f"{path}[{index}]"))
    return out


def json_compatible(value: Any) -> bool:
    """Only JSON types (YAML dates, binary data, sets ... cannot be sent to Home Assistant's editor endpoint)."""
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(json_compatible(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and json_compatible(v) for k, v in value.items())
    return False
