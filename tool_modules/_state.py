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
* ``_recover_audit_state`` (called at import) and ``_reset_state`` /
  ``_reset_phase0_state`` (test hooks).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

RFQ_STATUS_OPEN = "OPEN"
RFQ_STATUS_CLOSED = "CLOSED"
RFQ_STATUS_AWARDED = "AWARDED"
RFQ_STATUS_CANCELLED = "CANCELLED"

QUOTE_STATUS_RECEIVED = "RECEIVED"
QUOTE_STATUS_AWARDED = "AWARDED"
QUOTE_STATUS_REJECTED = "REJECTED"

EXCEPTION_STATUS_OPEN = "OPEN"
EXCEPTION_STATUS_RESOLVED = "RESOLVED"

# Duplicate-invoice detection window. Two invoices from the same vendor, for
# the same amount, against the same PO, inside this many days of each other are
# treated as a suspected duplicate. Kept here rather than in ``tools`` so the
# detector and any test that asserts on the window read one value.
DUPLICATE_WINDOW_DAYS = 30

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
        # Sourcing: RFQ -> quote -> award.
        # RFQ_CLOSED / RFQ_CANCELLED / QUOTE_SUBMITTED are written by
        # _tools_sourcing via _commit(..., source="tool").
        "RFQ_CREATED",
        "RFQ_STATUS_CHANGED",
        "RFQ_CLOSED",
        "RFQ_CANCELLED",
        "QUOTE_RECEIVED",
        "QUOTE_SUBMITTED",
        "QUOTE_AWARDED",
        # Follow-up. Written by the maintenance pass, never by the model.
        "PO_LATE",
        "DELAY_NOTIFIED",
        "PAYMENT_DUE",
        "PAYMENT_REMINDER_SENT",
        # Exception workflow. ``EXCEPTION_OPENED`` is written by the detector
        # and by the match path; ``EXCEPTION_RESOLVED`` by the operator tool.
        "EXCEPTION_OPENED",
        "EXCEPTION_RESOLVED",
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
    roles: tuple[str, ...] = field(default_factory=tuple)
    email: str = ""
    auth_method: str = "service"

    def has_role(self, role: str) -> bool:
        return role in self.roles


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()

_VENDORS: dict[str, dict[str, Any]] = {}
_POS: dict[str, dict[str, Any]] = {}
_RECEIPTS: dict[str, dict[str, Any]] = {}
_INVOICES: dict[str, dict[str, Any]] = {}
_APPROVALS: dict[str, dict[str, Any]] = {}
_REQUISITIONS: dict[str, dict[str, Any]] = {}
_RFQS: dict[str, dict[str, Any]] = {}
_QUOTES: dict[str, dict[str, Any]] = {}
_STOCK: dict[str, dict[str, Any]] = {}
_VENDOR_QUOTES: dict[str, dict[str, Any]] = {}
_EXCEPTIONS: dict[str, dict[str, Any]] = {}

_AUDIT: list[dict[str, Any]] = []
_AUDIT_SEQ: int = 0

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    """Return the current UTC date as ``YYYY-MM-DD``.

    Follow-up work (late POs, due payments) is date-granular, so the reference
    date is a plain ISO date rather than a timestamp. Callers that need a
    deterministic result pass ``as_of`` explicitly.
    """
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Payment terms
# ---------------------------------------------------------------------------

_TERMS_RE = re.compile(r"^NET\s*(\d+)$", re.IGNORECASE)


def _payment_terms_days(terms: Optional[str]) -> Optional[int]:
    """Parse a ``NETnn`` payment term into a day count.

    Returns ``None`` for an unrecognised term rather than guessing, so a caller
    can distinguish "due on receipt" (``NET0``) from "we do not know". The
    previous behaviour of defaulting to 30 silently invented a due date for
    every vendor with a malformed term.
    """
    if not terms:
        return None
    match = _TERMS_RE.match(str(terms).strip())
    if match is None:
        return None
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------


def _compute_hash(entry: dict[str, Any]) -> str:
    """Hash an audit entry, excluding the ``hash`` field itself."""
    canonical = {k: v for k, v in entry.items() if k != "hash"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _head_hash(entries: list[dict[str, Any]]) -> str:
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


def _load_audit_from_file() -> list[dict[str, Any]]:
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

    entries: list[dict[str, Any]] = []
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


def _verify_chain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check sequence continuity, hash linkage and per-entry hashes.

    Returns a list of problems; empty means the chain is intact. Reading the
    log without verifying it is what let a tampered file boot cleanly.
    """
    problems: list[dict[str, Any]] = []
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


def _audit_entries() -> list[dict[str, Any]]:
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


def _persist_audit_entry(entry: dict[str, Any]) -> None:
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


def _resolve_persist() -> Callable[[dict[str, Any]], None]:
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
    payload: dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> dict[str, Any]:
    """Append an entry to the in-memory and durable audit chain.

    The durable write happens first: if it fails, ``ToolError`` propagates and
    neither the in-memory chain nor ``_AUDIT_SEQ`` advances, so a caller that
    treats the failure as fatal leaves no partial state behind.
    """
    global _AUDIT_SEQ
    with _LOCK:
        seq = _AUDIT_SEQ + 1
        entry: dict[str, Any] = {
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

    store: Callable[[], dict[str, dict[str, Any]]]
    id_field: str
    not_found_code: str
    status_field: str = "status"
    updated_field: str = "updated_at"


_ENTITIES: dict[str, _EntitySpec] = {
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
    "rfq": _EntitySpec(
        store=lambda: _RFQS,
        id_field="rfq_id",
        not_found_code="RFQ_NOT_FOUND",
    ),
    "quote": _EntitySpec(
        store=lambda: _QUOTES,
        id_field="quote_id",
        not_found_code="QUOTE_NOT_FOUND",
    ),
    "exception": _EntitySpec(
        store=lambda: _EXCEPTIONS,
        id_field="exception_id",
        not_found_code="EXCEPTION_NOT_FOUND",
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
    from_states: tuple[str, ...],
    to_state: str,
) -> None:
    """Validate a transition for any registered entity without changing it.

    One implementation for invoices, POs, requisitions, RFQs, quotes and
    exceptions. The previous invoice-only helper forced
    ``tools._apply_transition`` to re-implement the same checks for the other
    record types, and the copies could drift.

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
    from_states: tuple[str, ...],
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
    from_states: tuple[str, ...],
    to_state: str,
) -> None:
    """Invoice-specific wrapper kept for existing callers.

    Delegates to ``_check_entity_transition`` so there is exactly one
    implementation of the check.
    """
    _check_entity_transition("invoice", invoice_id, from_states, to_state)


