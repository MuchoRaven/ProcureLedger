"""
tools.py
========
ProcureLedger - tool facade.

This module is the only place that touches invoice, approval and audit state.
``agent.py`` reaches it exclusively through the default-deny ``_DISPATCH``
allowlist, so any public function here that is not in ``ALLOWED_TOOLS`` stays
app-layer only.

The domain logic now lives in the ``tool_modules`` package:

* ``tool_modules._state``        - shared state, audit chain, replay, commit.
* ``tool_modules.vendors``       - ``lookup_vendor``.
* ``tool_modules.orders``        - POs, goods receipts, three-way match.
* ``tool_modules.requisitions``  - requisitions and the open-needs monitor.
* ``tool_modules.aging``         - ``get_invoice_aging``.

This module re-exports those functions so external imports (``from tools
import lookup_vendor``) keep working, and keeps the invoice/approval/payout,
audit-read and sandbox-support functions that were not part of the split.

App-layer only (never exposed to the model)
-------------------------------------------
* ``ingest_invoice``         - creates invoice records from the ERP feed.
* ``create_purchase_order``  - creates PO records from the ERP feed.
* ``update_po_status``       - moves a PO out of ``OPEN`` (close/cancel).
* ``record_goods_receipt``   - records a goods receipt against an open PO.
* ``approve_payout``         - records a human approval decision.
* ``_execute_payout``        - settles an approved payout. Private on purpose.
* ``create_requisition``     - registers an internal purchasing demand.
* ``update_requisition_status`` - converts/cancels an open requisition.

Sandbox support
---------------
``sandbox.dispatch`` re-decides every tool call on live state and applies the
verified proposal through ``_snapshot_for`` and the ``_apply_*`` helpers below.
Those helpers delegate to the public tool functions, so validation and audit
logic is not duplicated.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from tool_modules._state import (
    AMOUNT_TOLERANCE,
    GENESIS_HASH,
    PO_STATUS_CANCELLED,
    PO_STATUS_CLOSED,
    PO_STATUS_OPEN,
    REQ_STATUS_CANCELLED,
    REQ_STATUS_CONVERTED,
    REQ_STATUS_OPEN,
    Principal,
    ToolError,
    _APPROVALS,
    _AUDIT,
    _INVOICES,
    _LOCK,
    _POS,
    _RECEIPTS,
    _REQUISITIONS,
    _RESERVED_EVENT_TYPES,
    _VENDORS,
    _append_audit,
    _commit,
    _compute_hash,
    _head_hash,
    _load_audit_from_file,
    _now,
    _recover_audit_state,  # re-exported for backward compatibility
    _reset_state,  # re-exported for backward compatibility (test hook)
    _transition,
)
from tool_modules.aging import get_invoice_aging
from tool_modules.orders import (
    create_purchase_order,
    record_goods_receipt,
    run_three_way_match,
    update_po_status,
)
from tool_modules.requisitions import (
    create_requisition,
    monitor_purchasing_needs,
    update_requisition_status,
)
from tool_modules.vendors import lookup_vendor

__all__ = [
    # Re-exported domain tools
    "lookup_vendor",
    "monitor_purchasing_needs",
    "run_three_way_match",
    "create_purchase_order",
    "update_po_status",
    "record_goods_receipt",
    "create_requisition",
    "update_requisition_status",
    "get_invoice_aging",
    # Invoice / approval / payout
    "ingest_invoice",
    "request_payout_approval",
    "approve_payout",
    "get_invoice",
    "get_approval",
    # Audit
    "log_audit_event",
    "verify_audit_chain",
    # Shared types / constants
    "ToolError",
    "Principal",
    "GENESIS_HASH",
    "AMOUNT_TOLERANCE",
    "PO_STATUS_OPEN",
    "PO_STATUS_CLOSED",
    "PO_STATUS_CANCELLED",
    "REQ_STATUS_OPEN",
    "REQ_STATUS_CONVERTED",
    "REQ_STATUS_CANCELLED",
]

# ---------------------------------------------------------------------------
# Invoice / approval / payout
# ---------------------------------------------------------------------------


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
# Audit
# ---------------------------------------------------------------------------


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
        if tool == "monitor_purchasing_needs":
            return {"requisitions": {k: dict(v) for k, v in _REQUISITIONS.items()}}
        if tool == "get_invoice_aging":
            return {
                "invoices": {k: dict(v) for k, v in _INVOICES.items()},
                "vendors": {k: dict(v) for k, v in _VENDORS.items()},
            }
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
