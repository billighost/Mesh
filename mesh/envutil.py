"""
mesh/envutil.py — Defensive parsing for numeric and boolean environment/context values.

Environment variables are user-controlled. A malformed value (for example
``MESH_HTTP_PORT=abc``) must never crash a module at import time or take down
an otherwise healthy CLI/MCP session; callers fall back to the documented
default instead.
"""
from __future__ import annotations

import math
import os
from typing import Any, Optional

_TRUE_STRINGS = {"1", "true", "yes"}
_FALSE_STRINGS = {"0", "false", "no"}


def _raw(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Read an integer env var, clamping to bounds and falling back on bad input."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Read a finite float env var, clamping to bounds and falling back on bad input."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def parse_strict_bool(
    value: Any,
    *,
    default: bool = False,
    raise_on_invalid: bool = False,
) -> bool:
    """
    Strictly parse a boolean from bool, int (1/0), or string (true/false/1/0/yes/no, case-insensitive).
    Unlike `bool(value)`, string values such as `"false"`, `"0"`, and `"no"` evaluate to `False`.
    Unknown values raise `ValueError` when `raise_on_invalid=True`, or return `default` (default-deny `False`).
    """
    if value is None:
        if raise_on_invalid:
            raise ValueError("Boolean value cannot be None.")
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        if raise_on_invalid:
            raise ValueError(f"Invalid boolean integer {value!r}; expected 0 or 1.")
        return bool(default)
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in _TRUE_STRINGS:
            return True
        if clean in _FALSE_STRINGS:
            return False
        if raise_on_invalid:
            raise ValueError(
                f"Invalid boolean value {value!r}; expected one of true/false/1/0/yes/no."
            )
        return bool(default)
    if raise_on_invalid:
        raise ValueError(f"Invalid boolean value {value!r}; expected bool, 0/1, or boolean string.")
    return bool(default)


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var using the package-wide ``1/true/yes`` / ``0/false/no`` convention."""
    raw = _raw(name)
    if raw is None:
        return default
    return parse_strict_bool(raw, default=False)


__all__ = ["env_bool", "env_float", "env_int", "parse_strict_bool"]
