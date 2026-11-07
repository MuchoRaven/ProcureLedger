"""
tool_modules/_followup.py
=========================
Follow-up work: flagging late POs and notifying on delays.

App-layer only. Nothing here is in ``decisions.PROPOSAL_KINDS``, so the model
cannot reach it; the operator credential is required at the HTTP surface.

The flag path is idempotent and race-safe: ``_check_flag_late`` raises
``_PO_ALREADY_FLAGGED`` when a PO has already been flagged, and
``_apply_flag_late`` treats that as a skip rather than an error. The notify
path uses the same pattern with ``_DELAY_ALREADY_NOTIFIED``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from tool_modules._state import (
    _LOCK,
    _POS,
    ToolError,
    _now,
    _today,
    _commit,
    PO_STATUS_OPEN,
)
from tool_modules._codes import PO_NOT_LATE

# Module-local sentinels. They never leave this module, so they are not in
# ``_codes``. ``_PO_ALREADY_FLAGGED`` is raised by ``_check_flag_late`` and
# caught by ``_apply_flag_late`` to make the pass idempotent and race-safe;
# ``_DELAY_ALREADY_NOTIFIED`` plays the same role for ``_apply_notify_delay``.
_PO_ALREADY_FLAGGED = "PO_ALREADY_FLAGGED"
_DELAY_ALREADY_NOTIFIED = "DELAY_ALREADY_NOTIFIED"


def _resolve_as_of(as_of: str) -> str:
    """Return ``as_of`` as an ISO date, defaulting to today.

    An empty value means "today", so a caller that omits the argument gets a
    deterministic reference rather than an empty string that would make every
    comparison false.
    """
    text = (as_of or "").strip()
    if not text:
        return _today()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ToolError("INVALID_ARGUMENTS", {"as_of": as_of})


def _is_late(po: dict, reference: str) -> bool:
    """Return True if ``po`` is scheduled before ``reference``.

    The empty-string guard is load-bearing: ``"" < "2024-01-01"`` is True
    lexicographically, so an unscheduled PO would otherwise be flagged late on
    every run. The ISO guard is load-bearing too: ``list_late_pos`` calls
    ``date.fromisoformat`` on the same field, and a malformed value that sorts
    before the reference would crash it.
    """
    expected = po.get("expected_delivery")
    if not isinstance(expected, str) or not expected:
        return False
    try:
        date.fromisoformat(expected)
    except ValueError:
        return False
    return expected < reference


def _late_pos(as_of: str = "") -> list[str]:
    """Return the IDs of OPEN POs that are late and not yet flagged.

    Resolves ``as_of`` once so every comparison in the pass uses the same
    reference date. Reads under ``_LOCK`` so a concurrent mutation cannot be
    observed half-applied.
    """
    reference = _resolve_as_of(as_of)
    with _LOCK:
        return sorted(
            po_id
            for po_id, po in _POS.items()
            if po.get("status") == PO_STATUS_OPEN
            and _is_late(po, reference)
            and not po.get("late_flagged_at")
        )
