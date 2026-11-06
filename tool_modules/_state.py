"""
tool_modules/_state.py
======================
Shared state, audit chain and commit primitives for ProcureLedger.

This module is the single source of truth for module-level state. It must not
import from ``tools`` or any other ``tool_modules`` submodule at import time,
so it can be imported first without creating a cycle. ``_resolve_persist``
imports ``tools`` lazily, at call time, purely to honour the
``tools._persist_audit`` test seam.

Layout
------
* Constants and status enums.
* ``ToolError`` and ``Principal``.
* Module-level state dictionaries and the re-entrant lock that guards them.
* Time, hashing and audit-chain helpers.
* ``_check_transition`` / ``_transition`` / ``_commit`` primitives.
* ``_recover_audit_state`` (called at import) and ``_reset_state`` (test hook).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_HASH = "0" * 64
AMOUNT_TOLERANCE = 0.01

PO_STATUS_OPEN = "OPEN"
PO_STATUS_CLOSED = "CLOSED"
PO_STATUS_CANCELLED = "CANCELLED"

REQ_STATUS_OPEN = "OPEN"
REQ_STATUS_CONVERTED = "CONVERTED"
REQ_STATUS_CANCELLED = "CANCELLED"

# Event types the model must not be able to forge via ``log_audit_event``.
# These are written by the control plane (``source="tool"``) only.
_RESERVED_EVENT_TYPES = frozenset(
    {
        "INVOICE_INGESTED",
        "PAYOUT_APPROVAL_REQUESTED",
        "PAYOUT_APPROVED",
        "PAYOUT_EXECUTED",
        "PO_CREATED",
        "PO_STATUS_CHANGED",
        "GOODS_RECEIPT_RECORDED",
        "REQUISITION_CREATED",
        "REQUISITION_STATUS_CHANGED",
        "THREE_WAY_MATCH",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_COMPLETED",
    }
)

# ---------------------------------------------------------------------------
# Errors and principals
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Domain error carrying a stable machine-readable code."""

    def __init__(self, code: str, detail: Optional[Any] = None):
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if self.detail:
            return f"{self.code}: {self.detail}"
        return self.code


@dataclass(frozen=True)
class Principal:
    """Authenticated caller identity."""

    sub: str
    roles: Tuple[str, ...] = field(default_factory=tuple)
    email: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()

_VENDORS: Dict[str, Dict[str, Any]] = {}
_POS: Dict[str, Dict[str, Any]] = {}
_RECEIPTS: Dict[str, Dict[str, Any]] = {}
_INVOICES: Dict[str, Dict[str, Any]] = {}
_APPROVALS: Dict[str, Dict[str, Any]] = {}
_REQUISITIONS: Dict[str, Dict[str, Any]] = {}

_AUDIT: List[Dict[str, Any]] = []
_AUDIT_SEQ: int = 0

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------


