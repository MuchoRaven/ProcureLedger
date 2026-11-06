"""
tools.py
========
ProcureLedger - tool layer.

This module is the only place that touches invoice, approval and audit state.
``agent.py`` reaches it exclusively through the default-deny ``_DISPATCH``
allowlist, so any public function here that is not in ``ALLOWED_TOOLS`` stays
app-layer only.

App-layer only (never exposed to the model)
-------------------------------------------
* ``ingest_invoice``         - creates invoice records from the ERP feed.
* ``create_purchase_order``  - creates PO records from the ERP feed.
* ``update_po_status``       - moves a PO out of ``OPEN`` (close/cancel).
* ``record_goods_receipt``   - records a goods receipt against an open PO.
* ``approve_payout``         - records a human approval decision.
* ``_execute_payout``        - settles an approved payout. Private on purpose.

Sandbox support
---------------
``sandbox.dispatch`` re-decides every tool call on live state and applies the
verified proposal through ``_snapshot_for`` and the ``_apply_*`` helpers below.
Those helpers delegate to the public tool functions, so validation and audit
logic is not duplicated.

Concurrency
-----------
State lives in module globals guarded by ``_LOCK``. Run the server with a
single worker (``uvicorn ... --workers 1``); the audit chain is process-local.
``app.lifespan`` enforces this with an exclusive lock on
``<AUDIT_LOG_PATH>.lock`` so a second worker fails at boot instead of forking
the chain.

Persistence and replay
----------------------
``config.AUDIT_LOG_PATH`` (``audit_log.jsonl``) is the source of truth for the
audit chain. Every link is appended as one canonical JSON object per line and
``fsync``-ed before the tool call returns. On import, ``_recover_audit_state``
loads that file back into memory *and replays it* to rebuild ``_INVOICES``,
``_APPROVALS``, ``_POS`` and ``_RECEIPTS``. The chain is durable; the dicts it
describes are not, so without replay a restart resets every invoice to
``RECEIVED`` and a paid invoice can be matched and paid again. Replay is
at-least-once and safe because every transition is status-guarded: a link that
would move an invoice backwards is skipped with a warning rather than applied.

``verify_audit_chain`` re-verifies the on-disk chain by default
(``from_file=True``); pass ``from_file=False`` to check the in-memory copy
instead.

Ordering (audit before mutation)
--------------------------------
Every mutating tool routes its state change through ``_commit``. ``_commit``
writes the audit link to disk *first* and only then applies the mutation, all
under ``_LOCK``. A failed write (``ToolError("AUDIT_WRITE_FAILED")``) therefore
leaves business state untouched, so the caller can retry instead of being told
the call failed while the mutation is already durable and unrecorded. If the
mutation itself raises (it should not, since callers validate first), the
in-memory chain and the on-disk link are rolled back so memory and disk stay
consistent.

Source labelling
----------------
Every entry carries ``source``: ``"tool"`` for links the control plane writes,
``"agent"`` for model reasoning notes. ``log_audit_event`` refuses the
tool-authored event types when ``source="agent"``, so a model note cannot
masquerade as a real approval gate or settlement in the chain.

Filesystem failures
-------------------
``_persist_audit`` and ``_load_audit_from_file`` translate ``OSError`` into
``ToolError`` (``AUDIT_WRITE_FAILED`` / ``AUDIT_LOG_UNREADABLE``) so a disk
problem surfaces as a structured error instead of an unhandled 500. The
``_apply_*`` helpers and ``agent._audit`` already catch ``ToolError``, so a
failed write can never leave a dangling ``TOOL_CALL_STARTED`` link.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

import config

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_HASH = "0" * 64
AMOUNT_TOLERANCE = 0.01

# Purchase-order lifecycle. A PO is created ``OPEN`` and may only be moved out
# of ``OPEN`` (to ``CLOSED`` or ``CANCELLED``) by ``update_po_status``.
PO_STATUS_OPEN = "OPEN"
PO_STATUS_CLOSED = "CLOSED"
PO_STATUS_CANCELLED = "CANCELLED"
_PO_ALLOWED_TARGETS = frozenset({PO_STATUS_CLOSED, PO_STATUS_CANCELLED})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Raised by ``_fail`` for states the caller should never reach."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> None:
    """Abort the current tool call with a structured error code."""
    raise ToolError(code, detail)


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    ``sub`` is the identity recorded in the audit chain.
    ``sandbox._build_request`` serialises ``sub``/``roles``/``auth_method`` via
    ``getattr``, so this is the type the sandbox envelope carries. It lives
    here rather than in ``app.py`` because ``agent.py`` already imports
    ``tools``; importing ``app`` would be circular.
    """

    sub: str
    roles: FrozenSet[str] = field(default_factory=frozenset)
    auth_method: str = "bearer"

    def has_role(self, role: str) -> bool:
        return role in self.roles


# Event types written by the tool layer. ``log_audit_event`` is model-facing, so
# a model note must not be able to masquerade as a real gate or settlement in
# the chain. The control plane writes these with ``source="tool"`` and bypasses
# the reservation.
_RESERVED_EVENT_TYPES = frozenset(
    {
        "INVOICE_INGESTED",
        "PO_CREATED",
        "PO_STATUS_UPDATED",
        "GOODS_RECEIPT_RECORDED",
        "THREE_WAY_MATCH",
        "PAYOUT_APPROVAL_REQUESTED",
        "PAYOUT_APPROVED",
        "PAYOUT_EXECUTED",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_COMPLETED",
    }
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()

_VENDORS: Dict[str, Dict[str, Any]] = {
    "VEND-001": {
        "vendor_id": "VEND-001",
        "name": "Northwind Components",
        "status": "ACTIVE",
        "risk_tier": "LOW",
        "payment_terms": "NET30",
    },
    "VEND-002": {
        "vendor_id": "VEND-002",
        "name": "Cobalt Freight",
        "status": "ACTIVE",
        "risk_tier": "HIGH",
        "payment_terms": "NET15",
    },
    "VEND-003": {
        "vendor_id": "VEND-003",
        "name": "Apex Industrial",
        "status": "INACTIVE",
        "risk_tier": "LOW",
        "payment_terms": "NET45",
    },
}

# Seed purchase orders. ``_POS`` is the live, mutable store; ``_SEED_POS`` is
# the immutable literal that ``_reset_business_state`` restores.
_SEED_POS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "po_id": "PO-1001",
        "vendor_id": "VEND-001",
        "amount": 12500.00,
        "currency": "USD",
        "status": "OPEN",
        "expected_delivery_date": "2024-05-10",
    },
    "PO-1002": {
        "po_id": "PO-1002",
        "vendor_id": "VEND-002",
        "amount": 8000.00,
        "currency": "USD",
        "status": "OPEN",
        "expected_delivery_date": "2024-05-12",
    },
    "PO-1003": {
        "po_id": "PO-1003",
        "vendor_id": "VEND-001",
        "amount": 3000.00,
        "currency": "USD",
        "status": "OPEN",
        "expected_delivery_date": "2024-05-15",
    },
}

