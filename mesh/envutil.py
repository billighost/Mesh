"""
mesh/envutil.py — Defensive parsing for numeric environment variables.

Environment variables are user-controlled. A malformed value (for example
``MESH_HTTP_PORT=abc``) must never crash a module at import time or take down
an otherwise healthy CLI/MCP session; callers fall back to the documented
default instead.
"""
from __future__ import annotations

import math
import os
from typing import Optional


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


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var using the package-wide ``1/true/yes`` convention."""
    raw = _raw(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


__all__ = ["env_bool", "env_float", "env_int"]
