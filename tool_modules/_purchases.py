"""
tool_modules/_purchases.py
==========================
Read-only purchase monitoring: one PO, or every PO, with derived progress
fields.

This module is read-only, operator-only and app-layer only. Neither
``purchase_status`` nor ``list_purchases`` appears in
``decisions.PROPOSAL_KINDS`` or ``decisions.SNAPSHOT_SCOPES``, so the model
cannot reach them; they are exposed only through ``app.py``'s operator-only
``/purchases`` endpoints. Nothing here writes an audit event - there is no
``_commit``, no ``_append_audit`` and no ``log_audit_event`` - because a read
that appended to the chain would turn every monitoring poll into an audit
entry and make the chain useless as a record of what changed.

The stores are read from ``_state`` (``_POS``, ``_RECEIPTS``, ``_INVOICES``,
``_VENDORS``) under ``_LOCK`` and copied out; the rows are then built outside
the lock, so a slow report never blocks a mutation. ``as_of`` is the reference
date for the late check: an empty value means today (UTC), and an explicit ISO
date pins the report so a test or a backfill can reproduce it.

This module imports only ``_state`` and ``_util``. It deliberately does not
import ``tools``, ``decisions``, ``sandbox`` or ``_followup``, so it cannot
participate in an import cycle. ``_resolve_as_of`` is a local copy of
``_followup._resolve_as_of`` for the same reason: the two must agree on what an
empty or malformed ``as_of`` means, but this module must not depend on the
follow-up module to say so.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _RECEIPTS,
    _INVOICES,
    ToolError,
    _today,
    AMOUNT_TOLERANCE,
)
from tool_modules._util import _as_float


def _resolve_as_of(as_of: str) -> str:
    """Return ``as_of`` as an ISO date, defaulting to today.

    An empty value means "today", so a caller that omits the argument gets a
    deterministic reference rather than an empty string that would make every
    comparison false. A malformed value raises ``ToolError`` rather than being
    silently ignored, matching ``_followup._resolve_as_of``.
    """
    text = (as_of or "").strip()
    if not text:
        return _today()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ToolError("INVALID_ARGUMENTS", {"as_of": as_of})


def _purchase_row(
    po: Dict[str, Any],
    receipts: List[Dict[str, Any]],
    invoices: List[Dict[str, Any]],
    vendors: Dict[str, Any],
    reference: str,
) -> Dict[str, Any]:
    """Build one purchase-status row.

    ``receipts`` and ``invoices`` are already filtered to this PO, and
    ``reference`` is the ISO date from ``_resolve_as_of``. The clock is never
    read here: the reference date is the caller's, so the row is a pure
    function of its arguments and a pinned ``as_of`` reproduces exactly.

    ``amount`` is coerced with ``_as_float`` rather than copied raw, so the
    reported field and the arithmetic below cannot disagree on a malformed
    store value. The same coercion is used for the receipt and invoice sums.
    """
    reference_date = date.fromisoformat(reference)

    amount = _as_float(po.get("amount"))
    received_amount = sum(_as_float(r.get("amount")) for r in receipts)
    invoiced_amount = sum(_as_float(i.get("amount")) for i in invoices)

    # A PO is late only while it is still OPEN and its delivery date is a real
    # ISO date strictly before the reference. An empty or malformed
    # ``expected_delivery`` is a data gap, not a late delivery: ``"" <
    # "2024-03-01"`` is True lexicographically, so without the guard an
    # unscheduled PO would report as late on every run.
    expected = po.get("expected_delivery")
    is_late = False
    days_late = 0
    if po.get("status") == "OPEN" and isinstance(expected, str) and expected:
        try:
            expected_date: Optional[date] = date.fromisoformat(expected)
        except ValueError:
            expected_date = None
        if expected_date is not None and expected_date < reference_date:
            is_late = True
            days_late = (reference_date - expected_date).days

    is_fully_received = received_amount >= amount - AMOUNT_TOLERANCE
    is_fully_invoiced = invoiced_amount >= received_amount - AMOUNT_TOLERANCE

    vendor = vendors.get(po.get("vendor_id"), {})

    return {
        "po_id": po.get("po_id"),
        "vendor_id": po.get("vendor_id"),
        "vendor_name": vendor.get("name", ""),
        "status": po.get("status"),
        "currency": po.get("currency"),
        "amount": amount,
        "expected_delivery": po.get("expected_delivery"),
        "requisition_id": po.get("requisition_id"),
        "created_at": po.get("created_at"),
        "received_amount": received_amount,
        "invoiced_amount": invoiced_amount,
        "outstanding_receipt": round(max(0.0, amount - received_amount), 2),
        "outstanding_invoice": round(
            max(0.0, received_amount - invoiced_amount), 2
        ),
        "receipt_count": len(receipts),
        "invoice_count": len(invoices),
        "last_receipt_at": max(
            (r.get("received_at") or "" for r in receipts), default=""
        ),
        "is_fully_received": is_fully_received,
        "is_fully_invoiced": is_fully_invoiced,
        "is_late": is_late,
        "days_late": days_late,
        "is_outstanding": (
            not is_fully_received or not is_fully_invoiced or is_late
        ),
        "as_of": reference,
    }


def purchase_status(po_id: str, as_of: str = "") -> Dict[str, Any]:
    """Return one PO with its derived purchase status.

    ``as_of`` is an optional ISO date used for the late check; an empty value
    means today (UTC). A malformed ``as_of`` raises
    ``ToolError("INVALID_ARGUMENTS")``, which ``app.py`` renders as a 400.

    The PO, its receipts, its invoices and its vendor are copied out under
    ``_LOCK``; the row is built outside it. Only the PO's own vendor is copied
    - a single-PO read has no reason to widen to the whole vendor table.
    """
    if not isinstance(po_id, str) or not po_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "po_id must be a non-empty string",
        }

    reference = _resolve_as_of(as_of)

    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "detail": po_id}
        po_copy = dict(po)
        receipts = [
            dict(receipt)
            for receipt in _RECEIPTS.values()
            if receipt.get("po_id") == po_id
        ]
        invoices = [
            dict(invoice)
            for invoice in _INVOICES.values()
            if invoice.get("po_id") == po_id
        ]
        vendor_id = po.get("vendor_id")
        vendors: Dict[str, Any] = {}
        if vendor_id in _VENDORS:
            vendors[vendor_id] = dict(_VENDORS[vendor_id])

    return {
        "ok": True,
        "purchase": _purchase_row(
            po_copy, receipts, invoices, vendors, reference
        ),
    }


def list_purchases(as_of: str = "") -> Dict[str, Any]:
    """Return every PO with its derived purchase status, sorted by ``po_id``.

    ``as_of`` behaves as in ``purchase_status``. The whole store is copied out
    under ``_LOCK`` and the rows are built outside it, so a large report does
    not hold the lock while it formats. Receipts and invoices are grouped by
    ``po_id`` once rather than rescanned per PO.
    """
    reference = _resolve_as_of(as_of)

    with _LOCK:
        pos = {po_id: dict(po) for po_id, po in _POS.items()}
        receipts = [dict(receipt) for receipt in _RECEIPTS.values()]
        invoices = [dict(invoice) for invoice in _INVOICES.values()]
        vendors = {vid: dict(vendor) for vid, vendor in _VENDORS.items()}

    # ``po_id`` may be missing on a malformed record, so the key is optional.
    receipts_by_po: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for receipt in receipts:
        receipts_by_po.setdefault(receipt.get("po_id"), []).append(receipt)

    invoices_by_po: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for invoice in invoices:
        invoices_by_po.setdefault(invoice.get("po_id"), []).append(invoice)

    rows = [
        _purchase_row(
            pos[po_id],
            receipts_by_po.get(po_id, []),
            invoices_by_po.get(po_id, []),
            vendors,
            reference,
        )
        for po_id in sorted(pos)
    ]

    return {"ok": True, "as_of": reference, "purchases": rows}
