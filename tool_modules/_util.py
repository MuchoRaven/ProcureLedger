"""
tool_modules/_util.py
=====================
Small shared helpers for the tool layer.

``_utc_today`` and ``_as_float`` used to live in ``tools.py``. They are moved
here so other modules can use them without importing the whole tool layer. The
bodies are unchanged, so behaviour is identical.
"""

from __future__ import annotations

from typing import Any

from tool_modules._state import _now


def _utc_today() -> str:
    """Return today's UTC date as an ISO-8601 string."""
    return _now()[:10]


def _as_float(value: Any) -> float:
    """Coerce a numeric field to float, treating anything else as 0.0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0
