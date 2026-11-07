"""
tool_modules/_apply_procurement.py
==================================
The procurement half of the control plane: requisitions, purchase orders,
goods receipts, and the PO/requisition status changes.

These helpers used to live in ``tools.py``. They are moved here so the
procurement apply path can be read without the whole tool layer. The bodies are
unchanged.

Every helper follows the same contract as the rest of the control plane:
validate under ``_LOCK``, write the audit link before the mutation, and
generate IDs/timestamps only in the apply step. The ``_check_*`` helpers are
the fallible validation that ``_commit`` runs first, so a proposal that raced
past ``_redeci`` is rejected before an audit entry is written for it.

This module imports only ``_state`` (the store, its lock, and the status
values) and ``_util`` (``_as_float``). It deliberately does not import
``tools``, ``decisions`` or ``sandbox``, so it cannot participate in an import
cycle.
"""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Any, Optional

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _RECEIPTS,
    _REQUISITIONS,
    ToolError,
    _now,
    _commit,
    PO_STATUS_OPEN,
    PO_STATUS_CLOSED,
    PO_STATUS_CANCELLED,
    REQ_STATUS_OPEN,
    REQ_STATUS_CONVERTED,
    REQ_STATUS_CANCELLED,
    AMOUNT_TOLERANCE,
)
from tool_modules._util import _as_float


def _check_create_requisition(vendor_id: Optional[str]) -> None:
    """Validate a requisition request without changing anything.

    ``vendor_id`` is optional (a requisition may be raised before a vendor is
    chosen, as ``REQ-4002`` in the seed shows). When it is supplied it must
    name an ACTIVE vendor, mirroring ``_check_create_po``.
    """
    with _LOCK:
        if not vendor_id:
            return
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            raise ToolError("VENDOR_NOT_FOUND", {"vendor_id": vendor_id})
        if vendor.get("status") != "ACTIVE":
            raise ToolError(
                "VENDOR_INACTIVE",
                {"vendor_id": vendor_id, "status": vendor.get("status")},
            )