def _compute_hash(entry: Dict[str, Any]) -> str:
    """Hash an audit entry, excluding the ``hash`` field itself."""
    canonical = {k: v for k, v in entry.items() if k != "hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _head_hash(entries: List[Dict[str, Any]]) -> str:
    """Return the hash of the last entry, or the genesis hash if empty."""
    if not entries:
        return GENESIS_HASH
    return entries[-1].get("hash", GENESIS_HASH)


def _audit_log_path() -> str:
    """Resolve the durable audit log path from ``config`` if available."""
    try:
        import config  # local import: config is optional at import time

        return str(getattr(config, "AUDIT_LOG_PATH", "audit_log.jsonl"))
    except Exception:
        return "audit_log.jsonl"


def _load_audit_from_file() -> List[Dict[str, Any]]:
    """Read the durable audit log. Raises ``ToolError`` if it is unreadable."""
    path = _audit_log_path()
    if not os.path.exists(path):
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError("AUDIT_LOG_UNREADABLE", {"path": path, "error": str(exc)})
    return entries


def _audit_entries() -> List[Dict[str, Any]]:
    """Return a snapshot of the in-memory audit chain.

    Read through this helper rather than importing ``_AUDIT`` directly:
    ``_reset_state`` and ``_recover_audit_state`` rebind the module global, so
    a name imported into another module would go stale.
    """
    with _LOCK:
        return [dict(entry) for entry in _AUDIT]


def _persist_audit_entry(entry: Dict[str, Any]) -> None:
    """Append one entry to the durable audit log."""
    path = _audit_log_path()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        raise ToolError("AUDIT_LOG_UNWRITABLE", {"path": path, "error": str(exc)})


def _resolve_persist() -> Callable[[Dict[str, Any]], None]:
    """Return the function ``_append_audit`` should use to write one entry.

    ``tools`` re-exports ``_persist_audit_entry`` as ``_persist_audit`` so the
    test suite can monkeypatch ``tools._persist_audit`` to simulate a disk
    failure. Resolving the attribute at call time - rather than binding it at
    import - is what makes that patch effective. The fallback keeps ``_state``
    usable on its own (for example inside the sandbox worker).
    """
    try:
        import tools as _tools
    except Exception:
        return _persist_audit_entry
    candidate = getattr(_tools, "_persist_audit", None)
    return candidate if callable(candidate) else _persist_audit_entry


def _append_audit(
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> Dict[str, Any]:
    """Append an entry to the in-memory and durable audit chain.

    The durable write happens first: if it fails, ``ToolError`` propagates and
    neither the in-memory chain nor ``_AUDIT_SEQ`` advances, so a caller that
    treats the failure as fatal leaves no partial state behind.
    """
    global _AUDIT_SEQ
    with _LOCK:
        seq = _AUDIT_SEQ + 1
        entry: Dict[str, Any] = {
            "seq": seq,
            "event_type": event_type,
            "payload": payload,
            "correlation_id": correlation_id,
            "source": source,
            "timestamp": _now(),
            "prev_hash": _head_hash(_AUDIT),
        }
        entry["hash"] = _compute_hash(entry)
        # Persist first: if the write fails we must not advance in-memory state.
        _resolve_persist()(entry)
        _AUDIT.append(entry)
        _AUDIT_SEQ = seq
        return entry


# ---------------------------------------------------------------------------
# State transitions and commits
# ---------------------------------------------------------------------------


def _check_transition(
    invoice_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
) -> None:
    """Validate an invoice transition without changing anything.

    Raises ``ToolError`` on an unknown invoice or an illegal transition. Split
    out from ``_transition`` so ``_commit`` can reject a bad transition before
    it writes an audit link for it.
    """
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            raise ToolError("INVOICE_NOT_FOUND", {"invoice_id": invoice_id})
        current = invoice.get("status")
        if current not in from_states:
            raise ToolError(
                "INVALID_TRANSITION",
                {
                    "invoice_id": invoice_id,
                    "from": current,
                    "allowed": list(from_states),
                    "to": to_state,
                },
            )


def _transition(
    invoice_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
    correlation_id: str = "",
) -> None:
    """Move an invoice to ``to_state`` if its current state is allowed.

    Re-validates under the lock so a caller that already ran
    ``_check_transition`` cannot race a concurrent change.
    """
    with _LOCK:
        _check_transition(invoice_id, from_states, to_state)
        invoice = _INVOICES[invoice_id]
        invoice["status"] = to_state
        invoice["updated_at"] = _now()


def _commit(
    mutation: Callable[[], None],
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    check: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Validate, record, then mutate - in that order.

    The audit link is written *before* the mutation so a failed audit write
    leaves business state untouched (audit-before-mutation). ``check`` runs
    first and performs the mutation's fallible validation without changing
    anything, so an illegal transition is rejected before an audit entry is
    written for it. ``mutation`` is therefore expected to be infallible once
    ``check`` has passed.
    """
    with _LOCK:
        if check is not None:
            check()
        entry = _append_audit(event_type, payload, correlation_id, source="tool")
        mutation()
        return entry


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
#
# NOTE: this is a representative seed. Replace it with the real ERP seed (or
# load it from ``config.SEED_PATH``) before running against production data.


def _seed_state() -> None:
    """Populate the in-memory state with the baseline dataset."""
    _VENDORS.update(
        {
            "VEND-001": {
                "vendor_id": "VEND-001",
                "name": "Acme Industrial Supply",
                "status": "ACTIVE",
                "risk_tier": "LOW",
                "currency": "USD",
                "payment_terms": "NET30",
                "lead_time_days": 7,
            },
            "VEND-002": {
                "vendor_id": "VEND-002",
                "name": "Globex Components",
                "status": "ACTIVE",
                "risk_tier": "MEDIUM",
                "currency": "USD",
                "payment_terms": "NET45",
                "lead_time_days": 14,
            },
            "VEND-003": {
                "vendor_id": "VEND-003",
                "name": "Initech Logistics",
                "status": "INACTIVE",
                "risk_tier": "LOW",
                "currency": "USD",
                "payment_terms": "NET30",
                "lead_time_days": 21,
            },
            "VEND-004": {
                "vendor_id": "VEND-004",
                "name": "Umbrella Risk Partners",
                "status": "ACTIVE",
                "risk_tier": "HIGH",
                "currency": "USD",
                "payment_terms": "NET15",
                "lead_time_days": 5,
            },
        }
    )
    _POS.update(
        {
            "PO-1001": {
                "po_id": "PO-1001",
                "vendor_id": "VEND-001",
                "amount": 12500.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": "2024-01-20",
                "requisition_id": "REQ-4001",
                "created_at": _now(),
            },
            "PO-1002": {
                "po_id": "PO-1002",
                "vendor_id": "VEND-002",
                "amount": 800.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": "2024-02-10",
                "requisition_id": None,
                "created_at": _now(),
            },
        }
    )
    _RECEIPTS.update(
        {
            "RCPT-3001": {
                "receipt_id": "RCPT-3001",
                "po_id": "PO-1001",
                "amount": 12500.00,
                "received_at": _now(),
            },
            "RCPT-3002": {
                "receipt_id": "RCPT-3002",
                "po_id": "PO-1002",
                "amount": 800.00,
                "received_at": _now(),
            },
        }
    )
    _INVOICES.update(
        {
            "INV-2001": {
                "invoice_id": "INV-2001",
                "vendor_id": "VEND-001",
                "po_id": "PO-1001",
                "amount": 12500.00,
                "currency": "USD",
                "status": "RECEIVED",
                "receipt_ids": ["RCPT-3001"],
                "due_date": "2024-01-31",
                "created_at": _now(),
                "updated_at": _now(),
            },
            "INV-2002": {
                "invoice_id": "INV-2002",
                "vendor_id": "VEND-002",
                "po_id": "PO-1002",
                "amount": 800.00,
                "currency": "USD",
                "status": "RECEIVED",
                "receipt_ids": ["RCPT-3002"],
                "due_date": "2024-02-29",
                "created_at": _now(),
                "updated_at": _now(),
            },
        }
    )
    _REQUISITIONS.update(
        {
            "REQ-4001": {
                "requisition_id": "REQ-4001",
                "description": "Hydraulic fittings, Q1 restock",
                "vendor_id": "VEND-001",
                "po_id": "PO-1001",
                "quantity": 100,
                "amount": 12500.00,
                "currency": "USD",
                "status": REQ_STATUS_CONVERTED,
                "requested_by": "operator",
                "created_at": _now(),
                "updated_at": _now(),
            },
            "REQ-4002": {
                "requisition_id": "REQ-4002",
                "description": "Spare gaskets",
                "vendor_id": None,
                "po_id": None,
                "quantity": 40,
                "amount": 800.00,
                "currency": "USD",
                "status": REQ_STATUS_OPEN,
                "requested_by": "operator",
                "created_at": _now(),
                "updated_at": _now(),
            },
        }
    )


# ---------------------------------------------------------------------------
# Startup recovery and test reset
# ---------------------------------------------------------------------------


def _recover_audit_state() -> None:
    """Load the durable audit chain into memory at startup."""
    global _AUDIT, _AUDIT_SEQ
    with _LOCK:
        try:
            entries = _load_audit_from_file()
        except ToolError:
            entries = []
        _AUDIT = entries
        _AUDIT_SEQ = entries[-1]["seq"] if entries else 0


def _truncate_audit_log() -> None:
    """Remove the durable audit log, if present."""
    path = _audit_log_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _reset_state() -> None:
    """Reset all in-memory state and the audit log. Test hook only."""
    global _AUDIT, _AUDIT_SEQ
    with _LOCK:
        _VENDORS.clear()
        _POS.clear()
        _RECEIPTS.clear()
        _INVOICES.clear()
        _APPROVALS.clear()
        _REQUISITIONS.clear()
        _AUDIT = []
        _AUDIT_SEQ = 0
        _truncate_audit_log()
        _seed_state()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_seed_state()
_recover_audit_state()
