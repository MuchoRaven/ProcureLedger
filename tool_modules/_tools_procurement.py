"""
tool_modules/_tools_procurement.py
==================================
The procurement public tools: PO and requisition reads, PO creation, goods
receipts, and the PO close/cancel paths.

These used to live in ``tools.py``. They are moved here so the procurement
surface can be read without the sourcing tools. The bodies are unchanged.

``create_po``, ``record_goods_receipt``, ``close_po`` and ``cancel_po`` are
thin pass-throughs to the apply helpers in
``tool_modules._apply_procurement``; ``get_po`` and ``get_requisition`` are
plain reads. All six are app-layer only: none is in
``decisions.PROPOSAL_KINDS``, so the model cannot reach them.

This module imports ``_state`` (the store, its lock, and the PO status values)
and ``_apply_procurement`` (the four apply helpers). It does not import
``tools``, ``decisions`` or ``sandbox``, so it cannot participate in an import
cycle.
"""

from __future__ import annotations

from typing import Any

from tool_modules._state import (
    _LOCK,
    _POS,
    _REQUISITIONS,
    PO_STATUS_CLOSED,
    PO_STATUS_CANCELLED,
)
from tool_modules._apply_procurement import (
    _apply_create_po,
    _apply_record_receipt,
    _apply_po_status,
)


def get_po(po_id: str) -> dict[str, Any]:
    """Return a PO record, or ``PO_NOT_FOUND``.

    App-layer only: it is not in ``decisions.PROPOSAL_KINDS``, so the model
    cannot reach it. A plain read in the style of ``get_invoice`` - it reports
    a missing record in-band rather than raising.
    """
    if not isinstance(po_id, str) or not po_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "po_id must be a non-empty string",
        }
    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "detail": po_id}
        return {"ok": True, "po": dict(po)}


def get_requisition(requisition_id: str) -> dict[str, Any]:
    """Return a requisition record, or ``REQUISITION_NOT_FOUND``.

    App-layer only, for the same reason as ``get_po``.
    """
    if not isinstance(requisition_id, str) or not requisition_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "requisition_id must be a non-empty string",
        }
    with _LOCK:
        requisition = _REQUISITIONS.get(requisition_id)
        if requisition is None:
            return {
                "ok": False,
                "error": "REQUISITION_NOT_FOUND",
                "detail": requisition_id,
            }
        return {"ok": True, "requisition": dict(requisition)}


def create_po(
    vendor_id: str,
    amount: Any,
    currency: str = "USD",
    expected_delivery: str = "",
    requisition_id: str = "",
    correlation_id: str = "",
) -> dict[str, Any]:
    """Create a purchase order. App-layer only; never reachable by the model.

    A thin pass-through to ``_apply_create_po`` so the ID generation, the
    ``_LOCK`` re-checks and the audit-before-mutation ordering all stay in one
    place. ``requisition_id`` is normalised to ``None`` when blank, matching
    the proposal contract.
    """
    return _apply_create_po(
        {
            "kind": "create_po",
            "vendor_id": vendor_id,
            "amount": amount,
            "currency": currency,
            "expected_delivery": expected_delivery,
            "requisition_id": requisition_id or None,
        },
        correlation_id,
    )


def record_goods_receipt(
    po_id: str, amount: Any, correlation_id: str = ""
) -> dict[str, Any]:
    """Record a goods receipt against an OPEN PO. App-layer only.

    The over-receipt control lives in ``_check_record_receipt`` and runs under
    ``_LOCK`` before the audit link is written.
    """
    return _apply_record_receipt(
        {"kind": "record_receipt", "po_id": po_id, "amount": amount},
        correlation_id,
    )


def close_po(po_id: str, correlation_id: str = "") -> dict[str, Any]:
    """Close an OPEN PO. App-layer only."""
    return _apply_po_status(
        {"kind": "po_status", "po_id": po_id, "to_status": PO_STATUS_CLOSED},
        correlation_id,
    )


def cancel_po(po_id: str, correlation_id: str = "") -> dict[str, Any]:
    """Cancel an OPEN PO. App-layer only."""
    return _apply_po_status(
        {"kind": "po_status", "po_id": po_id, "to_status": PO_STATUS_CANCELLED},
        correlation_id,
    )