def _apply_create_requisition(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``create_requisition`` proposal.

    Generates the requisition ID and timestamps here (the only
    non-deterministic step). The new requisition starts ``OPEN`` with no PO;
    ``_apply_create_po`` later flips it to ``CONVERTED`` inside its own
    mutation, so no separate status event is written for that transition.
    """
    description = proposal.get("description")
    vendor_id = proposal.get("vendor_id") or None
    quantity = proposal.get("quantity")
    amount = proposal.get("amount")
    currency = proposal.get("currency", "USD")

    if not isinstance(description, str) or not description.strip():
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "description",
        }
    description = description.strip()
    if vendor_id is not None and (
        not isinstance(vendor_id, str) or not vendor_id.strip()
    ):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "vendor_id"}
    if vendor_id is not None:
        vendor_id = vendor_id.strip()
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "quantity"}
    quantity = float(quantity)
    if not math.isfinite(quantity) or quantity <= 0:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "quantity"}
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}
    if not isinstance(currency, str) or not currency.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "currency"}
    currency = currency.strip()

    requisition_id = "REQ-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    record = {
        "requisition_id": requisition_id,
        "description": description,
        "vendor_id": vendor_id,
        "po_id": None,
        "quantity": quantity,
        "amount": amount,
        "currency": currency,
        "status": REQ_STATUS_OPEN,
        "requested_by": "agent",
        "created_at": now,
        "updated_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _REQUISITIONS[requisition_id] = record

    payload = {
        "requisition_id": requisition_id,
        "description": description,
        "vendor_id": vendor_id,
        "quantity": quantity,
        "amount": amount,
        "currency": currency,
    }

    try:
        _commit(
            mutation,
            "REQUISITION_CREATED",
            payload,
            correlation_id,
            check=lambda: _check_create_requisition(vendor_id),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "requisition": dict(record)}


def _check_create_po(
    vendor_id: str, amount: float, requisition_id: Optional[str]
) -> None:
    """Validate a PO-creation request without changing anything.

    ``amount`` is accepted for symmetry with the other ``_check_*`` helpers
    and to keep the call site readable; the amount itself is validated as a
    malformed-proposal check in ``_apply_create_po``. The business rules here
    are the ones that can race: the vendor must still be ACTIVE, and a linked
    requisition must still be OPEN.
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
        if requisition_id:
            req = _REQUISITIONS.get(requisition_id)
            if req is None:
                raise ToolError(
                    "REQUISITION_NOT_FOUND",
                    {"requisition_id": requisition_id},
                )
            if req.get("status") != REQ_STATUS_OPEN:
                raise ToolError(
                    "REQUISITION_NOT_OPEN",
                    {"status": req.get("status")},
                )


def _apply_create_po(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``create_po`` proposal.

    Generates the PO ID and timestamps here. When a ``requisition_id`` is
    supplied, the requisition is flipped to ``CONVERTED`` inside the same
    mutation, so the status change is captured by the ``PO_CREATED`` payload's
    ``requisition_id`` rather than a second event. ``_commit`` writes exactly
    one audit link, and splitting it into two would break audit-before-
    mutation for the second; ``REQUISITION_STATUS_CHANGED`` is therefore
    reserved for the explicit cancel path.
    """
    vendor_id = proposal.get("vendor_id")
    amount = proposal.get("amount")
    currency = proposal.get("currency", "USD")
    expected_delivery = proposal.get("expected_delivery", "")
    requisition_id = proposal.get("requisition_id") or None

    if not isinstance(vendor_id, str) or not vendor_id:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "vendor_id"}
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}
    if not isinstance(currency, str) or not currency.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "currency"}
    currency = currency.strip()
    if not isinstance(expected_delivery, str):
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "expected_delivery",
        }
    expected_delivery = expected_delivery.strip()
    if expected_delivery:
        try:
            expected_delivery = date.fromisoformat(expected_delivery).isoformat()
        except ValueError:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENTS",
                "detail": "expected_delivery is not an ISO date",
            }

    po_id = "PO-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    record = {
        "po_id": po_id,
        "vendor_id": vendor_id,
        "amount": amount,
        "currency": currency,
        "status": PO_STATUS_OPEN,
        "expected_delivery": expected_delivery,
        "requisition_id": requisition_id,
        "created_at": now,
        "updated_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _POS[po_id] = record
            if requisition_id and requisition_id in _REQUISITIONS:
                req = _REQUISITIONS[requisition_id]
                req["status"] = REQ_STATUS_CONVERTED
                req["po_id"] = po_id
                req["updated_at"] = now

    payload = {
        "po_id": po_id,
        "vendor_id": vendor_id,
        "amount": amount,
        "currency": currency,
        "expected_delivery": expected_delivery,
        "requisition_id": requisition_id,
    }

    try:
        _commit(
            mutation,
            "PO_CREATED",
            payload,
            correlation_id,
            check=lambda: _check_create_po(vendor_id, amount, requisition_id),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "po": dict(record)}


def _check_record_receipt(po_id: str, amount: float) -> None:
    """Validate a goods-receipt request without changing anything.

    The PO must still be OPEN, and the cumulative received amount (existing
    receipts plus this one) must not exceed the PO amount beyond
    ``AMOUNT_TOLERANCE``. This is the over-receipt control the
    ``record_goods_receipt`` decider relies on the snapshot to detect; the
    check here is the authoritative one, run under ``_LOCK``.
    """
    with _LOCK:
        po = _POS.get(po_id)
        if po is None:
            raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
        if po.get("status") != PO_STATUS_OPEN:
            raise ToolError("PO_NOT_OPEN", {"status": po.get("status")})
        received = sum(
            _as_float(r.get("amount"))
            for r in _RECEIPTS.values()
            if r.get("po_id") == po_id
        )
        if received + amount > _as_float(po.get("amount")) + AMOUNT_TOLERANCE:
            raise ToolError(
                "RECEIPT_EXCEEDS_PO",
                {
                    "po_amount": po.get("amount"),
                    "received": received,
                    "requested": amount,
                },
            )


def _apply_record_receipt(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``record_receipt`` proposal.

    Generates the receipt ID and timestamp here. The over-receipt check runs
    under ``_LOCK`` in ``_check_record_receipt`` before the audit link is
    written, so a proposal that raced past ``_redeci`` cannot over-receive.
    """
    po_id = proposal.get("po_id")
    amount = proposal.get("amount")
    if not isinstance(po_id, str) or not po_id:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "po_id"}
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "amount"}

    receipt_id = "RCPT-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    record = {
        "receipt_id": receipt_id,
        "po_id": po_id,
        "amount": amount,
        "received_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _RECEIPTS[receipt_id] = record

    payload = {"receipt_id": receipt_id, "po_id": po_id, "amount": amount}

    try:
        _commit(
            mutation,
            "GOODS_RECEIPT_RECORDED",
            payload,
            correlation_id,
            check=lambda: _check_record_receipt(po_id, amount),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "receipt": dict(record)}


