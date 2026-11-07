"""
tool_modules/_tools_audit.py
============================
The audit-chain tools: appending a note, and re-verifying the chain.

``log_audit_event`` and ``verify_audit_chain`` used to live in ``tools.py``.
They are moved here so the audit surface can be read without the whole tool
layer, and so ``_apply_ap._apply_audit_note`` can import ``log_audit_event`` at
module scope instead of deferring the import to call time.

This module imports only ``_state`` (the chain, its lock, and the hash
helpers). It deliberately does not import ``tools``, ``decisions`` or
``sandbox``, so it cannot participate in an import cycle.

``_state._append_audit`` resolves ``tools._persist_audit`` at call time, which
is what lets the test suite monkeypatch ``tools._persist_audit`` to simulate a
disk failure. That indirection lives in ``_state``, not here, so moving
``log_audit_event`` does not disturb it.
"""

from __future__ import annotations

from typing import Any

from tool_modules._state import (
    _RESERVED_EVENT_TYPES,
    ToolError,
    _append_audit,
    _load_audit_from_file,
    _audit_entries,
    _compute_hash,
    GENESIS_HASH,
)


def log_audit_event(
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> dict[str, Any]:
    """Append a note to the audit chain.

    ``source="tool"`` is the control plane's own bookkeeping and may use the
    reserved event types; ``source="agent"`` (the default) may not. Raises
    ``ToolError`` when the chain itself is unwritable - the caller decides
    whether that is fatal (``agent._audit`` treats it as such).
    """
    if not isinstance(event_type, str) or not event_type.strip():
        return {
            "ok": False,
            "error": "INVALID_EVENT_TYPE",
            "detail": "event_type must be a non-empty string",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "INVALID_PAYLOAD",
            "detail": "payload must be an object",
        }

    event_type = event_type.strip()
    if source != "tool" and event_type in _RESERVED_EVENT_TYPES:
        return {
            "ok": False,
            "error": "RESERVED_EVENT_TYPE",
            "detail": event_type,
        }

    entry = _append_audit(event_type, payload, correlation_id, source=source)
    return {
        "ok": True,
        "audit": {
            "seq": entry.get("seq"),
            "event_type": entry.get("event_type"),
            "hash": entry.get("hash"),
        },
    }


def verify_audit_chain(from_file: bool = True) -> dict[str, Any]:
    """Re-verify the hash chain.

    Reports an unreadable log in-band with ``ok: True`` and the raw error text
    in ``detail``; ``app.py`` redacts that field on 5xx-style failures. A
    broken link is reported with ``valid: False`` and the index it broke at.
    """
    try:
        entries = _load_audit_from_file() if from_file else _audit_entries()
    except ToolError as exc:
        return {
            "ok": True,
            "valid": False,
            "entries": 0,
            "detail": exc.detail,
        }

    prev = GENESIS_HASH
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "MALFORMED_ENTRY",
            }
        if entry.get("prev_hash") != prev:
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "PREV_HASH_MISMATCH",
            }
        if entry.get("hash") != _compute_hash(entry):
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "HASH_MISMATCH",
            }
        prev = entry.get("hash", prev)

    return {"ok": True, "valid": True, "entries": len(entries)}
