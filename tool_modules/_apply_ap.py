"""
tool_modules/_apply_ap.py
=========================
The accounts-payable half of the control plane: three-way match, payout
approval, and the agent's audit note.

These helpers used to live in ``tools.py``. They are moved here so the AP apply
path can be read without the whole tool layer. The bodies are unchanged.

``_apply_audit_note`` delegates to ``log_audit_event``, which lives in
``tool_modules._tools_audit``. That module imports only ``_state``, so the
import is at module scope: there is no cycle back to ``tools``.
"""

from __future__ import annotations

import uuid
from typing import Any

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _RECEIPTS,
    _INVOICES,
    _APPROVALS,
    ToolError,
    _now,
    _commit,
    _transition,
    _check_transition,
    AMOUNT_TOLERANCE,
)
from tool_modules._tools_audit import log_audit_event
from tool_modules._util import _as_float


def _receipts_for_invoice(invoice: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the receipt records linked to ``invoice``.

    Invoices may carry either a single ``receipt_id`` or a ``receipt_ids``
    list; both are honoured and de-duplicated. Caller must hold ``_LOCK``.
    """
    receipts: list[dict[str, Any]] = []
    seen = set()

    def add(rid: Any) -> None:
        if isinstance(rid, str) and rid in _RECEIPTS and rid not in seen:
            receipts.append(_RECEIPTS[rid])
            seen.add(rid)

    linked = invoice.get("receipt_ids")
    if isinstance(linked, (list, tuple)):
        for rid in linked:
            add(rid)
    add(invoice.get("receipt_id"))
    return receipts


def _three_way_match(
    invoice: dict[str, Any],
    po: dict[str, Any],
    receipts: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Compare invoice, PO and received amounts.

    A receipt carries an ``amount`` field: the value of the goods it records.
    The match succeeds only when the invoice agrees with both the PO and the
    total received amount within ``AMOUNT_TOLERANCE``. A short (or missing)
    receipt therefore yields ``EXCEPTION``, which is the AP control the
    decider in ``decisions`` deliberately leaves to this step.
    """
    invoice_amount = _as_float(invoice.get("amount"))
    po_amount = _as_float(po.get("amount"))
    received_amount = sum(_as_float(r.get("amount")) for r in receipts)

    po_ok = abs(invoice_amount - po_amount) <= AMOUNT_TOLERANCE
    receipt_ok = abs(invoice_amount - received_amount) <= AMOUNT_TOLERANCE
    matched = po_ok and receipt_ok

    return matched, {
        "invoice_amount": invoice_amount,
        "po_amount": po_amount,
        "received_amount": received_amount,
        "po_ok": po_ok,
        "receipt_ok": receipt_ok,
    }


def _evaluate_three_way_match(
    invoice_id: str, po_id: str
) -> tuple[bool, dict[str, Any]]:
    """Re-read live state and compute the match outcome.

    Raises ``ToolError`` if the invoice or PO is missing or inconsistent, so
    the caller can surface the same codes the decider would have.
    """
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            raise ToolError("INVOICE_NOT_FOUND", {"invoice_id": invoice_id})
        po = _POS.get(po_id)
        if po is None:
            raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
        if invoice.get("po_id") != po_id:
            raise ToolError(
                "PO_INVOICE_MISMATCH",
                {"invoice_id": invoice_id, "po_id": po_id},
            )
        if invoice.get("vendor_id") != po.get("vendor_id"):
            raise ToolError(
                "VENDOR_MISMATCH",
                {
                    "invoice_vendor": invoice.get("vendor_id"),
                    "po_vendor": po.get("vendor_id"),
                },
            )
        receipts = _receipts_for_invoice(invoice)
        return _three_way_match(invoice, po, receipts)


def _check_create_approval(
    vendor_id: str, po_id: str, invoice_id: str, amount: float
) -> None:
    """Validate a payout-approval request without changing anything.

    Mirrors the checks in ``decisions._decide_request_payout_approval`` so a
    proposal that raced past ``_redeci`` is rejected before an audit link is
    written for it.
    """
    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            raise ToolError("VENDOR_NOT_FOUND", {"vendor_id": vendor_id})
        if vendor.get("status") != "ACTIVE":
            raise ToolError(
                "VENDOR_INACTIVE",
                {"vendor_id": vendor_id, "status": vendor.get("status")},
            )
        if vendor.get("risk_tier") == "HIGH":
            raise ToolError("VENDOR_HIGH_RISK", {"vendor_id": vendor_id})

        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            raise ToolError("INVOICE_NOT_FOUND", {"invoice_id": invoice_id})
        if invoice.get("vendor_id") != vendor_id:
            raise ToolError(
                "VENDOR_MISMATCH",
                {
                    "invoice_vendor": invoice.get("vendor_id"),
                    "vendor_id": vendor_id,
                },
            )
        if invoice.get("po_id") != po_id:
            raise ToolError(
                "PO_INVOICE_MISMATCH",
                {"invoice_po": invoice.get("po_id"), "po_id": po_id},
            )
        if invoice.get("status") != "MATCHED":
            raise ToolError(
                "INVOICE_NOT_MATCHED", {"status": invoice.get("status")}
            )
        if abs(float(amount) - _as_float(invoice.get("amount"))) > AMOUNT_TOLERANCE:
            raise ToolError(
                "AMOUNT_MISMATCH",
                {"requested": amount, "invoice": invoice.get("amount")},
            )
        for existing in _APPROVALS.values():
            if (
                existing.get("invoice_id") == invoice_id
                and existing.get("status") in ("PENDING", "APPROVED")
            ):
                raise ToolError(
                    "APPROVAL_ALREADY_PENDING",
                    {"approval_id": existing.get("approval_id")},
                )


def _apply_transition(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``transition`` proposal (three-way match).

    The proposal carries inputs only; the outcome is recomputed here from live
    state so the sandbox cannot dictate MATCHED vs EXCEPTION.
    """
    invoice_id = proposal.get("invoice_id")
    po_id = proposal.get("po_id")
    if not isinstance(invoice_id, str) or not isinstance(po_id, str):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "missing ids"}

    try:
        matched, detail = _evaluate_three_way_match(invoice_id, po_id)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    to_state = "MATCHED" if matched else "EXCEPTION"
    payload = {
        "invoice_id": invoice_id,
        "po_id": po_id,
        "outcome": to_state,
        **detail,
    }

    try:
        _commit(
            lambda: _transition(
                invoice_id, ("RECEIVED", "EXCEPTION"), to_state, correlation_id
            ),
            "THREE_WAY_MATCH",
            payload,
            correlation_id,
            check=lambda: _check_transition(
                invoice_id, ("RECEIVED", "EXCEPTION"), to_state
            ),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {
        "ok": True,
        "invoice_id": invoice_id,
        "po_id": po_id,
        "status": to_state,
        "match": detail,
    }


def _apply_create_approval(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``create_approval`` proposal.

    Generates the approval ID and timestamp here (the only non-deterministic
    step) and re-checks the invoice state and duplicate approvals under
    ``_LOCK`` so a proposal that raced past ``_redeci`` cannot create a second
    pending approval or approve an invoice that is no longer MATCHED.
    """
    vendor_id = proposal.get("vendor_id")
    po_id = proposal.get("po_id")
    invoice_id = proposal.get("invoice_id")
    amount = proposal.get("amount")
    if (
        not isinstance(vendor_id, str)
        or not isinstance(po_id, str)
        or not isinstance(invoice_id, str)
        or not isinstance(amount, (int, float))
        or isinstance(amount, bool)
    ):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "bad fields"}
    amount = float(amount)

    approval_id = "APR-" + uuid.uuid4().hex[:12].upper()
    now = _now()
    record = {
        "approval_id": approval_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "invoice_id": invoice_id,
        "amount": amount,
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _APPROVALS[approval_id] = record

    payload = {
        "approval_id": approval_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "invoice_id": invoice_id,
        "amount": amount,
    }

    try:
        _commit(
            mutation,
            "PAYOUT_APPROVAL_REQUESTED",
            payload,
            correlation_id,
            check=lambda: _check_create_approval(
                vendor_id, po_id, invoice_id, amount
            ),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "approval": dict(record)}


def _apply_audit_note(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply an ``audit_note`` proposal.

    Delegates to ``log_audit_event`` with ``source="agent"`` so the reserved
    event types stay control-plane only and the note is distinguishable from
    control-plane events in the chain.
    """
    event_type = proposal.get("event_type")
    payload = proposal.get("payload")
    if not isinstance(event_type, str) or not event_type:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "event_type"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "payload"}

    try:
        return log_audit_event(
            event_type, payload, correlation_id, source="agent"
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}