_POS: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _SEED_POS.items()}

# Seed goods receipts, keyed by ``po_id`` (one receipt per PO in this model).
_SEED_RECEIPTS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "po_id": "PO-1001",
        "amount": 12500.00,
        "received_at": "2024-05-02T09:00:00Z",
    },
    "PO-1002": {
        "po_id": "PO-1002",
        "amount": 8000.00,
        "received_at": "2024-05-03T11:30:00Z",
    },
    "PO-1003": {
        "po_id": "PO-1003",
        "amount": 2500.00,
        "received_at": "2024-05-04T14:15:00Z",
    },
}

_RECEIPTS: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _SEED_RECEIPTS.items()}

_INVOICES: Dict[str, Dict[str, Any]] = {
    "INV-2001": {
        "invoice_id": "INV-2001",
        "vendor_id": "VEND-001",
        "po_id": "PO-1001",
        "amount": 12500.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:00:00Z",
        "updated_at": "2024-05-05T08:00:00Z",
    },
    "INV-2002": {
        "invoice_id": "INV-2002",
        "vendor_id": "VEND-002",
        "po_id": "PO-1002",
        "amount": 8400.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:05:00Z",
        "updated_at": "2024-05-05T08:05:00Z",
    },
    "INV-2003": {
        "invoice_id": "INV-2003",
        "vendor_id": "VEND-001",
        "po_id": "PO-1003",
        "amount": 3000.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:10:00Z",
        "updated_at": "2024-05-05T08:10:00Z",
    },
}

# Snapshot of the seed invoices so ``_reset_state`` can drop records added by
# ``ingest_invoice`` instead of leaking them into the next test.
_SEED_INVOICE_IDS = frozenset(_INVOICES.keys())

_APPROVALS: Dict[str, Dict[str, Any]] = {}