def _transition(
    invoice_id: str,
    from_states: tuple[str, ...],
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
    payload: dict[str, Any],
    correlation_id: str = "",
    check: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
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
#
# The dataset is deliberately wide: every status the UI renders has at least
# one row, and the rows are chosen so the *derived* views have something to
# say. Concretely, the seed is built to keep these invariants true, because
# each one is enforced somewhere in the tool layer:
#
# * ``is_fully_received`` / ``is_fully_invoiced`` (``_purchases``) are
#   consistent with the receipt and invoice sums, so the outstanding table is
#   not lying about a PO.
# * A PO is late only when it is OPEN and its ``expected_delivery`` is a real
#   ISO date in the past (``_followup._is_late``). Only those POs carry
#   ``late_flagged_at`` / ``delay_notified_at``, so the follow-up pass has
#   unflagged work to do and the notify button has a legal target.
# * Every SUBMITTED quote has a future ``valid_until`` and is from a vendor
#   invited on that RFQ, with at most one live quote per vendor
#   (``_check_submit_quote``). An expired quote would make the Award button
#   fail with ``QUOTE_EXPIRED`` on a fresh install.
# * An awarded RFQ points at a real PO whose ``rfq_id`` points back, and
#   ``quantity * unit_price`` equals the PO amount (``award_quote``).
# * An APPROVED approval has ``approver != executor``, so the executor queue
#   can actually settle it (``_execute_payout``).
#
# Dates are relative to the day the process starts, so the late/outstanding
# views stay meaningful instead of drifting into "everything is overdue" as
# the seed ages. ``_EXCEPTIONS`` is deliberately empty: exceptions are opened
# by the detector and the match path, so seeding one would make a fresh
# install look like it already had an unresolved problem.


def _seed_state() -> None:
    """Populate the in-memory state with the baseline dataset."""
    today = datetime.now(timezone.utc).date()

    def _d(offset: int) -> str:
        """An ISO date ``offset`` days from today (negative = past)."""
        return (today + timedelta(days=offset)).isoformat()

    def _ts(offset: int) -> str:
        """An ISO timestamp ``offset`` days from today (negative = past)."""
        return (datetime.now(timezone.utc) + timedelta(days=offset)).isoformat()

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
            "VEND-005": {
                "vendor_id": "VEND-005",
                "name": "Northwind Fasteners",
                "status": "ACTIVE",
                "risk_tier": "LOW",
                "currency": "USD",
                "payment_terms": "NET30",
                "lead_time_days": 10,
            },
            "VEND-006": {
                "vendor_id": "VEND-006",
                "name": "Stark Precision Tools",
                "status": "ACTIVE",
                "risk_tier": "MEDIUM",
                "currency": "USD",
                "payment_terms": "NET60",
                "lead_time_days": 30,
            },
            "VEND-007": {
                "vendor_id": "VEND-007",
                "name": "Soylent Packaging Co",
                "status": "ACTIVE",
                "risk_tier": "LOW",
                "currency": "USD",
                "payment_terms": "NET30",
                "lead_time_days": 12,
            },
            "VEND-008": {
                "vendor_id": "VEND-008",
                "name": "Cyberdyne Systems",
                "status": "ACTIVE",
                "risk_tier": "HIGH",
                "currency": "USD",
                "payment_terms": "NET15",
                "lead_time_days": 3,
            },
            "VEND-009": {
                "vendor_id": "VEND-009",
                "name": "Wayne Fabrication",
                "status": "ACTIVE",
                "risk_tier": "MEDIUM",
                "currency": "USD",
                "payment_terms": "NET45",
                "lead_time_days": 18,
            },
        }
    )
    _POS.update(
        {
            # Late, fully received, fully invoiced - and invoiced twice, so the
            # duplicate detector has a pair to find.
            "PO-1001": {
                "po_id": "PO-1001",
                "vendor_id": "VEND-001",
                "amount": 12500.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": "2024-01-20",
                "requisition_id": "REQ-4001",
                "created_at": _ts(-120),
            },
            "PO-1002": {
                "po_id": "PO-1002",
                "vendor_id": "VEND-002",
                "amount": 800.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": "2024-02-10",
                "requisition_id": None,
                "created_at": _ts(-110),
            },
            # On schedule, partially received: the ordinary in-flight PO.
            "PO-1003": {
                "po_id": "PO-1003",
                "vendor_id": "VEND-001",
                "amount": 4200.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(14),
                "requisition_id": None,
                "created_at": _ts(-30),
            },
            # Late, nothing received, invoice in EXCEPTION. Already flagged and
            # notified, so the follow-up pass skips it.
            "PO-1004": {
                "po_id": "PO-1004",
                "vendor_id": "VEND-002",
                "amount": 9600.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-21),
                "requisition_id": None,
                "created_at": _ts(-60),
                "late_flagged_at": _ts(-20),
                "delay_notified_at": _ts(-19),
                "delay_notified_by": "operator",
            },
            # Closed, fully settled, raised from an awarded RFQ.
            "PO-1005": {
                "po_id": "PO-1005",
                "vendor_id": "VEND-004",
                "amount": 3000.00,
                "currency": "USD",
                "status": PO_STATUS_CLOSED,
                "expected_delivery": _d(-40),
                "requisition_id": None,
                "rfq_id": "RFQ-5003",
                "created_at": _ts(-90),
            },
            "PO-1006": {
                "po_id": "PO-1006",
                "vendor_id": "VEND-003",
                "amount": 1500.00,
                "currency": "USD",
                "status": PO_STATUS_CANCELLED,
                "expected_delivery": _d(-5),
                "requisition_id": None,
                "created_at": _ts(-25),
            },
            # Converted from REQ-4003; nothing received yet.
            "PO-1007": {
                "po_id": "PO-1007",
                "vendor_id": "VEND-005",
                "amount": 1875.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(10),
                "requisition_id": "REQ-4003",
                "created_at": _ts(-12),
            },
            # Large, long lead time, converted from REQ-4008.
            "PO-1008": {
                "po_id": "PO-1008",
                "vendor_id": "VEND-006",
                "amount": 24000.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(45),
                "requisition_id": "REQ-4008",
                "created_at": _ts(-8),
            },
            # Late, fully received, flagged but not yet notified.
            "PO-1009": {
                "po_id": "PO-1009",
                "vendor_id": "VEND-001",
                "amount": 6400.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-3),
                "requisition_id": None,
                "created_at": _ts(-40),
                "late_flagged_at": _ts(-2),
            },
            # Late, partially received.
            "PO-1010": {
                "po_id": "PO-1010",
                "vendor_id": "VEND-002",
                "amount": 3300.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-12),
                "requisition_id": None,
                "created_at": _ts(-50),
            },
            "PO-1011": {
                "po_id": "PO-1011",
                "vendor_id": "VEND-005",
                "amount": 900.00,
                "currency": "USD",
                "status": PO_STATUS_CLOSED,
                "expected_delivery": _d(-20),
                "requisition_id": None,
                "created_at": _ts(-70),
            },
            # High-risk vendor, partially received, large pending approval.
            "PO-1012": {
                "po_id": "PO-1012",
                "vendor_id": "VEND-004",
                "amount": 15000.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(5),
                "requisition_id": None,
                "created_at": _ts(-15),
            },
            # Late, fully received, invoice in EXCEPTION, raised from an award.
            "PO-1013": {
                "po_id": "PO-1013",
                "vendor_id": "VEND-006",
                "amount": 7200.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-30),
                "requisition_id": None,
                "rfq_id": "RFQ-5008",
                "created_at": _ts(-80),
            },
            "PO-1014": {
                "po_id": "PO-1014",
                "vendor_id": "VEND-007",
                "amount": 2100.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(21),
                "requisition_id": None,
                "created_at": _ts(-6),
            },
            # Late, nothing received, high-risk vendor.
            "PO-1015": {
                "po_id": "PO-1015",
                "vendor_id": "VEND-008",
                "amount": 4800.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-7),
                "requisition_id": None,
                "created_at": _ts(-35),
            },
            "PO-1016": {
                "po_id": "PO-1016",
                "vendor_id": "VEND-001",
                "amount": 500.00,
                "currency": "USD",
                "status": PO_STATUS_CLOSED,
                "expected_delivery": _d(-60),
                "requisition_id": None,
                "created_at": _ts(-100),
            },
            "PO-1017": {
                "po_id": "PO-1017",
                "vendor_id": "VEND-009",
                "amount": 8800.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(30),
                "requisition_id": None,
                "created_at": _ts(-4),
            },
            # Fully received, not yet invoiced: the "waiting on the invoice"
            # case, which is the one ``is_fully_invoiced`` exists to name.
            "PO-1018": {
                "po_id": "PO-1018",
                "vendor_id": "VEND-002",
                "amount": 12000.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(2),
                "requisition_id": None,
                "created_at": _ts(-20),
            },
            # Late by a day, fully received, invoice pending approval.
            "PO-1019": {
                "po_id": "PO-1019",
                "vendor_id": "VEND-005",
                "amount": 2600.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(-1),
                "requisition_id": None,
                "created_at": _ts(-28),
            },
            # Fully settled and approved, waiting on the executor.
            "PO-1020": {
                "po_id": "PO-1020",
                "vendor_id": "VEND-006",
                "amount": 18000.00,
                "currency": "USD",
                "status": PO_STATUS_OPEN,
                "expected_delivery": _d(35),
                "requisition_id": None,
                "created_at": _ts(-10),
            },
        }
    )
    _RECEIPTS.update(
        {
            "RCPT-3001": {
                "receipt_id": "RCPT-3001",
                "po_id": "PO-1001",
                "amount": 12500.00,
                "received_at": _ts(-100),
            },
            "RCPT-3002": {
                "receipt_id": "RCPT-3002",
                "po_id": "PO-1002",
                "amount": 800.00,
                "received_at": _ts(-95),
            },
            "RCPT-3003": {
                "receipt_id": "RCPT-3003",
                "po_id": "PO-1003",
                "amount": 2000.00,
                "received_at": _ts(-20),
            },
            "RCPT-3004": {
                "receipt_id": "RCPT-3004",
                "po_id": "PO-1005",
                "amount": 3000.00,
                "received_at": _ts(-85),
            },
            "RCPT-3005": {
                "receipt_id": "RCPT-3005",
                "po_id": "PO-1009",
                "amount": 6400.00,
                "received_at": _ts(-30),
            },
            "RCPT-3006": {
                "receipt_id": "RCPT-3006",
                "po_id": "PO-1010",
                "amount": 1500.00,
                "received_at": _ts(-40),
            },
            "RCPT-3007": {
                "receipt_id": "RCPT-3007",
                "po_id": "PO-1011",
                "amount": 900.00,
                "received_at": _ts(-60),
            },
            "RCPT-3008": {
                "receipt_id": "RCPT-3008",
                "po_id": "PO-1013",
                "amount": 7200.00,
                "received_at": _ts(-70),
            },
            "RCPT-3009": {
                "receipt_id": "RCPT-3009",
                "po_id": "PO-1016",
                "amount": 500.00,
                "received_at": _ts(-90),
            },
            "RCPT-3010": {
                "receipt_id": "RCPT-3010",
                "po_id": "PO-1019",
                "amount": 2600.00,
                "received_at": _ts(-20),
            },
            "RCPT-3011": {
                "receipt_id": "RCPT-3011",
                "po_id": "PO-1014",
                "amount": 700.00,
                "received_at": _ts(-5),
            },
            "RCPT-3012": {
                "receipt_id": "RCPT-3012",
                "po_id": "PO-1012",
                "amount": 5000.00,
                "received_at": _ts(-10),
            },
            "RCPT-3013": {
                "receipt_id": "RCPT-3013",
                "po_id": "PO-1018",
                "amount": 12000.00,
                "received_at": _ts(-15),
            },
            "RCPT-3014": {
                "receipt_id": "RCPT-3014",
                "po_id": "PO-1020",
                "amount": 18000.00,
                "received_at": _ts(-8),
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
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3001"],
                "due_date": _d(-10),
                "created_at": _ts(-100),
                "updated_at": _ts(-99),
            },
            "INV-2002": {
                "invoice_id": "INV-2002",
                "vendor_id": "VEND-002",
                "po_id": "PO-1002",
                "amount": 800.00,
                "currency": "USD",
                "status": "RECEIVED",
                "receipt_ids": ["RCPT-3002"],
                "due_date": _d(20),
                "created_at": _ts(-95),
                "updated_at": _ts(-95),
            },
            "INV-2003": {
                "invoice_id": "INV-2003",
                "vendor_id": "VEND-001",
                "po_id": "PO-1003",
                "amount": 2000.00,
                "currency": "USD",
                "status": "PENDING_APPROVAL",
                "receipt_ids": ["RCPT-3003"],
                "due_date": _d(30),
                "created_at": _ts(-20),
                "updated_at": _ts(-20),
            },
            "INV-2004": {
                "invoice_id": "INV-2004",
                "vendor_id": "VEND-002",
                "po_id": "PO-1004",
                "amount": 9600.00,
                "currency": "USD",
                "status": "EXCEPTION",
                "receipt_ids": [],
                "due_date": _d(-5),
                "created_at": _ts(-55),
                "updated_at": _ts(-54),
            },
            "INV-2005": {
                "invoice_id": "INV-2005",
                "vendor_id": "VEND-004",
                "po_id": "PO-1005",
                "amount": 3000.00,
                "currency": "USD",
                "status": "APPROVED",
                "receipt_ids": ["RCPT-3004"],
                "due_date": _d(-30),
                "created_at": _ts(-85),
                "updated_at": _ts(-84),
            },
            # Second invoice against PO-1001 for the same amount: the duplicate
            # the detector is meant to catch.
            "INV-2006": {
                "invoice_id": "INV-2006",
                "vendor_id": "VEND-001",
                "po_id": "PO-1001",
                "amount": 12500.00,
                "currency": "USD",
                "status": "EXCEPTION",
                "receipt_ids": ["RCPT-3001"],
                "due_date": _d(-10),
                "created_at": _ts(-98),
                "updated_at": _ts(-97),
            },
            "INV-2007": {
                "invoice_id": "INV-2007",
                "vendor_id": "VEND-001",
                "po_id": "PO-1009",
                "amount": 6400.00,
                "currency": "USD",
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3005"],
                "due_date": _d(5),
                "created_at": _ts(-30),
                "updated_at": _ts(-29),
            },
            "INV-2008": {
                "invoice_id": "INV-2008",
                "vendor_id": "VEND-002",
                "po_id": "PO-1010",
                "amount": 1500.00,
                "currency": "USD",
                "status": "RECEIVED",
                "receipt_ids": ["RCPT-3006"],
                "due_date": _d(15),
                "created_at": _ts(-40),
                "updated_at": _ts(-40),
            },
            "INV-2009": {
                "invoice_id": "INV-2009",
                "vendor_id": "VEND-005",
                "po_id": "PO-1011",
                "amount": 900.00,
                "currency": "USD",
                "status": "APPROVED",
                "receipt_ids": ["RCPT-3007"],
                "due_date": _d(-12),
                "created_at": _ts(-60),
                "updated_at": _ts(-59),
            },
            "INV-2010": {
                "invoice_id": "INV-2010",
                "vendor_id": "VEND-006",
                "po_id": "PO-1013",
                "amount": 7200.00,
                "currency": "USD",
                "status": "EXCEPTION",
                "receipt_ids": ["RCPT-3008"],
                "due_date": _d(-20),
                "created_at": _ts(-70),
                "updated_at": _ts(-69),
            },
            "INV-2011": {
                "invoice_id": "INV-2011",
                "vendor_id": "VEND-001",
                "po_id": "PO-1016",
                "amount": 500.00,
                "currency": "USD",
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3009"],
                "due_date": _d(-40),
                "created_at": _ts(-90),
                "updated_at": _ts(-89),
            },
            "INV-2012": {
                "invoice_id": "INV-2012",
                "vendor_id": "VEND-005",
                "po_id": "PO-1019",
                "amount": 2600.00,
                "currency": "USD",
                "status": "PENDING_APPROVAL",
                "receipt_ids": ["RCPT-3010"],
                "due_date": _d(10),
                "created_at": _ts(-20),
                "updated_at": _ts(-20),
            },
            "INV-2013": {
                "invoice_id": "INV-2013",
                "vendor_id": "VEND-007",
                "po_id": "PO-1014",
                "amount": 700.00,
                "currency": "USD",
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3011"],
                "due_date": _d(12),
                "created_at": _ts(-5),
                "updated_at": _ts(-4),
            },
            "INV-2014": {
                "invoice_id": "INV-2014",
                "vendor_id": "VEND-004",
                "po_id": "PO-1012",
                "amount": 5000.00,
                "currency": "USD",
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3012"],
                "due_date": _d(18),
                "created_at": _ts(-10),
                "updated_at": _ts(-9),
            },
            "INV-2015": {
                "invoice_id": "INV-2015",
                "vendor_id": "VEND-006",
                "po_id": "PO-1020",
                "amount": 18000.00,
                "currency": "USD",
                "status": "MATCHED",
                "receipt_ids": ["RCPT-3014"],
                "due_date": _d(35),
                "created_at": _ts(-8),
                "updated_at": _ts(-7),
            },
        }
    )
    _APPROVALS.update(
        {
            "APR-8001": {
                "approval_id": "APR-8001",
                "vendor_id": "VEND-001",
                "po_id": "PO-1003",
                "invoice_id": "INV-2003",
                "amount": 2000.00,
                "status": "PENDING",
                "created_at": _ts(-20),
                "updated_at": _ts(-20),
            },
            "APR-8002": {
                "approval_id": "APR-8002",
                "vendor_id": "VEND-002",
                "po_id": "PO-1002",
                "invoice_id": "INV-2002",
                "amount": 800.00,
                "status": "APPROVED",
                "approver": "operator",
                "approved_at": _ts(-90),
                "created_at": _ts(-95),
                "updated_at": _ts(-90),
            },
            "APR-8003": {
                "approval_id": "APR-8003",
                "vendor_id": "VEND-004",
                "po_id": "PO-1005",
                "invoice_id": "INV-2005",
                "amount": 3000.00,
                "status": "EXECUTED",
                "approver": "operator",
                "approved_at": _ts(-84),
                "executor": "executor",
                "executed_at": _ts(-83),
                "created_at": _ts(-85),
                "updated_at": _ts(-83),
            },
            "APR-8004": {
                "approval_id": "APR-8004",
                "vendor_id": "VEND-005",
                "po_id": "PO-1019",
                "invoice_id": "INV-2012",
                "amount": 2600.00,
                "status": "PENDING",
                "created_at": _ts(-20),
                "updated_at": _ts(-20),
            },
            "APR-8005": {
                "approval_id": "APR-8005",
                "vendor_id": "VEND-001",
                "po_id": "PO-1009",
                "invoice_id": "INV-2007",
                "amount": 6400.00,
                "status": "PENDING",
                "created_at": _ts(-30),
                "updated_at": _ts(-30),
            },
            "APR-8006": {
                "approval_id": "APR-8006",
                "vendor_id": "VEND-005",
                "po_id": "PO-1011",
                "invoice_id": "INV-2009",
                "amount": 900.00,
                "status": "APPROVED",
                "approver": "operator",
                "approved_at": _ts(-55),
                "created_at": _ts(-60),
                "updated_at": _ts(-55),
            },
            "APR-8007": {
                "approval_id": "APR-8007",
                "vendor_id": "VEND-001",
                "po_id": "PO-1016",
                "invoice_id": "INV-2011",
                "amount": 500.00,
                "status": "EXECUTED",
                "approver": "operator",
                "approved_at": _ts(-89),
                "executor": "executor",
                "executed_at": _ts(-88),
                "created_at": _ts(-90),
                "updated_at": _ts(-88),
            },
            "APR-8008": {
                "approval_id": "APR-8008",
                "vendor_id": "VEND-004",
                "po_id": "PO-1012",
                "invoice_id": "INV-2014",
                "amount": 5000.00,
                "status": "PENDING",
                "created_at": _ts(-10),
                "updated_at": _ts(-10),
            },
            "APR-8009": {
                "approval_id": "APR-8009",
                "vendor_id": "VEND-007",
                "po_id": "PO-1014",
                "invoice_id": "INV-2013",
                "amount": 700.00,
                "status": "PENDING",
                "created_at": _ts(-5),
                "updated_at": _ts(-5),
            },
            "APR-8010": {
                "approval_id": "APR-8010",
                "vendor_id": "VEND-006",
                "po_id": "PO-1020",
                "invoice_id": "INV-2015",
                "amount": 18000.00,
                "status": "APPROVED",
                "approver": "operator",
                "approved_at": _ts(-7),
                "created_at": _ts(-8),
                "updated_at": _ts(-7),
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
                "created_at": _ts(-121),
                "updated_at": _ts(-120),
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
                "created_at": _ts(-50),
                "updated_at": _ts(-50),
            },
            "REQ-4003": {
                "requisition_id": "REQ-4003",
                "description": "Conveyor belt spares",
                "vendor_id": "VEND-005",
                "po_id": "PO-1007",
                "quantity": 25,
                "amount": 1875.00,
                "currency": "USD",
                "status": REQ_STATUS_CONVERTED,
                "requested_by": "operator",
                "created_at": _ts(-13),
                "updated_at": _ts(-12),
            },
            "REQ-4004": {
                "requisition_id": "REQ-4004",
                "description": "Obsolete gasket order",
                "vendor_id": "VEND-002",
                "po_id": None,
                "quantity": 10,
                "amount": 200.00,
                "currency": "USD",
                "status": REQ_STATUS_CANCELLED,
                "requested_by": "operator",
                "created_at": _ts(-45),
                "updated_at": _ts(-44),
            },
            "REQ-4005": {
                "requisition_id": "REQ-4005",
                "description": "Safety gloves, Q3 restock",
                "vendor_id": "VEND-007",
                "po_id": None,
                "quantity": 200,
                "amount": 2400.00,
                "currency": "USD",
                "status": REQ_STATUS_OPEN,
                "requested_by": "operator",
                "created_at": _ts(-9),
                "updated_at": _ts(-9),
            },
            "REQ-4006": {
                "requisition_id": "REQ-4006",
                "description": "Hydraulic hose assemblies",
                "vendor_id": "VEND-001",
                "po_id": None,
                "quantity": 30,
                "amount": 3600.00,
                "currency": "USD",
                "status": REQ_STATUS_OPEN,
                "requested_by": "operator",
                "created_at": _ts(-7),
                "updated_at": _ts(-7),
            },
            "REQ-4007": {
                "requisition_id": "REQ-4007",
                "description": "Pallet racking, warehouse B",
                "vendor_id": "VEND-009",
                "po_id": None,
                "quantity": 12,
                "amount": 5400.00,
                "currency": "USD",
                "status": REQ_STATUS_OPEN,
                "requested_by": "operator",
                "created_at": _ts(-5),
                "updated_at": _ts(-5),
            },
            "REQ-4008": {
                "requisition_id": "REQ-4008",
                "description": "Precision tooling package",
                "vendor_id": "VEND-006",
                "po_id": "PO-1008",
                "quantity": 1,
                "amount": 24000.00,
                "currency": "USD",
                "status": REQ_STATUS_CONVERTED,
                "requested_by": "operator",
                "created_at": _ts(-9),
                "updated_at": _ts(-8),
            },
            "REQ-4009": {
                "requisition_id": "REQ-4009",
                "description": "Office supplies",
                "vendor_id": None,
                "po_id": None,
                "quantity": 50,
                "amount": 350.00,
                "currency": "USD",
                "status": REQ_STATUS_OPEN,
                "requested_by": "operator",
                "created_at": _ts(-3),
                "updated_at": _ts(-3),
            },
            "REQ-4010": {
                "requisition_id": "REQ-4010",
                "description": "Forklift maintenance",
                "vendor_id": "VEND-003",
                "po_id": None,
                "quantity": 1,
                "amount": 900.00,
                "currency": "USD",
                "status": REQ_STATUS_CANCELLED,
                "requested_by": "operator",
                "created_at": _ts(-22),
                "updated_at": _ts(-21),
            },
        }
    )
    _RFQS.update(
        {
            "RFQ-5001": {
                "rfq_id": "RFQ-5001",
                "description": "Hydraulic fittings, Q2 restock",
                "quantity": 120,
                "currency": "USD",
                "vendor_ids": ["VEND-001", "VEND-002", "VEND-004"],
                "due_date": _d(7),
                "status": RFQ_STATUS_OPEN,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-14),
                "updated_at": _ts(-14),
            },
            "RFQ-5002": {
                "rfq_id": "RFQ-5002",
                "description": "Gasket sets, annual contract",
                "quantity": 50,
                "currency": "USD",
                "vendor_ids": ["VEND-001", "VEND-005"],
                "due_date": _d(-3),
                "status": RFQ_STATUS_CLOSED,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-40),
                "updated_at": _ts(-3),
            },
            "RFQ-5003": {
                "rfq_id": "RFQ-5003",
                "description": "Precision tooling, awarded",
                "quantity": 50,
                "currency": "USD",
                "vendor_ids": ["VEND-004", "VEND-006"],
                "due_date": _d(-45),
                "status": RFQ_STATUS_AWARDED,
                "awarded_quote_id": "QTE-6005",
                "po_id": "PO-1005",
                "created_at": _ts(-90),
                "updated_at": _ts(-87),
            },
            # Open, one invited vendor has not quoted yet.
            "RFQ-5004": {
                "rfq_id": "RFQ-5004",
                "description": "Fastener restock, Q3",
                "quantity": 400,
                "currency": "USD",
                "vendor_ids": ["VEND-001", "VEND-002", "VEND-005"],
                "due_date": _d(14),
                "status": RFQ_STATUS_OPEN,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-6),
                "updated_at": _ts(-6),
            },
            # Open with no quotes at all: the empty-quotes state.
            "RFQ-5005": {
                "rfq_id": "RFQ-5005",
                "description": "Pump housings, urgent",
                "quantity": 20,
                "currency": "USD",
                "vendor_ids": ["VEND-004", "VEND-006"],
                "due_date": _d(3),
                "status": RFQ_STATUS_OPEN,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-2),
                "updated_at": _ts(-2),
            },
            # Closed but awardable: closing stops new quotes, not the award.
            "RFQ-5006": {
                "rfq_id": "RFQ-5006",
                "description": "Valve assemblies, annual",
                "quantity": 150,
                "currency": "USD",
                "vendor_ids": ["VEND-001", "VEND-002"],
                "due_date": _d(-10),
                "status": RFQ_STATUS_CLOSED,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-35),
                "updated_at": _ts(-10),
            },
            "RFQ-5007": {
                "rfq_id": "RFQ-5007",
                "description": "Obsolete sensor buy",
                "quantity": 60,
                "currency": "USD",
                "vendor_ids": ["VEND-005"],
                "due_date": _d(-20),
                "status": RFQ_STATUS_CANCELLED,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-50),
                "updated_at": _ts(-20),
            },
            "RFQ-5008": {
                "rfq_id": "RFQ-5008",
                "description": "Precision tooling, Q1",
                "quantity": 48,
                "currency": "USD",
                "vendor_ids": ["VEND-002", "VEND-006"],
                "due_date": _d(-60),
                "status": RFQ_STATUS_AWARDED,
                "awarded_quote_id": "QTE-6010",
                "po_id": "PO-1013",
                "created_at": _ts(-80),
                "updated_at": _ts(-77),
            },
            "RFQ-5009": {
                "rfq_id": "RFQ-5009",
                "description": "Packaging film, Q3",
                "quantity": 500,
                "currency": "USD",
                "vendor_ids": ["VEND-001", "VEND-005", "VEND-007"],
                "due_date": _d(21),
                "status": RFQ_STATUS_OPEN,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-4),
                "updated_at": _ts(-4),
            },
            # Open and past its due date: the overdue-RFQ case.
            "RFQ-5010": {
                "rfq_id": "RFQ-5010",
                "description": "Bearing sets, overdue",
                "quantity": 30,
                "currency": "USD",
                "vendor_ids": ["VEND-002", "VEND-004"],
                "due_date": _d(-2),
                "status": RFQ_STATUS_OPEN,
                "awarded_quote_id": None,
                "po_id": None,
                "created_at": _ts(-25),
                "updated_at": _ts(-25),
            },
        }
    )
    # ``"SUBMITTED"`` is a literal on purpose. ``_state`` must not import
    # ``_codes`` (see the module docstring), and the local
    # ``QUOTE_STATUS_RECEIVED`` is the stale value that made the Sourcing
    # drawer read ``unit_price`` off a record that had ``amount`` and render
    # ``$NaN``. ``_tools_sourcing.submit_quote`` writes ``"SUBMITTED"``, so the
    # seed has to agree with it or the Award button never appears.
    #
    # Every SUBMITTED quote has a future ``valid_until`` and is from a vendor
    # invited on that RFQ, with at most one live quote per vendor:
    # ``_check_submit_quote`` rejects an expired quote with ``QUOTE_EXPIRED``
    # and a second live quote from the same vendor, so a past date or a
    # duplicate vendor here would make the Award button fail on a fresh
    # install.
    _QUOTES.update(
        {
            "QTE-6001": {
                "quote_id": "QTE-6001",
                "rfq_id": "RFQ-5001",
                "vendor_id": "VEND-001",
                "unit_price": 120.00,
                "currency": "USD",
                "lead_time_days": 7,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-12),
                "updated_at": _ts(-12),
            },
            "QTE-6002": {
                "quote_id": "QTE-6002",
                "rfq_id": "RFQ-5001",
                "vendor_id": "VEND-002",
                "unit_price": 115.00,
                "currency": "USD",
                "lead_time_days": 14,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-11),
                "updated_at": _ts(-11),
            },
            "QTE-6003": {
                "quote_id": "QTE-6003",
                "rfq_id": "RFQ-5001",
                "vendor_id": "VEND-004",
                "unit_price": 110.00,
                "currency": "USD",
                "lead_time_days": 5,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-10),
                "updated_at": _ts(-10),
            },
            "QTE-6004": {
                "quote_id": "QTE-6004",
                "rfq_id": "RFQ-5002",
                "vendor_id": "VEND-001",
                "unit_price": 20.00,
                "currency": "USD",
                "lead_time_days": 7,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-38),
                "updated_at": _ts(-38),
            },
            "QTE-6005": {
                "quote_id": "QTE-6005",
                "rfq_id": "RFQ-5003",
                "vendor_id": "VEND-004",
                "unit_price": 60.00,
                "currency": "USD",
                "lead_time_days": 5,
                "valid_until": _d(30),
                "status": QUOTE_STATUS_AWARDED,
                "submitted_at": _ts(-88),
                "awarded_at": _ts(-87),
                "updated_at": _ts(-87),
            },
            "QTE-6006": {
                "quote_id": "QTE-6006",
                "rfq_id": "RFQ-5004",
                "vendor_id": "VEND-001",
                "unit_price": 45.50,
                "currency": "USD",
                "lead_time_days": 7,
                "valid_until": _d(45),
                "status": "SUBMITTED",
                "submitted_at": _ts(-5),
                "updated_at": _ts(-5),
            },
            "QTE-6007": {
                "quote_id": "QTE-6007",
                "rfq_id": "RFQ-5004",
                "vendor_id": "VEND-005",
                "unit_price": 42.00,
                "currency": "USD",
                "lead_time_days": 10,
                "valid_until": _d(45),
                "status": "SUBMITTED",
                "submitted_at": _ts(-4),
                "updated_at": _ts(-4),
            },
            "QTE-6008": {
                "quote_id": "QTE-6008",
                "rfq_id": "RFQ-5006",
                "vendor_id": "VEND-001",
                "unit_price": 88.00,
                "currency": "USD",
                "lead_time_days": 7,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-33),
                "updated_at": _ts(-33),
            },
            "QTE-6009": {
                "quote_id": "QTE-6009",
                "rfq_id": "RFQ-5006",
                "vendor_id": "VEND-002",
                "unit_price": 91.00,
                "currency": "USD",
                "lead_time_days": 14,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-32),
                "updated_at": _ts(-32),
            },
            "QTE-6010": {
                "quote_id": "QTE-6010",
                "rfq_id": "RFQ-5008",
                "vendor_id": "VEND-006",
                "unit_price": 150.00,
                "currency": "USD",
                "lead_time_days": 30,
                "valid_until": _d(30),
                "status": QUOTE_STATUS_AWARDED,
                "submitted_at": _ts(-78),
                "awarded_at": _ts(-77),
                "updated_at": _ts(-77),
            },
            "QTE-6011": {
                "quote_id": "QTE-6011",
                "rfq_id": "RFQ-5009",
                "vendor_id": "VEND-007",
                "unit_price": 33.25,
                "currency": "USD",
                "lead_time_days": 12,
                "valid_until": _d(60),
                "status": "SUBMITTED",
                "submitted_at": _ts(-3),
                "updated_at": _ts(-3),
            },
            "QTE-6012": {
                "quote_id": "QTE-6012",
                "rfq_id": "RFQ-5010",
                "vendor_id": "VEND-002",
                "unit_price": 210.00,
                "currency": "USD",
                "lead_time_days": 14,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-24),
                "updated_at": _ts(-24),
            },
            "QTE-6013": {
                "quote_id": "QTE-6013",
                "rfq_id": "RFQ-5010",
                "vendor_id": "VEND-004",
                "unit_price": 205.00,
                "currency": "USD",
                "lead_time_days": 5,
                "valid_until": _d(30),
                "status": "SUBMITTED",
                "submitted_at": _ts(-23),
                "updated_at": _ts(-23),
            },
        }
    )
    # ``valid_until`` is relative rather than the old hardcoded "2025-12-31":
    # a fixed date silently expires every quote once the calendar passes it,
    # and ``compare_vendors`` filters on expiry. VQ-7019 is deliberately
    # expired so the expiry path has a row to exercise.
    _VENDOR_QUOTES.update(
        {
            "VQ-7001": {
                "quote_id": "VQ-7001",
                "vendor_id": "VEND-001",
                "sku": "SKU-HYD-001",
                "unit_price": 120.00,
                "currency": "USD",
                "lead_time_days": 7,
                "moq": 10,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7002": {
                "quote_id": "VQ-7002",
                "vendor_id": "VEND-002",
                "sku": "SKU-HYD-001",
                "unit_price": 115.00,
                "currency": "USD",
                "lead_time_days": 14,
                "moq": 20,
                "valid_until": _d(180),
                "payment_terms": "NET45",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7003": {
                "quote_id": "VQ-7003",
                "vendor_id": "VEND-004",
                "sku": "SKU-HYD-001",
                "unit_price": 110.00,
                "currency": "USD",
                "lead_time_days": 5,
                "moq": 5,
                "valid_until": _d(180),
                "payment_terms": "NET15",
                "risk_tier": "HIGH",
                "status": "ACTIVE",
            },
            "VQ-7004": {
                "quote_id": "VQ-7004",
                "vendor_id": "VEND-001",
                "sku": "SKU-GSK-002",
                "unit_price": 20.00,
                "currency": "USD",
                "lead_time_days": 7,
                "moq": 10,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7005": {
                "quote_id": "VQ-7005",
                "vendor_id": "VEND-002",
                "sku": "SKU-GSK-002",
                "unit_price": 18.00,
                "currency": "USD",
                "lead_time_days": 14,
                "moq": 20,
                "valid_until": _d(180),
                "payment_terms": "NET45",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7006": {
                "quote_id": "VQ-7006",
                "vendor_id": "VEND-001",
                "sku": "SKU-FST-003",
                "unit_price": 0.45,
                "currency": "USD",
                "lead_time_days": 7,
                "moq": 500,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7007": {
                "quote_id": "VQ-7007",
                "vendor_id": "VEND-005",
                "sku": "SKU-FST-003",
                "unit_price": 0.42,
                "currency": "USD",
                "lead_time_days": 10,
                "moq": 1000,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7008": {
                "quote_id": "VQ-7008",
                "vendor_id": "VEND-007",
                "sku": "SKU-FST-003",
                "unit_price": 0.48,
                "currency": "USD",
                "lead_time_days": 12,
                "moq": 250,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7009": {
                "quote_id": "VQ-7009",
                "vendor_id": "VEND-004",
                "sku": "SKU-PMP-004",
                "unit_price": 890.00,
                "currency": "USD",
                "lead_time_days": 5,
                "moq": 1,
                "valid_until": _d(180),
                "payment_terms": "NET15",
                "risk_tier": "HIGH",
                "status": "ACTIVE",
            },
            "VQ-7010": {
                "quote_id": "VQ-7010",
                "vendor_id": "VEND-006",
                "sku": "SKU-PMP-004",
                "unit_price": 850.00,
                "currency": "USD",
                "lead_time_days": 30,
                "moq": 2,
                "valid_until": _d(180),
                "payment_terms": "NET60",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7011": {
                "quote_id": "VQ-7011",
                "vendor_id": "VEND-001",
                "sku": "SKU-VLV-005",
                "unit_price": 88.00,
                "currency": "USD",
                "lead_time_days": 7,
                "moq": 10,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7012": {
                "quote_id": "VQ-7012",
                "vendor_id": "VEND-002",
                "sku": "SKU-VLV-005",
                "unit_price": 91.00,
                "currency": "USD",
                "lead_time_days": 14,
                "moq": 20,
                "valid_until": _d(180),
                "payment_terms": "NET45",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7013": {
                "quote_id": "VQ-7013",
                "vendor_id": "VEND-007",
                "sku": "SKU-PKG-006",
                "unit_price": 33.25,
                "currency": "USD",
                "lead_time_days": 12,
                "moq": 100,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7014": {
                "quote_id": "VQ-7014",
                "vendor_id": "VEND-005",
                "sku": "SKU-PKG-006",
                "unit_price": 35.00,
                "currency": "USD",
                "lead_time_days": 10,
                "moq": 50,
                "valid_until": _d(180),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
            "VQ-7015": {
                "quote_id": "VQ-7015",
                "vendor_id": "VEND-002",
                "sku": "SKU-BRG-007",
                "unit_price": 210.00,
                "currency": "USD",
                "lead_time_days": 14,
                "moq": 5,
                "valid_until": _d(180),
                "payment_terms": "NET45",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7016": {
                "quote_id": "VQ-7016",
                "vendor_id": "VEND-004",
                "sku": "SKU-BRG-007",
                "unit_price": 205.00,
                "currency": "USD",
                "lead_time_days": 5,
                "moq": 5,
                "valid_until": _d(180),
                "payment_terms": "NET15",
                "risk_tier": "HIGH",
                "status": "ACTIVE",
            },
            "VQ-7017": {
                "quote_id": "VQ-7017",
                "vendor_id": "VEND-009",
                "sku": "SKU-RCK-008",
                "unit_price": 450.00,
                "currency": "USD",
                "lead_time_days": 18,
                "moq": 1,
                "valid_until": _d(180),
                "payment_terms": "NET45",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            "VQ-7018": {
                "quote_id": "VQ-7018",
                "vendor_id": "VEND-006",
                "sku": "SKU-RCK-008",
                "unit_price": 470.00,
                "currency": "USD",
                "lead_time_days": 30,
                "moq": 1,
                "valid_until": _d(180),
                "payment_terms": "NET60",
                "risk_tier": "MEDIUM",
                "status": "ACTIVE",
            },
            # Deliberately expired: exercises the expiry filter in
            # ``compare_vendors`` without breaking anything else.
            "VQ-7019": {
                "quote_id": "VQ-7019",
                "vendor_id": "VEND-001",
                "sku": "SKU-EXP-009",
                "unit_price": 50.00,
                "currency": "USD",
                "lead_time_days": 7,
                "moq": 10,
                "valid_until": _d(-10),
                "payment_terms": "NET30",
                "risk_tier": "LOW",
                "status": "ACTIVE",
            },
        }
    )
    # ``_EXCEPTIONS`` is deliberately empty: exceptions are opened by the
    # detector and the match path, so seeding one would make a fresh install
    # look like it already had an unresolved problem.


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


def _reset_phase0_state() -> None:
    """Clear the Phase 0 stores: RFQs, quotes, stock and vendor quotes.

    Called by ``_reset_state`` so a test reset covers them too. Kept as a
    separate function so a caller that only wants to clear the Phase 0
    stores can do so without touching the rest of the state.
    """
    with _LOCK:
        _RFQS.clear()
        _QUOTES.clear()
        _STOCK.clear()
        _VENDOR_QUOTES.clear()


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
        _EXCEPTIONS.clear()
        _reset_phase0_state()
        _AUDIT = []
        _AUDIT_SEQ = 0
        _truncate_audit_log()
        _seed_state()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_seed_state()
_recover_audit_state()
