"""Lossless comparison of automation configurations and registry metadata.

A configuration is compared as the JSON value Home Assistant returns: every key, every value and its type
(``1`` is not ``1.0`` is not ``true`` is not ``"1"``), list order, unknown keys. Only the order of keys inside
a mapping is not significant (it carries no meaning in YAML, and Home Assistant's editor writes ``id``,
``alias``, ``description``, ``triggers`` ... first).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same(a: Any, b: Any) -> bool:
    """Exact equality including types (Python's ``True == 1`` and ``1 == 1.0`` are not accepted)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def differences(a: Any, b: Any, path: str = "", limit: int = 20) -> list[str]:
    """Where two values differ (for reports), at most ``limit`` entries."""
    out: list[str] = []

    def walk(x: Any, y: Any, where: str) -> None:
        if len(out) >= limit:
            return
        if type(x) is not type(y):
            out.append(f"{where or '/'}: {type(x).__name__} {_short(x)} → {type(y).__name__} {_short(y)}")
            return
        if isinstance(x, dict):
            for key in sorted(set(x) | set(y), key=str):
                if key not in y:
                    out.append(f"{where}/{key}: removed")
                elif key not in x:
                    out.append(f"{where}/{key}: added")
                else:
                    walk(x[key], y[key], f"{where}/{key}")
            return
        if isinstance(x, list):
            if len(x) != len(y):
                out.append(f"{where or '/'}: {len(x)} items → {len(y)} items")
            for index, (p, q) in enumerate(zip(x, y, strict=False)):
                walk(p, q, f"{where}[{index}]")
            return
        if x != y:
            out.append(f"{where or '/'}: {_short(x)} → {_short(y)}")

    walk(a, b, path)
    return out


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 60 else text[:57] + "..."