_AUDIT: List[Dict[str, Any]] = []
_AUDIT_SEQ = 0
_AUDIT_RECOVERY_ERROR = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """UTC timestamp, millisecond precision, ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _valid_date(value: Any) -> bool:
    """True if ``value`` is a non-empty ISO ``YYYY-MM-DD`` date string."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _canonical(obj: Any) -> str:
    """Deterministic JSON encoding used for hashing and for the audit log."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(entry: Dict[str, Any]) -> str:
    """Hash the immutable fields of an audit entry (``hash`` excluded).

    ``source`` is included only when present, so entries written before the
    field existed still verify. New entries always carry it.
    """
    body = {
        "seq": entry["seq"],
        "ts": entry["ts"],
        "event_type": entry["event_type"],
        "payload": entry["payload"],
        "correlation_id": entry["correlation_id"],
        "prev_hash": entry["prev_hash"],
    }
    if "source" in entry:
        body["source"] = entry["source"]
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _head_hash(entries: List[Dict[str, Any]]) -> str:
    """Return the hash of the last entry, tolerating a malformed tail.

    ``verify_audit_chain`` reports divergence on a malformed entry; it must not
    itself raise ``KeyError`` when the last entry is missing ``hash``.
    """
    if not entries:
        return GENESIS_HASH
    last = entries[-1]
    if isinstance(last, dict):
        return last.get("hash") or GENESIS_HASH
    return GENESIS_HASH


def _persist_audit(entry: Dict[str, Any]) -> None:
    """Append one canonical JSON line to ``config.AUDIT_LOG_PATH`` and fsync it.

    The record is written as ``_canonical(entry) + "\\n"`` so the on-disk bytes
    are exactly the bytes that were hashed, then ``flush`` + ``os.fsync`` push
    it to stable storage before the tool call returns.

    A filesystem failure (disk full, permissions, read-only mount) is
    translated into ``ToolError("AUDIT_WRITE_FAILED")`` so it surfaces as a
    structured error rather than an unhandled 500. Callers already catch
    ``ToolError``, so a failed write can never leave a dangling
    ``TOOL_CALL_STARTED`` link.
    """
    path = config.AUDIT_LOG_PATH
    if not path:
        return
    line = _canonical(entry)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _fail("AUDIT_WRITE_FAILED", str(exc))


def _load_audit_from_file() -> List[Dict[str, Any]]:
    """Read the on-disk audit chain. Missing file means empty chain.

    Raises ``ToolError("AUDIT_LOG_CORRUPT")`` if a line is not a JSON object,
    and ``ToolError("AUDIT_LOG_UNREADABLE")`` if the file cannot be read, so
    callers can report the problem instead of silently dropping a link or
    crashing with an unhandled ``OSError``.
    """
    path = config.AUDIT_LOG_PATH
    if not path or not os.path.exists(path):
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    _fail("AUDIT_LOG_CORRUPT", f"line {lineno}: {exc}")
                if not isinstance(entry, dict):
                    _fail("AUDIT_LOG_CORRUPT", f"line {lineno}: not a JSON object")
                entries.append(entry)
    except OSError as exc:
        _fail("AUDIT_LOG_UNREADABLE", str(exc))
    return entries


def _build_audit_entry(
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "tool",
) -> Dict[str, Any]:
    """Build the next audit link. Caller must hold ``_LOCK``."""
    seq = _AUDIT_SEQ + 1
    prev_hash = GENESIS_HASH
    if _AUDIT:
        prev_hash = _AUDIT[-1].get("hash") or GENESIS_HASH

    entry: Dict[str, Any] = {
        "seq": seq,
        "ts": _now(),
        "event_type": event_type,
        "payload": payload,
        "correlation_id": correlation_id or "",
        "prev_hash": prev_hash,
        "source": source,
    }
    entry["hash"] = _compute_hash(entry)
    return entry


def _append_audit(
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> Dict[str, Any]:
    """Append one link to the hash chain and return the stored entry.

    The link is written to ``audit_log.jsonl`` (canonical JSON + newline,
    ``flush`` + ``fsync``) *before* it is added to the in-memory chain, so a
    failed write can never leave memory ahead of disk.

    This is for audit-only events (``log_audit_event``) that do not mutate
    business state. Mutating tools must go through ``_commit`` instead.
    """
    global _AUDIT_SEQ
    with _LOCK:
        if _AUDIT_RECOVERY_ERROR:
            # Fail closed: appending on top of an unreadable log would produce
            # a chain that can never verify.
            _fail("AUDIT_LOG_CORRUPT", _AUDIT_RECOVERY_ERROR)

        entry = _build_audit_entry(event_type, payload, correlation_id, source)
        _persist_audit(entry)
        _AUDIT.append(entry)
        _AUDIT_SEQ = entry["seq"]
        return entry


def _commit(
    mutation: Callable[[], Any],
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "tool",
) -> Any:
    """Persist the audit link, then apply ``mutation``, atomically under ``_LOCK``.

    The audit link is written to disk *before* the in-memory state changes, so
    a failed write (``ToolError("AUDIT_WRITE_FAILED")``) leaves business state
    untouched and the caller can safely retry. If the mutation itself raises
    (it should not, since callers validate first), the in-memory chain and the
    on-disk link are rolled back so memory and disk stay consistent.
    """
    global _AUDIT_SEQ
    with _LOCK:
        if _AUDIT_RECOVERY_ERROR:
            _fail("AUDIT_LOG_CORRUPT", _AUDIT_RECOVERY_ERROR)

        prev_seq = _AUDIT_SEQ
        entry = _build_audit_entry(event_type, payload, correlation_id, source)

        # Snapshot the on-disk length so a failed mutation can be rolled back.
        path = config.AUDIT_LOG_PATH
        pre_size: Optional[int] = None
        if path:
            try:
                pre_size = os.path.getsize(path) if os.path.exists(path) else 0
            except OSError:
                pre_size = None

        # Write the audit link first. If this raises, no state has changed.
        _persist_audit(entry)

        _AUDIT.append(entry)
        _AUDIT_SEQ = entry["seq"]
        try:
            return mutation()
        except Exception:
            # Mutation failed after the audit link was written. Roll back the
            # in-memory chain and truncate the on-disk link so memory and disk
            # stay consistent.
            _AUDIT.pop()
            _AUDIT_SEQ = prev_seq
            if path and pre_size is not None:
                try:
                    with open(path, "r+b") as handle:
                        handle.truncate(pre_size)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError:
                    pass
            raise


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _reset_business_state() -> None:
    """Restore invoices/approvals/POs/receipts to the seed literals.

    Caller holds ``_LOCK``.
    """
    _APPROVALS.clear()
    for invoice_id in list(_INVOICES.keys()):
        if invoice_id not in _SEED_INVOICE_IDS:
            del _INVOICES[invoice_id]
    for invoice in _INVOICES.values():
        invoice["status"] = "RECEIVED"
        invoice["updated_at"] = invoice["created_at"]

    _POS.clear()
    _POS.update({k: dict(v) for k, v in _SEED_POS.items()})
    _RECEIPTS.clear()
    _RECEIPTS.update({k: dict(v) for k, v in _SEED_RECEIPTS.items()})


def _replay_ingest(payload: Dict[str, Any], ts: str) -> None:
    invoice_id = payload.get("invoice_id")
    if not isinstance(invoice_id, str) or not invoice_id:
        _fail("AUDIT_LOG_CORRUPT", "INVOICE_INGESTED missing invoice_id")
    if invoice_id in _INVOICES:
        return
    vendor_id = payload.get("vendor_id")
    po_id = payload.get("po_id")
    amount = payload.get("amount")
    if not isinstance(vendor_id, str) or not vendor_id:
        _fail("AUDIT_LOG_CORRUPT", f"INVOICE_INGESTED {invoice_id} missing vendor_id")
    if not isinstance(po_id, str) or not po_id:
        _fail("AUDIT_LOG_CORRUPT", f"INVOICE_INGESTED {invoice_id} missing po_id")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        _fail("AUDIT_LOG_CORRUPT", f"INVOICE_INGESTED {invoice_id} bad amount")
    _INVOICES[invoice_id] = {
        "invoice_id": invoice_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "amount": float(amount),
        "currency": payload.get("currency") or "USD",
        "status": "RECEIVED",
        "created_at": ts,
        "updated_at": ts,
    }


def _replay_po_created(payload: Dict[str, Any], ts: str) -> None:
    po_id = payload.get("po_id")
    vendor_id = payload.get("vendor_id")
    amount = payload.get("amount")
    expected = payload.get("expected_delivery_date")
    if not isinstance(po_id, str) or not po_id:
        _fail("AUDIT_LOG_CORRUPT", "PO_CREATED missing po_id")
    if po_id in _POS:
        return
    if not isinstance(vendor_id, str) or not vendor_id:
        _fail("AUDIT_LOG_CORRUPT", f"PO_CREATED {po_id} missing vendor_id")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        _fail("AUDIT_LOG_CORRUPT", f"PO_CREATED {po_id} bad amount")
    if not isinstance(expected, str) or not expected:
        _fail(
            "AUDIT_LOG_CORRUPT",
            f"PO_CREATED {po_id} missing expected_delivery_date",
        )
    _POS[po_id] = {
        "po_id": po_id,
        "vendor_id": vendor_id,
        "amount": float(amount),
        "currency": payload.get("currency") or "USD",
        "status": PO_STATUS_OPEN,
        "expected_delivery_date": expected,
    }


def _replay_po_status_updated(payload: Dict[str, Any], ts: str) -> None:
    po_id = payload.get("po_id")
    status = payload.get("status")
    if not isinstance(po_id, str) or not po_id:
        _fail("AUDIT_LOG_CORRUPT", "PO_STATUS_UPDATED missing po_id")
    if status not in _PO_ALLOWED_TARGETS:
        _fail("AUDIT_LOG_CORRUPT", f"PO_STATUS_UPDATED {po_id} bad status")
    po = _POS.get(po_id)
    if po is None:
        _fail("AUDIT_LOG_CORRUPT", f"PO_STATUS_UPDATED for unknown PO {po_id}")
    if po["status"] != PO_STATUS_OPEN:
        _LOG.warning(
            "replay: skipping PO_STATUS_UPDATED for %s; status is %s",
            po_id,
            po["status"],
        )
        return
    po["status"] = status


def _replay_goods_receipt(payload: Dict[str, Any], ts: str) -> None:
    po_id = payload.get("po_id")
    amount = payload.get("amount")
    if not isinstance(po_id, str) or not po_id:
        _fail("AUDIT_LOG_CORRUPT", "GOODS_RECEIPT_RECORDED missing po_id")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        _fail("AUDIT_LOG_CORRUPT", f"GOODS_RECEIPT_RECORDED {po_id} bad amount")
    if po_id in _RECEIPTS:
        return
    po = _POS.get(po_id)
    if po is None:
        _fail("AUDIT_LOG_CORRUPT", f"GOODS_RECEIPT_RECORDED for unknown PO {po_id}")
    if po["status"] != PO_STATUS_OPEN:
        _LOG.warning(
            "replay: skipping GOODS_RECEIPT_RECORDED for %s; status is %s",
            po_id,
            po["status"],
        )
        return
    received_at = payload.get("received_at")
    if not isinstance(received_at, str) or not received_at:
        received_at = ts
    _RECEIPTS[po_id] = {
        "po_id": po_id,
        "amount": float(amount),
        "received_at": received_at,
    }


def _replay_match(payload: Dict[str, Any], ts: str) -> None:
    invoice_id = payload.get("invoice_id")
    status = payload.get("status")
    if not isinstance(invoice_id, str) or not invoice_id:
        _fail("AUDIT_LOG_CORRUPT", "THREE_WAY_MATCH missing invoice_id")
    if status not in ("MATCHED", "EXCEPTION"):
        _fail("AUDIT_LOG_CORRUPT", f"THREE_WAY_MATCH {invoice_id} bad status")
    invoice = _INVOICES.get(invoice_id)
    if invoice is None:
        _fail("AUDIT_LOG_CORRUPT", f"THREE_WAY_MATCH for unknown invoice {invoice_id}")
    if invoice["status"] not in ("RECEIVED", "EXCEPTION"):
        _LOG.warning(
            "replay: skipping THREE_WAY_MATCH for %s; status is %s",
            invoice_id,
            invoice["status"],
        )
        return
    invoice["status"] = status
    invoice["updated_at"] = ts


def _replay_approval_requested(payload: Dict[str, Any], ts: str) -> None:
    approval_id = payload.get("approval_id")
    invoice_id = payload.get("invoice_id")
    vendor_id = payload.get("vendor_id")
    amount = payload.get("amount")
    if not isinstance(approval_id, str) or not approval_id:
        _fail("AUDIT_LOG_CORRUPT", "PAYOUT_APPROVAL_REQUESTED missing approval_id")
    if not isinstance(invoice_id, str) or not invoice_id:
        _fail(
            "AUDIT_LOG_CORRUPT",
            f"PAYOUT_APPROVAL_REQUESTED {approval_id} missing invoice_id",
        )
    if not isinstance(vendor_id, str) or not vendor_id:
        _fail(
            "AUDIT_LOG_CORRUPT",
            f"PAYOUT_APPROVAL_REQUESTED {approval_id} missing vendor_id",
        )
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        _fail("AUDIT_LOG_CORRUPT", f"PAYOUT_APPROVAL_REQUESTED {approval_id} bad amount")
    if approval_id in _APPROVALS:
        return
    invoice = _INVOICES.get(invoice_id)
    if invoice is None:
        _fail(
            "AUDIT_LOG_CORRUPT",
            f"approval {approval_id} for unknown invoice {invoice_id}",
        )
    if invoice["status"] != "MATCHED":
        _LOG.warning(
            "replay: skipping approval %s; invoice %s is %s",
            approval_id,
            invoice_id,
            invoice["status"],
        )
        return
    invoice["status"] = "PENDING_APPROVAL"
    invoice["updated_at"] = ts
    _APPROVALS[approval_id] = {
        "approval_id": approval_id,
        "invoice_id": invoice_id,
        "po_id": payload.get("po_id") or invoice.get("po_id"),
        "vendor_id": vendor_id,
        "amount": float(amount),
        "currency": payload.get("currency") or invoice.get("currency", "USD"),
        "status": "PENDING",
        "requested_by": "agent",
        "requested_at": ts,
        "correlation_id": "",
        "approver": None,
        "decided_at": None,
        "executed_at": None,
    }


def _replay_approved(payload: Dict[str, Any], ts: str) -> None:
    approval_id = payload.get("approval_id")
    approver = payload.get("approver")
    if not isinstance(approval_id, str) or not approval_id:
        _fail("AUDIT_LOG_CORRUPT", "PAYOUT_APPROVED missing approval_id")
    if not isinstance(approver, str) or not approver.strip():
        _fail("AUDIT_LOG_CORRUPT", f"PAYOUT_APPROVED {approval_id} missing approver")
    approval = _APPROVALS.get(approval_id)
    if approval is None:
        _LOG.warning("replay: skipping approval %s; unknown", approval_id)
        return
    if approval["status"] != "PENDING":
        return
    invoice = _INVOICES.get(approval["invoice_id"])
    if invoice is None or invoice["status"] != "PENDING_APPROVAL":
        _LOG.warning("replay: skipping approval %s; invoice not pending", approval_id)
        return
    invoice["status"] = "APPROVED"
    invoice["updated_at"] = ts
    approval["status"] = "APPROVED"
    approval["approver"] = approver.strip()
    approval["decided_at"] = ts


def _replay_executed(payload: Dict[str, Any], ts: str) -> None:
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        _fail("AUDIT_LOG_CORRUPT", "PAYOUT_EXECUTED missing approval_id")
    approval = _APPROVALS.get(approval_id)
    if approval is None:
        _LOG.warning("replay: skipping execution %s; unknown", approval_id)
        return
    if approval["status"] != "APPROVED":
        return
    invoice = _INVOICES.get(approval["invoice_id"])
    if invoice is None or invoice["status"] != "APPROVED":
        _LOG.warning("replay: skipping execution %s; invoice not approved", approval_id)
        return
    invoice["status"] = "PAID"
    invoice["updated_at"] = ts
    approval["status"] = "EXECUTED"
    approval["executed_at"] = ts
    executor = payload.get("executor")
    if isinstance(executor, str) and executor.strip():
        approval["executor"] = executor.strip()


def _replay_audit_entries(entries: List[Dict[str, Any]]) -> None:
    """Rebuild business state by replaying the durable chain in seq order.

    The chain is the source of truth: ``_commit`` writes the audit link before
    the mutation, so a crash can leave a link whose mutation never landed.
    Replay applies it, which is at-least-once and safe because every transition
    is status-guarded. A link that would move an invoice backwards (for example
    a ``THREE_WAY_MATCH`` after a ``PAYOUT_EXECUTED``) is skipped with a warning
    rather than applied, so a restart cannot resurrect a paid invoice.

    Unknown event types (agent notes, tool brackets) are ignored.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        event_type = entry.get("event_type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = entry.get("ts") or _now()
        if event_type == "INVOICE_INGESTED":
            _replay_ingest(payload, ts)
        elif event_type == "PO_CREATED":
            _replay_po_created(payload, ts)
        elif event_type == "PO_STATUS_UPDATED":
            _replay_po_status_updated(payload, ts)
        elif event_type == "GOODS_RECEIPT_RECORDED":
            _replay_goods_receipt(payload, ts)
        elif event_type == "THREE_WAY_MATCH":
            _replay_match(payload, ts)
        elif event_type == "PAYOUT_APPROVAL_REQUESTED":
            _replay_approval_requested(payload, ts)
        elif event_type == "PAYOUT_APPROVED":
            _replay_approved(payload, ts)
        elif event_type == "PAYOUT_EXECUTED":
            _replay_executed(payload, ts)


def _recover_audit_state() -> None:
    """Load ``audit_log.jsonl`` and rebuild business state from it.

    Called once at import time. The chain is durable; the dicts it describes
    are not, so without replay a restart resets every invoice to ``RECEIVED``
    and a paid invoice can be matched and paid again. If the file cannot be
    parsed, read, or replayed, the in-memory chain is left empty, business
    state is reset to the seed literals, and ``_AUDIT_RECOVERY_ERROR`` is set;
    ``_append_audit`` then refuses to write until an operator repairs the log.
    """
    global _AUDIT_SEQ, _AUDIT_RECOVERY_ERROR
    with _LOCK:
        try:
            entries = _load_audit_from_file()
        except ToolError as exc:
            _AUDIT.clear()
            _AUDIT_SEQ = 0
            _AUDIT_RECOVERY_ERROR = f"{exc.code}: {exc.detail}"
            _reset_business_state()
            return
        except OSError as exc:
            # Defensive: ``_load_audit_from_file`` already converts OSError to
            # ToolError, but a future refactor must not turn a bad log into an
            # import-time traceback.
            _AUDIT.clear()
            _AUDIT_SEQ = 0
            _AUDIT_RECOVERY_ERROR = f"AUDIT_LOG_UNREADABLE: {exc}"
            _reset_business_state()
            return

        _AUDIT.clear()
        _AUDIT.extend(entries)

        try:
            _replay_audit_entries(entries)
        except ToolError as exc:
            _AUDIT.clear()
            _AUDIT_SEQ = 0
            _AUDIT_RECOVERY_ERROR = f"{exc.code}: {exc.detail}"
            _reset_business_state()
            return

        last_seq = entries[-1].get("seq") if entries else 0
        if isinstance(last_seq, int) and not isinstance(last_seq, bool):
            _AUDIT_SEQ = last_seq
        else:
            _AUDIT_SEQ = len(entries)
        _AUDIT_RECOVERY_ERROR = ""


def _transition(
    invoice_id: str,
    allowed_from: Tuple[str, ...],
    to_status: str,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Move an invoice between states, refusing illegal transitions."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            _fail("INVOICE_NOT_FOUND", invoice_id)
        if invoice["status"] not in allowed_from:
            _fail("INVALID_TRANSITION", f"{invoice['status']} -> {to_status}")
        invoice["status"] = to_status
        invoice["updated_at"] = _now()
        return dict(invoice)


def _reset_state() -> None:
    """Test hook: clear approvals and the audit chain, keep seed records.

    Invoices added by ``ingest_invoice`` are removed so a test that ingests
    does not leak into the next test. POs and receipts are restored to their
    seed literals for the same reason.
    """
    global _AUDIT_SEQ, _AUDIT_RECOVERY_ERROR
    with _LOCK:
        _reset_business_state()
        _AUDIT.clear()
        _AUDIT_SEQ = 0
        _AUDIT_RECOVERY_ERROR = ""
        path = config.AUDIT_LOG_PATH
        if path:
            with open(path, "w", encoding="utf-8"):
                pass

# ---------------------------------------------------------------------------
# Allowlisted tools
# ---------------------------------------------------------------------------


def lookup_vendor(vendor_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Check vendor status and risk tier. Read-only."""
    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        return {"ok": True, "vendor": dict(vendor)}


def run_three_way_match(
    invoice_id: str, po_id: str, correlation_id: str = ""
) -> Dict[str, Any]:
    """Compare PO, goods receipt and invoice; set MATCHED or EXCEPTION."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }

        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}

        if invoice["po_id"] != po_id:
            return {
                "ok": False,
                "error": "PO_INVOICE_MISMATCH",
                "invoice_po_id": invoice["po_id"],
                "po_id": po_id,
            }

        if invoice["vendor_id"] != po["vendor_id"]:
            return {
                "ok": False,
                "error": "VENDOR_MISMATCH",
                "invoice_vendor_id": invoice["vendor_id"],
                "po_vendor_id": po["vendor_id"],
            }

        if invoice["status"] not in ("RECEIVED", "EXCEPTION"):
            return {
                "ok": False,
                "error": "INVALID_STATE",
                "status": invoice["status"],
                "detail": "invoice is already matched or past the match stage",
            }

        mismatch_codes: List[str] = []
        receipt = _RECEIPTS.get(po_id)

        if receipt is None:
            mismatch_codes.append("RECEIPT_MISSING")
        elif receipt["amount"] + AMOUNT_TOLERANCE < po["amount"]:
            mismatch_codes.append("RECEIPT_SHORT")

        if abs(invoice["amount"] - po["amount"]) > AMOUNT_TOLERANCE:
            mismatch_codes.append("AMOUNT_MISMATCH")

        status = "MATCHED" if not mismatch_codes else "EXCEPTION"

        def mutation() -> None:
            _transition(invoice_id, ("RECEIVED", "EXCEPTION"), status, correlation_id)

        _commit(
            mutation,
            "THREE_WAY_MATCH",
            {
                "invoice_id": invoice_id,
                "po_id": po_id,
                "status": status,
                "mismatch_codes": mismatch_codes,
            },
            correlation_id,
        )

        return {
            "ok": True,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "status": status,
            "mismatch_codes": mismatch_codes,
            "invoice_amount": invoice["amount"],
            "po_amount": po["amount"],
            "receipt_amount": receipt["amount"] if receipt else None,
        }


def request_payout_approval(
    vendor_id: str,
    amount: float,
    po_id: str,
    invoice_id: str,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Open a human approval gate. Does NOT move money."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}

    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        if vendor["status"] != "ACTIVE":
            return {
                "ok": False,
                "error": "VENDOR_INACTIVE",
                "vendor_id": vendor_id,
                "status": vendor["status"],
            }
        if vendor["risk_tier"] == "HIGH":
            return {
                "ok": False,
                "error": "VENDOR_HIGH_RISK",
                "vendor_id": vendor_id,
                "risk_tier": vendor["risk_tier"],
            }

        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }
        if invoice["vendor_id"] != vendor_id:
            return {
                "ok": False,
                "error": "VENDOR_MISMATCH",
                "invoice_vendor_id": invoice["vendor_id"],
                "vendor_id": vendor_id,
            }
        if invoice["po_id"] != po_id:
            return {
                "ok": False,
                "error": "PO_INVOICE_MISMATCH",
                "invoice_po_id": invoice["po_id"],
                "po_id": po_id,
            }
        if invoice["status"] != "MATCHED":
            return {
                "ok": False,
                "error": "INVOICE_NOT_MATCHED",
                "status": invoice["status"],
            }
        if abs(float(amount) - invoice["amount"]) > AMOUNT_TOLERANCE:
            return {
                "ok": False,
                "error": "AMOUNT_MISMATCH",
                "requested": float(amount),
                "invoice_amount": invoice["amount"],
            }

        for existing in _APPROVALS.values():
            if (
                existing["invoice_id"] == invoice_id
                and existing["status"] in ("PENDING", "APPROVED")
            ):
                return {
                    "ok": False,
                    "error": "APPROVAL_ALREADY_PENDING",
                    "approval_id": existing["approval_id"],
                    "status": existing["status"],
                }

        approval_id = "APR-" + uuid.uuid4().hex[:8].upper()
        approval: Dict[str, Any] = {
            "approval_id": approval_id,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "vendor_id": vendor_id,
            "amount": float(amount),
            "currency": invoice.get("currency", "USD"),
            "status": "PENDING",
            "requested_by": "agent",
            "requested_at": _now(),
            "correlation_id": correlation_id,
            "approver": None,
            "decided_at": None,
            "executed_at": None,
        }

        def mutation() -> None:
            # Transition first: it is the only fallible step, so a failure
            # leaves no partial state behind.
            _transition(invoice_id, ("MATCHED",), "PENDING_APPROVAL", correlation_id)
            _APPROVALS[approval_id] = approval

        _commit(
            mutation,
            "PAYOUT_APPROVAL_REQUESTED",
            {
                "approval_id": approval_id,
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "po_id": po_id,
                "amount": approval["amount"],
                "currency": approval["currency"],
            },
            correlation_id,
        )

        return {
            "ok": True,
            "approval_id": approval_id,
            "status": "PENDING",
            "requires_human_approval": True,
            "invoice_id": invoice_id,
            "vendor_id": vendor_id,
            "amount": approval["amount"],
            "currency": approval["currency"],
        }


def log_audit_event(
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> Dict[str, Any]:
    """Record an agent reasoning note in the audit trail.

    ``source="agent"`` (the default, and the only value the model can reach)
    refuses the tool-authored event types, so a model note cannot masquerade as
    a real approval gate or settlement. The control plane passes
    ``source="tool"`` for its own brackets.
    """
    if not isinstance(event_type, str) or not event_type.strip():
        return {"ok": False, "error": "INVALID_EVENT_TYPE"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "INVALID_PAYLOAD"}

    clean = event_type.strip()
    if source == "agent" and clean in _RESERVED_EVENT_TYPES:
        return {
            "ok": False,
            "error": "RESERVED_EVENT_TYPE",
            "event_type": clean,
        }

    entry = _append_audit(clean, payload, correlation_id, source=source)
    return {"ok": True, "seq": entry["seq"], "hash": entry["hash"]}


def get_invoice(invoice_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Read an invoice record by ID."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }
        return {"ok": True, "invoice": dict(invoice)}


def get_approval(approval_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Read an approval record by ID."""
    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        return {"ok": True, "approval": dict(approval)}


def verify_audit_chain(
    correlation_id: str = "", from_file: bool = True
) -> Dict[str, Any]:
    """Recompute the audit hash chain and report the first divergence.

    ``from_file=True`` (default) re-reads ``config.AUDIT_LOG_PATH`` and verifies
    that, so the result reflects what is actually durable on disk.
    ``from_file=False`` verifies the in-memory chain instead.
    """
    with _LOCK:
        if from_file:
            try:
                entries = _load_audit_from_file()
            except ToolError as exc:
                return {
                    "ok": True,
                    "valid": False,
                    "checked": 0,
                    "diverged_at": None,
                    "reason": exc.code,
                    "detail": exc.detail,
                    "head_hash": GENESIS_HASH,
                }
        else:
            entries = list(_AUDIT)

        prev_hash = GENESIS_HASH
        for index, entry in enumerate(entries):
            expected_seq = index + 1
            if entry.get("seq") != expected_seq:
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "SEQ_GAP",
                    "head_hash": _head_hash(entries),
                }
            if entry.get("prev_hash") != prev_hash:
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "PREV_HASH_MISMATCH",
                    "head_hash": _head_hash(entries),
                }
            try:
                recomputed: Optional[str] = _compute_hash(entry)
            except (KeyError, TypeError):
                recomputed = None
            if recomputed != entry.get("hash"):
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "HASH_MISMATCH",
                    "head_hash": _head_hash(entries),
                }
            prev_hash = entry["hash"]

        return {
            "ok": True,
            "valid": True,
            "checked": len(entries),
            "head_hash": prev_hash,
        }

# ---------------------------------------------------------------------------
# App-layer only - never reachable from the agent
# ---------------------------------------------------------------------------


def ingest_invoice(
    invoice_id: str,
    vendor_id: str,
    po_id: str,
    amount: float,
    currency: str = "USD",
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Create an invoice record from the ERP feed."""
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        return {"ok": False, "error": "INVALID_INVOICE_ID"}
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}

    with _LOCK:
        if invoice_id in _INVOICES:
            return {"ok": False, "error": "INVOICE_EXISTS", "invoice_id": invoice_id}
        if vendor_id not in _VENDORS:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        if po_id not in _POS:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}

        # Reject an invoice whose PO belongs to a different vendor at ingest,
        # rather than accepting it and failing later in ``run_three_way_match``.
        po = _POS[po_id]
        if po["vendor_id"] != vendor_id:
            return {
                "ok": False,
                "error": "VENDOR_MISMATCH",
                "vendor_id": vendor_id,
                "po_vendor_id": po["vendor_id"],
            }

        now = _now()
        invoice: Dict[str, Any] = {
            "invoice_id": invoice_id,
            "vendor_id": vendor_id,
            "po_id": po_id,
            "amount": float(amount),
            "currency": currency or "USD",
            "status": "RECEIVED",
            "created_at": now,
            "updated_at": now,
        }

        def mutation() -> None:
            _INVOICES[invoice_id] = invoice

        _commit(
            mutation,
            "INVOICE_INGESTED",
            {
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "po_id": po_id,
                "amount": invoice["amount"],
                "currency": invoice["currency"],
            },
            correlation_id,
        )
        return {"ok": True, "invoice": dict(invoice)}


def create_purchase_order(
    po_id: str,
    vendor_id: str,
    amount: float,
    expected_delivery_date: str,
    currency: str = "USD",
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Create a purchase order from the ERP feed.

    The PO starts ``OPEN``. ``expected_delivery_date`` must be an ISO
    ``YYYY-MM-DD`` date string. App-layer only: never exposed to the model.
    """
    if not isinstance(po_id, str) or not po_id.strip():
        return {"ok": False, "error": "INVALID_PO_ID"}
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if not _valid_date(expected_delivery_date):
        return {
            "ok": False,
            "error": "INVALID_EXPECTED_DELIVERY_DATE",
            "expected_delivery_date": expected_delivery_date,
        }

    clean_po_id = po_id.strip()
    clean_date = expected_delivery_date.strip()

    with _LOCK:
        if clean_po_id in _POS:
            return {"ok": False, "error": "PO_EXISTS", "po_id": clean_po_id}
        if vendor_id not in _VENDORS:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}

        po: Dict[str, Any] = {
            "po_id": clean_po_id,
            "vendor_id": vendor_id,
            "amount": float(amount),
            "currency": currency or "USD",
            "status": PO_STATUS_OPEN,
            "expected_delivery_date": clean_date,
        }

        def mutation() -> None:
            _POS[clean_po_id] = po

        _commit(
            mutation,
            "PO_CREATED",
            {
                "po_id": clean_po_id,
                "vendor_id": vendor_id,
                "amount": po["amount"],
                "currency": po["currency"],
                "expected_delivery_date": clean_date,
            },
            correlation_id,
        )
        return {"ok": True, "po": dict(po)}


def update_po_status(
    po_id: str,
    new_status: str,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Move a PO out of ``OPEN`` (to ``CLOSED`` or ``CANCELLED``).

    Only a PO whose current status is ``OPEN`` can be updated; this is the
    "update PO status from OPEN" gate. App-layer only: never exposed to the
    model.
    """
    if not isinstance(po_id, str) or not po_id.strip():
        return {"ok": False, "error": "INVALID_PO_ID"}
    if not isinstance(new_status, str) or not new_status.strip():
        return {"ok": False, "error": "INVALID_STATUS"}
    clean_status = new_status.strip().upper()
    if clean_status not in _PO_ALLOWED_TARGETS:
        return {
            "ok": False,
            "error": "INVALID_STATUS",
            "status": clean_status,
            "allowed": sorted(_PO_ALLOWED_TARGETS),
        }

    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}
        if po["status"] != PO_STATUS_OPEN:
            return {
                "ok": False,
                "error": "PO_NOT_OPEN",
                "po_id": po_id,
                "status": po["status"],
            }

        def mutation() -> None:
            po["status"] = clean_status

        _commit(
            mutation,
            "PO_STATUS_UPDATED",
            {"po_id": po_id, "status": clean_status},
            correlation_id,
        )
        return {"ok": True, "po": dict(po)}


def record_goods_receipt(
    po_id: str,
    amount: float,
    received_at: str = "",
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Record a goods receipt against an ``OPEN`` PO.

    One receipt per PO in this model (matching ``run_three_way_match``), so a
    second receipt for the same PO is rejected with ``RECEIPT_EXISTS``.
    ``received_at`` defaults to the current UTC timestamp. App-layer only:
    never exposed to the model.
    """
    if not isinstance(po_id, str) or not po_id.strip():
        return {"ok": False, "error": "INVALID_PO_ID"}
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}

    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}
        if po["status"] != PO_STATUS_OPEN:
            return {
                "ok": False,
                "error": "PO_NOT_OPEN",
                "po_id": po_id,
                "status": po["status"],
            }
        if po_id in _RECEIPTS:
            return {"ok": False, "error": "RECEIPT_EXISTS", "po_id": po_id}

        received = (
            received_at.strip()
            if isinstance(received_at, str) and received_at.strip()
            else _now()
        )
        receipt: Dict[str, Any] = {
            "po_id": po_id,
            "amount": float(amount),
            "received_at": received,
        }

        def mutation() -> None:
            _RECEIPTS[po_id] = receipt

        _commit(
            mutation,
            "GOODS_RECEIPT_RECORDED",
            {
                "po_id": po_id,
                "amount": receipt["amount"],
                "received_at": received,
            },
            correlation_id,
        )
        return {"ok": True, "receipt": dict(receipt)}


def approve_payout(
    approval_id: str, approver: str, correlation_id: str = ""
) -> Dict[str, Any]:
    """Record a human approval decision. Does NOT move money."""
    if not isinstance(approver, str) or not approver.strip():
        return {"ok": False, "error": "INVALID_APPROVER"}

    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        if approval["status"] != "PENDING":
            return {
                "ok": False,
                "error": "APPROVAL_NOT_PENDING",
                "status": approval["status"],
            }

        invoice = _INVOICES.get(approval["invoice_id"])
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": approval["invoice_id"],
            }
        if invoice["status"] != "PENDING_APPROVAL":
            return {
                "ok": False,
                "error": "INVOICE_NOT_PENDING_APPROVAL",
                "status": invoice["status"],
            }

        clean_approver = approver.strip()

        def mutation() -> None:
            # Transition first: it is the only fallible step, so a failure
            # leaves no partial state behind.
            _transition(
                approval["invoice_id"],
                ("PENDING_APPROVAL",),
                "APPROVED",
                correlation_id,
            )
            approval["status"] = "APPROVED"
            approval["approver"] = clean_approver
            approval["decided_at"] = _now()

        _commit(
            mutation,
            "PAYOUT_APPROVED",
            {
                "approval_id": approval_id,
                "approver": clean_approver,
                "amount": approval["amount"],
            },
            correlation_id,
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": "APPROVED",
            "approver": approval["approver"],
        }


def _execute_payout(
    approval_id: str, executor: str = "", correlation_id: str = ""
) -> Dict[str, Any]:
    """Settle an approved payout. Private: never exposed to the model.

    ``executor`` is the authenticated principal's ``sub``. Separation of duties
    is enforced here, under ``_LOCK``, so a direct caller cannot bypass it: the
    person who approved a payout cannot be the person who settles it.
    """
    if not isinstance(executor, str) or not executor.strip():
        return {"ok": False, "error": "INVALID_EXECUTOR"}
    clean_executor = executor.strip()

    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        if approval["status"] != "APPROVED":
            return {
                "ok": False,
                "error": "APPROVAL_NOT_APPROVED",
                "status": approval["status"],
            }

        approver = approval.get("approver")
        if approver and approver == clean_executor:
            return {
                "ok": False,
                "error": "SEPARATION_OF_DUTIES",
                "approver": approver,
                "executor": clean_executor,
            }

        invoice = _INVOICES.get(approval["invoice_id"])
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": approval["invoice_id"],
            }
        if invoice["status"] != "APPROVED":
            return {
                "ok": False,
                "error": "INVOICE_NOT_APPROVED",
                "status": invoice["status"],
            }

        def mutation() -> None:
            # Transition first: it is the only fallible step, so a failure
            # leaves no partial state behind.
            _transition(invoice["invoice_id"], ("APPROVED",), "PAID", correlation_id)
            approval["status"] = "EXECUTED"
            approval["executed_at"] = _now()
            approval["executor"] = clean_executor

        _commit(
            mutation,
            "PAYOUT_EXECUTED",
            {
                "approval_id": approval_id,
                "invoice_id": invoice["invoice_id"],
                "vendor_id": approval["vendor_id"],
                "amount": approval["amount"],
                "executor": clean_executor,
            },
            correlation_id,
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "invoice_id": invoice["invoice_id"],
            "amount": approval["amount"],
            "status": "PAID",
            "settled_at": approval["executed_at"],
            "executor": clean_executor,
        }

# ---------------------------------------------------------------------------
# Sandbox support (control plane)
# ---------------------------------------------------------------------------
#
# ``sandbox.dispatch`` runs ``decisions.decide`` twice: once inside the sandbox
# against a read-only snapshot, and once here against live state. The proposal
# that survives the comparison is applied through the helpers below. They
# delegate to the public tool functions so validation and audit logic lives in
# exactly one place.
#
# Proposal contract (produced by ``decisions.decide``):
#
#   read            {"kind": "read", "data": {...}}
#   transition      {"kind": "transition", "invoice_id": str, "po_id": str}
#   create_approval {"kind": "create_approval", "vendor_id": str,
#                    "amount": float, "po_id": str, "invoice_id": str}
#   audit_note      {"kind": "audit_note", "event_type": str,
#                    "payload": dict}
#
# Proposals carry only inputs, never generated IDs or timestamps, so the
# sandbox's proposal and the control plane's re-decision compare equal.


def _snapshot_for(tool: str) -> Dict[str, Any]:
    """Return a read-only copy of the state ``tool`` needs to decide.

    The snapshot is serialised into the sandbox request envelope, so it must
    contain business data only - no secrets, no live references.
    """
    with _LOCK:
        if tool == "lookup_vendor":
            return {"vendors": {k: dict(v) for k, v in _VENDORS.items()}}
        if tool == "get_invoice":
            return {"invoices": {k: dict(v) for k, v in _INVOICES.items()}}
        if tool == "run_three_way_match":
            return {
                "invoices": {k: dict(v) for k, v in _INVOICES.items()},
                "pos": {k: dict(v) for k, v in _POS.items()},
                "receipts": {k: dict(v) for k, v in _RECEIPTS.items()},
            }
        if tool == "request_payout_approval":
            return {
                "vendors": {k: dict(v) for k, v in _VENDORS.items()},
                "invoices": {k: dict(v) for k, v in _INVOICES.items()},
                "approvals": {k: dict(v) for k, v in _APPROVALS.items()},
            }
        if tool == "log_audit_event":
            return {}
        return {}


def _apply_transition(
    proposal: Dict[str, Any], correlation_id: str = ""
) -> Dict[str, Any]:
    """Apply a ``transition`` proposal by re-running the three-way match."""
    invoice_id = proposal.get("invoice_id")
    po_id = proposal.get("po_id")
    if not isinstance(invoice_id, str) or not isinstance(po_id, str):
        return {"ok": False, "error": "INVALID_PROPOSAL", "kind": "transition"}
    try:
        return run_three_way_match(invoice_id, po_id, correlation_id)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}


def _apply_create_approval(
    proposal: Dict[str, Any], correlation_id: str = ""
) -> Dict[str, Any]:
    """Apply a ``create_approval`` proposal by opening the approval gate."""
    vendor_id = proposal.get("vendor_id")
    po_id = proposal.get("po_id")
    invoice_id = proposal.get("invoice_id")
    amount = proposal.get("amount")
    if not all(isinstance(v, str) for v in (vendor_id, po_id, invoice_id)):
        return {"ok": False, "error": "INVALID_PROPOSAL", "kind": "create_approval"}
    try:
        return request_payout_approval(
            vendor_id, amount, po_id, invoice_id, correlation_id
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}


def _apply_audit_note(
    proposal: Dict[str, Any], correlation_id: str = ""
) -> Dict[str, Any]:
    """Apply an ``audit_note`` proposal by appending the reasoning note."""
    event_type = proposal.get("event_type")
    payload = proposal.get("payload")
    if not isinstance(event_type, str) or not isinstance(payload, dict):
        return {"ok": False, "error": "INVALID_PROPOSAL", "kind": "audit_note"}
    try:
        return log_audit_event(event_type, payload, correlation_id)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

# Load the durable chain at import and replay it so business state matches the
# chain instead of resetting to the seed literals on every restart.
_recover_audit_state()
