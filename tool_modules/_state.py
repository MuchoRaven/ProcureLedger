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

import copy
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)

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
        # Written by ``_handle_unrecoverable_audit`` when a corrupt chain is
        # quarantined. Reserved so the model cannot forge a reset.
        "CHAIN_RESET",
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
    """Authenticated caller identity.

    ``auth_method`` is carried into the sandbox request envelope so the audit
    trail records how the caller authenticated. Without it,
    ``sandbox._build_request`` fell back to ``getattr(principal,
    "auth_method", "service")`` and every entry read ``"service"`` regardless
    of the credential used.
    """

    sub: str
    roles: Tuple[str, ...] = field(default_factory=tuple)
    email: str = ""
    auth_method: str = "service"

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


def _audit_recovery_mode() -> str:
    """Return the configured recovery policy: ``fail`` or ``quarantine``."""
    try:
        import config

        return str(getattr(config, "PL_AUDIT_RECOVERY", "fail")).strip().lower()
    except Exception:
        return "fail"


def _load_audit_from_file() -> List[Dict[str, Any]]:
    """Read the durable audit log.

    A torn *trailing* line is tolerated: it means the process died mid-append,
    so the line is incomplete rather than tampered with, and dropping it lets
    the next append start a clean line. A bad line anywhere else - or a
    non-object entry - raises ``AUDIT_LOG_CORRUPT``. Treating every parse
    failure as "unreadable" is what previously pushed a single torn write into
    the fail-open recovery path.
    """
    path = _audit_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        raise ToolError("AUDIT_LOG_UNREADABLE", {"path": path, "error": str(exc)})

    populated = [i for i, line in enumerate(raw_lines) if line.strip()]
    last_populated = populated[-1] if populated else -1

    entries: List[Dict[str, Any]] = []
    for index in populated:
        line = raw_lines[index].strip()
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == last_populated:
                _LOG.warning(
                    "audit log %s ends with a torn line at %d; dropping it",
                    path,
                    index + 1,
                )
                break
            raise ToolError(
                "AUDIT_LOG_CORRUPT",
                {"path": path, "line": index + 1, "error": str(exc)},
            )
        if not isinstance(entry, dict):
            raise ToolError(
                "AUDIT_LOG_CORRUPT",
                {
                    "path": path,
                    "line": index + 1,
                    "error": "entry is not an object",
                },
            )
        entries.append(entry)
    return entries