def _apply_po_status(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``po_status`` proposal (close or cancel an OPEN PO).

    Only ``CLOSED`` and ``CANCELLED`` are accepted; the OPEN -> OPEN no-op is
    rejected as malformed. The check re-reads the PO under ``_LOCK`` so a
    proposal that raced past ``_redeci`` cannot close a PO that has already
    moved on.
    """
    po_id = proposal.get("po_id")
    to_status = proposal.get("to_status")
    if not isinstance(po_id, str) or not po_id:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "po_id"}
    if to_status not in (PO_STATUS_CLOSED, PO_STATUS_CANCELLED):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "to_status"}

    def check() -> None:
        with _LOCK:
            po = _POS.get(po_id)
            if po is None:
                raise ToolError("PO_NOT_FOUND", {"po_id": po_id})
            if po.get("status") != PO_STATUS_OPEN:
                raise ToolError("PO_NOT_OPEN", {"status": po.get("status")})

    def mutation() -> None:
        with _LOCK:
            po = _POS[po_id]
            po["status"] = to_status
            po["updated_at"] = _now()

    payload = {"po_id": po_id, "from": PO_STATUS_OPEN, "to": to_status}

    try:
        _commit(
            mutation, "PO_STATUS_CHANGED", payload, correlation_id, check=check
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    with _LOCK:
        return {"ok": True, "po": dict(_POS[po_id])}


def _check_requisition_status(requisition_id: str) -> None:
    """Validate a requisition-status change without changing anything.

    Only an OPEN requisition may be cancelled; anything else is rejected so a
    proposal that raced past ``_redeci`` cannot re-cancel a requisition that
    has already been converted or cancelled.
    """
    with _LOCK:
        req = _REQUISITIONS.get(requisition_id)
        if req is None:
            raise ToolError(
                "REQUISITION_NOT_FOUND", {"requisition_id": requisition_id}
            )
        if req.get("status") != REQ_STATUS_OPEN:
            raise ToolError(
                "REQUISITION_NOT_OPEN", {"status": req.get("status")}
            )


def _apply_requisition_status(
    proposal: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    """Apply a ``requisition_status`` proposal (cancel an OPEN requisition).

    Only ``CANCELLED`` is accepted. The ``OPEN -> CONVERTED`` transition is
    performed inside ``_apply_create_po``'s mutation, so it is captured by the
    ``PO_CREATED`` payload rather than a second event; this helper exists for
    the explicit cancel path only.
    """
    requisition_id = proposal.get("requisition_id")
    to_status = proposal.get("to_status")
    if not isinstance(requisition_id, str) or not requisition_id:
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "requisition_id",
        }
    if to_status != REQ_STATUS_CANCELLED:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "to_status"}

    def check() -> None:
        _check_requisition_status(requisition_id)

    def mutation() -> None:
        with _LOCK:
            req = _REQUISITIONS[requisition_id]
            req["status"] = to_status
            req["updated_at"] = _now()

    payload = {
        "requisition_id": requisition_id,
        "from": REQ_STATUS_OPEN,
        "to": to_status,
    }

    try:
        _commit(
            mutation,
            "REQUISITION_STATUS_CHANGED",
            payload,
            correlation_id,
            check=check,
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    with _LOCK:
        return {"ok": True, "requisition": dict(_REQUISITIONS[requisition_id])}
