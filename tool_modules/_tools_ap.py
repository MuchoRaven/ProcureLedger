"""
tool_modules/_tools_ap.py
=========================
The accounts-payable public tools: vendor and invoice reads, the three-way
match, the payout-approval gate, and payout settlement.

These used to live in ``tools.py``. They are moved here so the AP surface can be
read without the procurement and sourcing tools. The bodies are unchanged.

``run_three_way_match`` and ``request_payout_approval`` are thin pass-throughs
to the apply helpers in ``tool_modules._apply_ap``; the rest are reads or
control-plane mutations that live entirely in this module.

This module imports ``_state`` (the store, its lock, and the commit helpers),
``_apply_ap`` (the two apply helpers) and ``_util`` (``_utc_today``). It also
imports ``decisions``, because ``get_invoice_aging`` reads
``decisions._aging_bucket`` so the direct call and the sandboxed decider cannot
drift. ``decisions`` imports nothing from ``tool_modules``, so that is not a
cycle. This module does not import ``tools``, so it cannot participate in one.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import decisions

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _INVOICES,
    _APPROVALS,
    ToolError,
    _now,
    _commit,
)
from tool_modules._apply_ap import (
    _apply_transition,
    _apply_create_approval,
)
from tool_modules._util import _utc_today


def lookup_vendor(vendor_id: str) -> dict[str, Any]:
    """Return a vendor record, or ``VENDOR_NOT_FOUND``."""
    if not isinstance(vendor_id, str) or not vendor_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "vendor_id must be a non-empty string",
        }
    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "detail": vendor_id}
        return {"ok": True, "vendor": dict(vendor)}


def get_invoice(invoice_id: str) -> dict[str, Any]:
    """Return an invoice record, or ``INVOICE_NOT_FOUND``."""
    if not isinstance(invoice_id, str) or not invoice_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "invoice_id must be a non-empty string",
        }
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {"ok": False, "error": "INVOICE_NOT_FOUND", "detail": invoice_id}
        return {"ok": True, "invoice": dict(invoice)}


def get_invoice_aging(invoice_id: str, as_of: str = "") -> dict[str, Any]:
    """Return the AP aging bucket for an invoice.

    ``as_of`` is an optional ISO date; when omitted the current UTC date is
    used. The bucket boundaries come from ``decisions._aging_bucket`` so the
    direct call and the sandboxed decider cannot drift.
    """
    if not isinstance(invoice_id, str) or not invoice_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "invoice_id must be a non-empty string",
        }
    if as_of is None:
        as_of = ""
    if not isinstance(as_of, str):
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "as_of must be a string",
        }
    as_of = as_of.strip()

    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {"ok": False, "error": "INVOICE_NOT_FOUND", "detail": invoice_id}
        due = invoice.get("due_date")

    reference = as_of or _utc_today()
    try:
        reference_date = date.fromisoformat(reference)
    except ValueError:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": f"as_of is not an ISO date: {reference}",
        }

    if not isinstance(due, str) or not due:
        return {
            "ok": False,
            "error": "AGING_UNAVAILABLE",
            "detail": f"invoice {invoice_id} has no due_date",
        }
    try:
        due_date = date.fromisoformat(due)
    except ValueError:
        return {
            "ok": False,
            "error": "AGING_UNAVAILABLE",
            "detail": f"invoice {invoice_id} has a bad due_date",
        }

    days_outstanding = (reference_date - due_date).days
    return {
        "ok": True,
        "aging": {
            "invoice_id": invoice_id,
            "as_of": reference_date.isoformat(),
            "due_date": due_date.isoformat(),
            "days_outstanding": days_outstanding,
            "bucket": decisions._aging_bucket(days_outstanding),
        },
    }


def run_three_way_match(
    invoice_id: str, po_id: str, correlation_id: str = ""
) -> dict[str, Any]:
    """Compare PO, goods receipt and invoice; set MATCHED or EXCEPTION."""
    return _apply_transition(
        {"kind": "transition", "invoice_id": invoice_id, "po_id": po_id},
        correlation_id,
    )


def request_payout_approval(
    vendor_id: str,
    amount: Any,
    po_id: str,
    invoice_id: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Open a human approval gate. Does NOT move money."""
    return _apply_create_approval(
        {
            "kind": "create_approval",
            "vendor_id": vendor_id,
            "amount": amount,
            "po_id": po_id,
            "invoice_id": invoice_id,
        },
        correlation_id,
    )


