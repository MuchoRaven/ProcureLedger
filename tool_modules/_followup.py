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


def _check_flag_late(po_id: str, as_of: str) -> None:
    """Validate a late-flag request without changing anything.

    Runs inside ``_commit``'s ``check`` callable, so it raises rather than
    returns. The order matters: a missing PO is ``PO_NOT_FOUND``, a PO that is
    not OPEN is ``PO_NOT_OPEN``, a PO that is not late is ``PO_NOT_LATE``, and
    only then is an already-flagged PO reported as ``_PO_ALREADY_FLAGGED`` -
    which ``_apply_flag_late`` turns into a skip.
    """
    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
        if po.get("status") != PO_STATUS_OPEN:
            raise ToolError(
                "PO_NOT_OPEN",
                {"po_id": po_id, "status": po.get("status")},
            )
        if not _is_late(po, as_of):
            raise ToolError(
                PO_NOT_LATE,
                {
                    "po_id": po_id,
                    "expected_delivery": po.get("expected_delivery"),
                    "as_of": as_of,
                },
            )
        if po.get("late_flagged_at"):
            raise ToolError(
                _PO_ALREADY_FLAGGED,
                {"po_id": po_id, "late_flagged_at": po.get("late_flagged_at")},
            )


def _apply_flag_late(po_id: str, as_of: str, correlation_id: str = "") -> dict:
    """Flag one late PO, idempotently.

    Resolves ``as_of`` at the top so a direct call with ``""`` means today.
    Reads ``expected_delivery`` under ``_LOCK`` to compute ``days_late`` for
    the audit payload; a missing or malformed value yields ``0``, which is
    never persisted because ``_check_flag_late`` raises ``PO_NOT_LATE`` first.

    A missing PO is reported without calling ``_commit``: the check would
    raise the same code, so this is equivalent and avoids a ``KeyError`` on
    the read above. An already-flagged PO is a skip, not an error, which is
    what makes the pass idempotent and race-safe.
    """
    reference = _resolve_as_of(as_of)

    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {
                "ok": False,
                "error": "PO_NOT_FOUND",
                "detail": {"po_id": po_id},
            }
        expected_delivery = po.get("expected_delivery")

    days_late = 0
    if isinstance(expected_delivery, str) and expected_delivery:
        try:
            days_late = (
                date.fromisoformat(reference)
                - date.fromisoformat(expected_delivery)
            ).days
        except ValueError:
            days_late = 0

    def mutation() -> None:
        with _LOCK:
            _POS[po_id]["late_flagged_at"] = _now()

    payload = {
        "po_id": po_id,
        "expected_delivery": expected_delivery,
        "as_of": reference,
        "days_late": days_late,
    }

    try:
        _commit(
            mutation,
            "PO_LATE",
            payload,
            correlation_id,
            check=lambda: _check_flag_late(po_id, reference),
        )
    except ToolError as exc:
        if exc.code == _PO_ALREADY_FLAGGED:
            return {"ok": True, "skipped": True, "po_id": po_id}
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "po_id": po_id, "days_late": days_late}


def run_followup(as_of: str = "") -> dict:
    """Flag every late, unflagged OPEN PO as of ``as_of``.

    Resolves ``as_of`` once, collects the IDs outside any lock, then flags
    them one at a time. ``_LOCK`` is deliberately not held across the loop:
    ``_commit`` does an fsync per entry, and holding the lock would serialise
    the whole store behind the audit log.

    Each result is classified as ``skipped`` (already flagged), ``flagged``
    (newly flagged), or an error. The pass is idempotent, so re-running it
    reports the same POs as skipped rather than flagging them twice.
    """
    reference = _resolve_as_of(as_of)

    flagged: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for po_id in _late_pos(reference):
        result = _apply_flag_late(po_id, reference)
        if result.get("ok") and result.get("skipped"):
            skipped.append(po_id)
        elif result.get("ok"):
            flagged.append(po_id)
        else:
            errors.append(
                {
                    "po_id": po_id,
                    "error": result.get("error"),
                    "detail": result.get("detail"),
                }
            )

    return {
        "ok": True,
        "as_of": reference,
        "flagged": flagged,
        "skipped": skipped,
        "errors": errors,
    }


def _apply_notify_delay(
    po_id: str, notified_by: str, correlation_id: str = ""
) -> dict:
    """Record that a late PO's delay has been notified, idempotently.

    Reads ``late_flagged_at`` under ``_LOCK`` for the audit payload. A missing
    PO is reported without calling ``_commit``: the check would raise the same
    code, so this is equivalent and avoids a ``KeyError`` on the read above.

    ``check`` raises rather than returns - it is ``_commit``'s
    ``Callable[[], None]``. The already-notified case is a sentinel raise that
    the caller turns into a skip, mirroring ``_apply_flag_late``. That is what
    makes a re-run report the same PO as skipped instead of notifying twice.
    """
    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {
                "ok": False,
                "error": "PO_NOT_FOUND",
                "detail": {"po_id": po_id},
            }
        late_flagged_at = po.get("late_flagged_at")

    def check() -> None:
        with _LOCK:
            current = _POS.get(po_id)
            if current is None:
                raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
            if current.get("status") != PO_STATUS_OPEN:
                raise ToolError(
                    "PO_NOT_OPEN",
                    {"po_id": po_id, "status": current.get("status")},
                )
            if not current.get("late_flagged_at"):
                raise ToolError(
                    PO_NOT_LATE,
                    {
                        "po_id": po_id,
                        "late_flagged_at": current.get("late_flagged_at"),
                    },
                )
            if current.get("delay_notified_at"):
                raise ToolError(
                    _DELAY_ALREADY_NOTIFIED,
                    {
                        "po_id": po_id,
                        "delay_notified_at": current.get("delay_notified_at"),
                    },
                )

    def mutation() -> None:
        with _LOCK:
            record = _POS[po_id]
            record["delay_notified_at"] = _now()
            record["delay_notified_by"] = notified_by

    payload = {
        "po_id": po_id,
        "notified_by": notified_by,
        "late_flagged_at": late_flagged_at,
    }

    try:
        _commit(
            mutation,
            "DELAY_NOTIFIED",
            payload,
            correlation_id,
            check=check,
        )
    except ToolError as exc:
        if exc.code == _DELAY_ALREADY_NOTIFIED:
            return {"ok": True, "skipped": True, "po_id": po_id}
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "po_id": po_id}


def list_late_pos(as_of: str = "") -> dict:
    """Return every late PO as of ``as_of``, flagged or not.

    The worklist answers "what is late", not "what is newly late", so an
    already-flagged PO is included. Each entry is a copy of the PO dict plus
    ``days_late``; the copy matters because the caller must not be able to
    mutate the live store through the result.

    ``_is_late`` has already validated ``expected_delivery`` as an ISO date,
    so the ``date.fromisoformat`` below cannot raise for an entry that made it
    into the list.
    """
    reference = _resolve_as_of(as_of)
    with _LOCK:
        entries: list[dict] = []
        for po_id, po in sorted(_POS.items()):
            if not _is_late(po, reference):
                continue
            entry = dict(po)
            entry["days_late"] = (
                date.fromisoformat(reference)
                - date.fromisoformat(po["expected_delivery"])
            ).days
            entries.append(entry)

    return {"ok": True, "as_of": reference, "pos": entries}