def _verify_chain(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Check sequence continuity, hash linkage and per-entry hashes.

    Returns a list of problems; empty means the chain is intact. Reading the
    log without verifying it is what let a tampered file boot cleanly.
    """
    problems: List[Dict[str, Any]] = []
    prev_hash = GENESIS_HASH
    expected_seq = 1
    for entry in entries:
        seq = entry.get("seq")
        if seq != expected_seq:
            problems.append(
                {"seq": seq, "problem": "SEQ_MISMATCH", "expected": expected_seq}
            )
        if entry.get("prev_hash") != prev_hash:
            problems.append({"seq": seq, "problem": "PREV_HASH_MISMATCH"})
        if entry.get("hash") != _compute_hash(entry):
            problems.append({"seq": seq, "problem": "HASH_MISMATCH"})
        prev_hash = entry.get("hash", GENESIS_HASH)
        expected_seq += 1
    return problems


def _audit_entries() -> List[Dict[str, Any]]:
    """Return a snapshot of the in-memory audit chain.

    Read through this helper rather than importing ``_AUDIT`` directly:
    ``_reset_state`` and ``_recover_audit_state`` rebind the module global, so
    a name imported into another module would go stale.

    The copy is deep: ``payload`` is a nested dict, and a shallow copy would
    hand the caller a reference into the live chain, letting it mutate an entry
    without changing that entry's hash.
    """
    with _LOCK:
        return copy.deepcopy(_AUDIT)


def _persist_audit_entry(entry: Dict[str, Any]) -> None:
    """Append one entry to the durable audit log and flush it to disk.

    ``fsync`` is what makes the audit-before-mutation ordering meaningful: a
    returned success must mean the entry survives a crash, not that it reached
    a buffer.
    """
    path = _audit_log_path()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
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


@dataclass(frozen=True)
class _EntitySpec:
    """Describes one transitionable record type.

    ``store`` is a callable rather than the dict itself so the spec keeps
    pointing at the live store after ``_reset_state`` clears it, and so a test
    that rebinds a module global is still observed.
    """

    store: Callable[[], Dict[str, Dict[str, Any]]]
    id_field: str
    not_found_code: str
    status_field: str = "status"
    updated_field: str = "updated_at"


_ENTITIES: Dict[str, _EntitySpec] = {
    "invoice": _EntitySpec(
        store=lambda: _INVOICES,
        id_field="invoice_id",
        not_found_code="INVOICE_NOT_FOUND",
    ),
    "po": _EntitySpec(
        store=lambda: _POS,
        id_field="po_id",
        not_found_code="PO_NOT_FOUND",
    ),
    "requisition": _EntitySpec(
        store=lambda: _REQUISITIONS,
        id_field="requisition_id",
        not_found_code="REQUISITION_NOT_FOUND",
    ),
}


def _entity_spec(entity: str) -> _EntitySpec:
    """Look up an entity spec, raising a stable code for an unknown name."""
    spec = _ENTITIES.get(entity)
    if spec is None:
        raise ToolError("UNKNOWN_ENTITY", {"entity": entity})
    return spec


def _check_entity_transition(
    entity: str,
    entity_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
) -> None:
    """Validate a transition for any registered entity without changing it.

    One implementation for invoices, POs and requisitions. The previous
    invoice-only helper forced ``tools._apply_transition`` to re-implement the
    same checks for the other two record types, and the copies could drift.

    Raises ``ToolError`` on an unknown entity or record, or an illegal
    transition. Split out from ``_transition_entity`` so ``_commit`` can reject
    a bad transition before it writes an audit link for it.
    """
    spec = _entity_spec(entity)
    with _LOCK:
        record = spec.store().get(entity_id)
        if record is None:
            raise ToolError(spec.not_found_code, {spec.id_field: entity_id})
        current = record.get(spec.status_field)
        if current not in from_states:
            raise ToolError(
                "INVALID_TRANSITION",
                {
                    "entity": entity,
                    spec.id_field: entity_id,
                    "from": current,
                    "allowed": list(from_states),
                    "to": to_state,
                },
            )


def _transition_entity(
    entity: str,
    entity_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
) -> None:
    """Move any registered entity to ``to_state`` if its state allows it.

    Re-validates under the lock so a caller that already ran
    ``_check_entity_transition`` cannot race a concurrent change.
    """
    spec = _entity_spec(entity)
    with _LOCK:
        _check_entity_transition(entity, entity_id, from_states, to_state)
        record = spec.store()[entity_id]
        record[spec.status_field] = to_state
        record[spec.updated_field] = _now()


def _check_transition(
    invoice_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
) -> None:
    """Invoice-specific wrapper kept for existing callers.

    Delegates to ``_check_entity_transition`` so there is exactly one
    implementation of the check.
    """
    _check_entity_transition("invoice", invoice_id, from_states, to_state)


def _transition(
    invoice_id: str,
    from_states: Tuple[str, ...],
    to_state: str,
    correlation_id: str = "",
) -> None:
    """Invoice-specific wrapper kept for existing callers.

    Delegates to ``_transition_entity`` so there is exactly one implementation
    of the mutation.
    """
    _transition_entity("invoice", invoice_id, from_states, to_state)


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


def _handle_unrecoverable_audit(exc: ToolError) -> None:
    """Apply the configured policy to an unreadable or unverifiable chain.

    ``fail`` (the default) re-raises, so the service refuses to boot. That is
    the fail-closed choice: continuing would append ``seq=1`` with
    ``prev_hash=GENESIS_HASH`` on top of a file that already holds entries,
    producing duplicate sequence numbers and a broken link - a forked chain
    that looks valid to every later reader.

    ``quarantine`` moves the bad file aside, starts a fresh chain, and records
    a ``CHAIN_RESET`` entry naming the quarantine path, so the reset is itself
    auditable rather than silent.
    """
    global _AUDIT, _AUDIT_SEQ
    if _audit_recovery_mode() != "quarantine":
        raise exc

    path = _audit_log_path()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantined = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, quarantined)
    except OSError as move_exc:
        raise ToolError(
            "AUDIT_LOG_UNREADABLE",
            {"path": path, "error": f"quarantine failed: {move_exc}"},
        )

    _AUDIT = []
    _AUDIT_SEQ = 0
    _LOG.error(
        "audit log %s quarantined to %s after %s; starting a fresh chain",
        path,
        quarantined,
        exc.code,
    )
    _append_audit(
        "CHAIN_RESET",
        {
            "reason": exc.code,
            "detail": exc.detail,
            "quarantined_to": quarantined,
        },
        source="tool",
    )


def _recover_audit_state() -> None:
    """Load and verify the durable audit chain into memory at startup.

    Verification is not optional: reading the log without checking it let a
    tampered file boot cleanly and be appended to.
    """
    global _AUDIT, _AUDIT_SEQ
    with _LOCK:
        try:
            entries = _load_audit_from_file()
        except ToolError as exc:
            _handle_unrecoverable_audit(exc)
            return

        problems = _verify_chain(entries)
        if problems:
            _handle_unrecoverable_audit(
                ToolError("AUDIT_LOG_CORRUPT", {"problems": problems[:5]})
            )
            return

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