def ingest_invoice(
    invoice_id: str,
    vendor_id: str,
    po_id: str,
    amount: Any,
    currency: str = "USD",
    correlation_id: str = "",
) -> dict[str, Any]:
    """Record a new invoice. Raises ``ToolError`` if the audit write fails.

    The record is only inserted after the ``INVOICE_INGESTED`` audit link is
    durable, so a failed write leaves no invoice behind.
    """
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "invoice_id"})
    if not isinstance(vendor_id, str) or not vendor_id.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "vendor_id"})
    if not isinstance(po_id, str) or not po_id.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "po_id"})
    if not isinstance(currency, str) or not currency.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "currency"})
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ToolError("INVALID_AMOUNT", {"amount": amount})
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ToolError("INVALID_AMOUNT", {"amount": amount})

    invoice_id = invoice_id.strip()
    vendor_id = vendor_id.strip()
    po_id = po_id.strip()
    currency = currency.strip()

    now = _now()
    record = {
        "invoice_id": invoice_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "amount": amount,
        "currency": currency,
        "status": "RECEIVED",
        "receipt_ids": [],
        "created_at": now,
        "updated_at": now,
    }

    def check() -> None:
        with _LOCK:
            if invoice_id in _INVOICES:
                raise ToolError("INVOICE_EXISTS", {"invoice_id": invoice_id})
            if vendor_id not in _VENDORS:
                raise ToolError("VENDOR_NOT_FOUND", {"vendor_id": vendor_id})
            po = _POS.get(po_id)
            if po is None:
                raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
            if po.get("vendor_id") != vendor_id:
                raise ToolError(
                    "VENDOR_MISMATCH",
                    {
                        "po_vendor": po.get("vendor_id"),
                        "vendor_id": vendor_id,
                    },
                )

    def mutation() -> None:
        with _LOCK:
            _INVOICES[invoice_id] = record

    payload = {
        "invoice_id": invoice_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "amount": amount,
        "currency": currency,
    }

    _commit(mutation, "INVOICE_INGESTED", payload, correlation_id, check=check)
    return {"ok": True, "invoice": dict(record)}


def get_approval(approval_id: str) -> dict[str, Any]:
    """Return an approval record, or ``APPROVAL_NOT_FOUND``."""
    if not isinstance(approval_id, str) or not approval_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "approval_id must be a non-empty string",
        }
    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "detail": approval_id,
            }
        return {"ok": True, "approval": dict(approval)}


def approve_payout(
    approval_id: str, approver: str, correlation_id: str = ""
) -> dict[str, Any]:
    """Record a human approval decision. Does NOT move money."""
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "approval_id"})
    if not isinstance(approver, str) or not approver.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "approver"})
    approval_id = approval_id.strip()
    approver = approver.strip()

    def check() -> None:
        with _LOCK:
            approval = _APPROVALS.get(approval_id)
            if approval is None:
                raise ToolError(
                    "APPROVAL_NOT_FOUND", {"approval_id": approval_id}
                )
            if approval.get("status") != "PENDING":
                raise ToolError(
                    "APPROVAL_NOT_PENDING", {"status": approval.get("status")}
                )

    def mutation() -> None:
        with _LOCK:
            approval = _APPROVALS[approval_id]
            stamp = _now()
            approval["status"] = "APPROVED"
            approval["approver"] = approver
            approval["approved_at"] = stamp
            approval["updated_at"] = stamp

    payload = {"approval_id": approval_id, "approver": approver}
    _commit(mutation, "PAYOUT_APPROVED", payload, correlation_id, check=check)

    with _LOCK:
        return {"ok": True, "approval": dict(_APPROVALS[approval_id])}


def _execute_payout(
    approval_id: str, executor: str, correlation_id: str = ""
) -> dict[str, Any]:
    """Settle an approved payout. Refuses when executor == approver."""
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "approval_id"})
    if not isinstance(executor, str) or not executor.strip():
        raise ToolError("INVALID_ARGUMENTS", {"field": "executor"})
    approval_id = approval_id.strip()
    executor = executor.strip()

    def check() -> None:
        with _LOCK:
            approval = _APPROVALS.get(approval_id)
            if approval is None:
                raise ToolError(
                    "APPROVAL_NOT_FOUND", {"approval_id": approval_id}
                )
            if approval.get("status") != "APPROVED":
                raise ToolError(
                    "APPROVAL_NOT_APPROVED", {"status": approval.get("status")}
                )
            if approval.get("approver") == executor:
                raise ToolError(
                    "SEPARATION_OF_DUTIES",
                    {
                        "approver": approval.get("approver"),
                        "executor": executor,
                    },
                )

    def mutation() -> None:
        with _LOCK:
            approval = _APPROVALS[approval_id]
            stamp = _now()
            approval["status"] = "EXECUTED"
            approval["executor"] = executor
            approval["executed_at"] = stamp
            approval["updated_at"] = stamp

    payload = {"approval_id": approval_id, "executor": executor}
    _commit(mutation, "PAYOUT_EXECUTED", payload, correlation_id, check=check)

    with _LOCK:
        return {"ok": True, "approval": dict(_APPROVALS[approval_id])}
