"""
decisions.py
============
Deterministic evaluation logic for ProcureLedger tool calls.

``decide`` is a pure function of ``(tool, arguments, snapshot)``. It runs twice
per call: once inside the sandbox against the read-only snapshot in the request
envelope, and once in the control plane against live state
(``sandbox._redeci``). The two proposals are compared for equality, so this
module must not read the clock, generate IDs, touch the filesystem, or import
``tools``/``config``. Anything non-deterministic belongs in ``tools._apply_*``.

Proposal contract (consumed by ``sandbox._apply`` and ``tools._apply_*``):

    read            {"kind": "read", "data": {...}}
    transition      {"kind": "transition", "invoice_id": str, "po_id": str}
    create_approval {"kind": "create_approval", "vendor_id": str,
                     "amount": float, "po_id": str, "invoice_id": str}
    audit_note      {"kind": "audit_note", "event_type": str, "payload": dict}

Proposals carry inputs only - never generated IDs, timestamps or derived
status - so the sandbox's proposal and the control plane's re-decision compare
equal.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Tuple

CONTRACT_VERSION = "1.0.0"

# Match tolerance. Must equal ``tools.AMOUNT_TOLERANCE``; the two are asserted
# equal in ``test_sandbox_wiring.py`` so they cannot drift. It lives here as
# well because ``decisions`` must stay importable inside the sandbox, where
# importing ``tools`` would pull in ``config`` and the audit-log recovery.
AMOUNT_TOLERANCE = 0.01

# Tool -> proposal kinds that tool may emit. ``sandbox._validate`` rejects any
# envelope whose proposal kind is not listed here.
PROPOSAL_KINDS: Dict[str, Tuple[str, ...]] = {
    "lookup_vendor": ("read",),
    "get_invoice": ("read",),
    "run_three_way_match": ("transition",),
    "request_payout_approval": ("create_approval",),
    "log_audit_event": ("audit_note",),
}

# Tools the sandbox accepts at all. Derived so the two tables cannot drift.
SNAPSHOT_SCOPES: Tuple[str, ...] = tuple(PROPOSAL_KINDS)


def _error(code: str, detail: str = "") -> Dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


def _proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "proposal": proposal}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


# ---------------------------------------------------------------------------
# Per-tool deciders
# ---------------------------------------------------------------------------


def _decide_lookup_vendor(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    vendor_id = arguments.get("vendor_id")
    if not _nonempty_str(vendor_id):
        return _error("INVALID_ARGUMENTS", "vendor_id must be a non-empty string")
    vendor = snapshot.get("vendors", {}).get(vendor_id)
    if vendor is None:
        return _error("VENDOR_NOT_FOUND", vendor_id)
    return _proposal({"kind": "read", "data": {"vendor": dict(vendor)}})


def _decide_get_invoice(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    invoice_id = arguments.get("invoice_id")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    return _proposal({"kind": "read", "data": {"invoice": dict(invoice)}})


def _decide_run_three_way_match(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    invoice_id = arguments.get("invoice_id")
    po_id = arguments.get("po_id")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    if not _nonempty_str(po_id):
        return _error("INVALID_ARGUMENTS", "po_id must be a non-empty string")

    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    po = snapshot.get("pos", {}).get(po_id)
    if po is None:
        return _error("PO_NOT_FOUND", po_id)
    if invoice.get("po_id") != po_id:
        return _error(
            "PO_INVOICE_MISMATCH",
            f"invoice {invoice_id} is on {invoice.get('po_id')}",
        )
    if invoice.get("vendor_id") != po.get("vendor_id"):
        return _error(
            "VENDOR_MISMATCH",
            f"invoice {invoice.get('vendor_id')} vs PO {po.get('vendor_id')}",
        )
    if invoice.get("status") not in ("RECEIVED", "EXCEPTION"):
        return _error("INVALID_STATE", f"invoice status is {invoice.get('status')}")

    # Receipts are deliberately not checked here: a short receipt is a legal
    # transition that ``tools.run_three_way_match`` records as EXCEPTION. The
    # proposal carries inputs only, so the outcome is recomputed on apply.
    return _proposal({"kind": "transition", "invoice_id": invoice_id, "po_id": po_id})


def _decide_request_payout_approval(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    vendor_id = arguments.get("vendor_id")
    po_id = arguments.get("po_id")
    invoice_id = arguments.get("invoice_id")
    amount = arguments.get("amount")

    if not _nonempty_str(vendor_id):
        return _error("INVALID_ARGUMENTS", "vendor_id must be a non-empty string")
    if not _nonempty_str(po_id):
        return _error("INVALID_ARGUMENTS", "po_id must be a non-empty string")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    # ``json.loads`` accepts NaN/Infinity by default; reject them here so the
    # proposal stays comparable (NaN != NaN would fail the equality check).
    if not _is_number(amount) or not math.isfinite(float(amount)):
        return _error("INVALID_AMOUNT", "amount must be a finite number")
    if amount <= 0:
        return _error("INVALID_AMOUNT", "amount must be greater than zero")

    vendor = snapshot.get("vendors", {}).get(vendor_id)
    if vendor is None:
        return _error("VENDOR_NOT_FOUND", vendor_id)
    if vendor.get("status") != "ACTIVE":
        return _error("VENDOR_INACTIVE", f"{vendor_id} is {vendor.get('status')}")
    if vendor.get("risk_tier") == "HIGH":
        return _error("VENDOR_HIGH_RISK", f"{vendor_id} is HIGH risk")

    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    if invoice.get("vendor_id") != vendor_id:
        return _error(
            "VENDOR_MISMATCH",
            f"invoice {invoice.get('vendor_id')} vs {vendor_id}",
        )
    if invoice.get("po_id") != po_id:
        return _error(
            "PO_INVOICE_MISMATCH",
            f"invoice {invoice.get('po_id')} vs {po_id}",
        )
    if invoice.get("status") != "MATCHED":
        return _error(
            "INVOICE_NOT_MATCHED", f"invoice status is {invoice.get('status')}"
        )
    if abs(float(amount) - float(invoice.get("amount", 0.0))) > AMOUNT_TOLERANCE:
        return _error(
            "AMOUNT_MISMATCH",
            f"requested {amount} vs invoice {invoice.get('amount')}",
        )

    for existing in snapshot.get("approvals", {}).values():
        if (
            existing.get("invoice_id") == invoice_id
            and existing.get("status") in ("PENDING", "APPROVED")
        ):
            return _error(
                "APPROVAL_ALREADY_PENDING",
                f"{existing.get('approval_id')} is {existing.get('status')}",
            )

    return _proposal(
        {
            "kind": "create_approval",
            "vendor_id": vendor_id,
            "amount": float(amount),
            "po_id": po_id,
            "invoice_id": invoice_id,
        }
    )


def _decide_log_audit_event(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    event_type = arguments.get("event_type")
    payload = arguments.get("payload")
    if not _nonempty_str(event_type) or not event_type.strip():
        return _error("INVALID_EVENT_TYPE", "event_type must be a non-empty string")
    if not isinstance(payload, dict):
        return _error("INVALID_PAYLOAD", "payload must be an object")
    return _proposal(
        {
            "kind": "audit_note",
            "event_type": event_type.strip(),
            "payload": payload,
        }
    )


_DECIDERS = {
    "lookup_vendor": _decide_lookup_vendor,
    "get_invoice": _decide_get_invoice,
    "run_three_way_match": _decide_run_three_way_match,
    "request_payout_approval": _decide_request_payout_approval,
    "log_audit_event": _decide_log_audit_event,
}


def decide(tool: str, arguments: Any, snapshot: Any) -> Dict[str, Any]:
    """Evaluate one tool call against a read-only snapshot.

    Returns ``{"ok": True, "proposal": {...}}`` or
    ``{"ok": False, "error": str, "detail": str}``. Pure: no I/O, no clock, no
    ID generation, so the sandbox and the control plane always agree.
    """
    decider = _DECIDERS.get(tool)
    if decider is None:
        return _error("TOOL_NOT_ALLOWED", str(tool))
    if not isinstance(arguments, dict):
        return _error("INVALID_ARGUMENTS", "arguments must be an object")
    if not isinstance(snapshot, dict):
        return _error("INVALID_SNAPSHOT", "snapshot must be an object")
    return decider(arguments, snapshot)
