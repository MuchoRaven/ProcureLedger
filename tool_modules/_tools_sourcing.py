"""
tool_modules/_tools_sourcing.py
===============================
The sourcing public tools: RFQ creation and reads, quote submission and
listing, RFQ close/cancel, and the award path.

These used to live in ``tools.py``. They are moved here so the sourcing surface
can be read on its own. The bodies are unchanged.

All eight are app-layer only: none is in ``decisions.PROPOSAL_KINDS``, so the
model cannot reach them. They follow the same contract as the rest of the
control plane - validate under ``_LOCK``, write the audit link before the
mutation, and generate IDs/timestamps only in the apply step. The validation
helpers (``_check_create_rfq``, ``_check_submit_quote``) and the RFQ status
change (``_apply_rfq_status``) live in ``tool_modules._apply_sourcing``.

``award_quote`` raises its PO through ``create_po``, which lives in
``tool_modules._tools_procurement``. That is the only cross-tool-module edge in
the package, and it is acyclic: ``_tools_procurement`` imports ``_state`` and
``_apply_procurement``, neither of which reaches back here.

This module does not import ``tools``, ``decisions`` or ``sandbox``, so it
cannot participate in an import cycle.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from typing import Any

from tool_modules._state import (
    _LOCK,
    _RFQS,
    _QUOTES,
    _POS,
    ToolError,
    _now,
    _commit,
)
from tool_modules._codes import (
    RFQ_NOT_FOUND,
    RFQ_NOT_OPEN,
    QUOTE_NOT_FOUND,
    QUOTE_NOT_SUBMITTED,
    RFQ_STATUS_OPEN,
    RFQ_STATUS_CLOSED,
    RFQ_STATUS_CANCELLED,
    RFQ_STATUS_AWARDED,
    QUOTE_STATUS_SUBMITTED,
    QUOTE_STATUS_AWARDED,
    QUOTE_STATUS_REJECTED,
)
from tool_modules._util import _as_float, _utc_today
from tool_modules._apply_sourcing import (
    _check_create_rfq,
    _check_submit_quote,
    _apply_rfq_status,
)
from tool_modules._tools_procurement import create_po


def create_rfq(
    description: str,
    quantity: Any,
    currency: str,
    vendor_ids: Any,
    due_date: str = "",
    correlation_id: str = "",
) -> dict[str, Any]:
    """Open a request for quotes. App-layer only; never reachable by the model.

    Validates the arguments, then re-checks every vendor under ``_LOCK`` in
    ``_check_create_rfq`` before the ``RFQ_CREATED`` audit link is written, so
    a vendor that went inactive cannot be quoted against.
    """
    if not isinstance(description, str) or not description.strip():
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "description",
        }
    description = description.strip()
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "quantity"}
    quantity = float(quantity)
    if not math.isfinite(quantity) or quantity <= 0:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "quantity"}
    if not isinstance(currency, str) or not currency.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "currency"}
    currency = currency.strip()
    if isinstance(vendor_ids, str) or not isinstance(vendor_ids, (list, tuple)):
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "vendor_ids",
        }
    cleaned: list[str] = []
    for vid in vendor_ids:
        if not isinstance(vid, str) or not vid.strip():
            return {
                "ok": False,
                "error": "MALFORMED_PROPOSAL",
                "detail": "vendor_ids",
            }
        vid = vid.strip()
        if vid not in cleaned:
            cleaned.append(vid)
    if not cleaned:
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "vendor_ids",
        }
    if due_date is None:
        due_date = ""
    if not isinstance(due_date, str):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "due_date"}
    due_date = due_date.strip()
    if due_date:
        try:
            due_date = date.fromisoformat(due_date).isoformat()
        except ValueError:
            return {
                "ok": False,
                "error": "INVALID_ARGUMENTS",
                "detail": "due_date is not an ISO date",
            }

    rfq_id = "RFQ-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    record = {
        "rfq_id": rfq_id,
        "description": description,
        "quantity": quantity,
        "currency": currency,
        "vendor_ids": list(cleaned),
        "due_date": due_date,
        "status": RFQ_STATUS_OPEN,
        "created_at": now,
        "created_by": "operator",
        "correlation_id": correlation_id,
        "updated_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _RFQS[rfq_id] = record

    payload = {
        "rfq_id": rfq_id,
        "description": description,
        "quantity": quantity,
        "currency": currency,
        "vendor_ids": list(cleaned),
        "due_date": due_date,
    }

    try:
        _commit(
            mutation,
            "RFQ_CREATED",
            payload,
            correlation_id,
            check=lambda: _check_create_rfq(cleaned),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "rfq": dict(record)}


def submit_quote(
    rfq_id: str,
    vendor_id: str,
    unit_price: Any,
    currency: str,
    lead_time_days: Any,
    valid_until: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Submit a vendor quote against an OPEN RFQ. App-layer only.

    The guards (RFQ OPEN, vendor on the RFQ, currency match, not expired, one
    live quote per vendor) live in ``_check_submit_quote`` and run under
    ``_LOCK`` before the ``QUOTE_SUBMITTED`` audit link is written.
    """
    if not isinstance(rfq_id, str) or not rfq_id.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "rfq_id"}
    rfq_id = rfq_id.strip()
    if not isinstance(vendor_id, str) or not vendor_id.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "vendor_id"}
    vendor_id = vendor_id.strip()
    if not isinstance(unit_price, (int, float)) or isinstance(unit_price, bool):
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "unit_price",
        }
    unit_price = float(unit_price)
    if not math.isfinite(unit_price) or unit_price <= 0:
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "unit_price",
        }
    if not isinstance(currency, str) or not currency.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "currency"}
    currency = currency.strip()
    if not isinstance(lead_time_days, (int, float)) or isinstance(
        lead_time_days, bool
    ):
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "lead_time_days",
        }
    if not math.isfinite(float(lead_time_days)) or lead_time_days < 0:
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "lead_time_days",
        }
    if float(lead_time_days) != int(lead_time_days):
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "lead_time_days",
        }
    lead_time_days = int(lead_time_days)
    if not isinstance(valid_until, str) or not valid_until.strip():
        return {
            "ok": False,
            "error": "MALFORMED_PROPOSAL",
            "detail": "valid_until",
        }
    valid_until = valid_until.strip()
    try:
        valid_until = date.fromisoformat(valid_until).isoformat()
    except ValueError:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "valid_until is not an ISO date",
        }

    quote_id = "QTE-" + uuid.uuid4().hex[:8].upper()
    now = _now()
    record = {
        "quote_id": quote_id,
        "rfq_id": rfq_id,
        "vendor_id": vendor_id,
        "unit_price": unit_price,
        "currency": currency,
        "lead_time_days": lead_time_days,
        "valid_until": valid_until,
        "status": QUOTE_STATUS_SUBMITTED,
        "submitted_at": now,
        "correlation_id": correlation_id,
        "updated_at": now,
    }

    def mutation() -> None:
        with _LOCK:
            _QUOTES[quote_id] = record

    payload = {
        "quote_id": quote_id,
        "rfq_id": rfq_id,
        "vendor_id": vendor_id,
        "unit_price": unit_price,
        "currency": currency,
        "lead_time_days": lead_time_days,
        "valid_until": valid_until,
    }

    try:
        _commit(
            mutation,
            "QUOTE_SUBMITTED",
            payload,
            correlation_id,
            check=lambda: _check_submit_quote(
                rfq_id, vendor_id, currency, valid_until
            ),
        )
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "quote": dict(record)}


def close_rfq(rfq_id: str, correlation_id: str = "") -> dict[str, Any]:
    """Close an OPEN RFQ so no further quotes are accepted. App-layer only."""
    return _apply_rfq_status(
        rfq_id, RFQ_STATUS_CLOSED, "RFQ_CLOSED", correlation_id
    )


def cancel_rfq(rfq_id: str, correlation_id: str = "") -> dict[str, Any]:
    """Cancel an OPEN RFQ. App-layer only."""
    return _apply_rfq_status(
        rfq_id, RFQ_STATUS_CANCELLED, "RFQ_CANCELLED", correlation_id
    )


def get_rfq(rfq_id: str) -> dict[str, Any]:
    """Return an RFQ record, or ``RFQ_NOT_FOUND``. App-layer only."""
    if not isinstance(rfq_id, str) or not rfq_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "rfq_id must be a non-empty string",
        }
    with _LOCK:
        rfq = _RFQS.get(rfq_id)
        if rfq is None:
            return {"ok": False, "error": RFQ_NOT_FOUND, "detail": rfq_id}
        return {"ok": True, "rfq": dict(rfq)}


def get_quote(quote_id: str) -> dict[str, Any]:
    """Return a quote record, or ``QUOTE_NOT_FOUND``. App-layer only."""
    if not isinstance(quote_id, str) or not quote_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "quote_id must be a non-empty string",
        }
    with _LOCK:
        quote = _QUOTES.get(quote_id)
        if quote is None:
            return {"ok": False, "error": QUOTE_NOT_FOUND, "detail": quote_id}
        return {"ok": True, "quote": dict(quote)}


def list_quotes(rfq_id: str) -> dict[str, Any]:
    """Return every quote on an RFQ, ordered by ``quote_id``. App-layer only.

    Reports a missing RFQ in-band rather than returning an empty list, so a
    typo in the ID is distinguishable from an RFQ with no quotes yet.
    """
    if not isinstance(rfq_id, str) or not rfq_id:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "rfq_id must be a non-empty string",
        }
    with _LOCK:
        if rfq_id not in _RFQS:
            return {"ok": False, "error": RFQ_NOT_FOUND, "detail": rfq_id}
        quotes = [
            dict(quote)
            for quote in _QUOTES.values()
            if quote.get("rfq_id") == rfq_id
        ]
    quotes.sort(key=lambda q: q.get("quote_id") or "")
    return {"ok": True, "rfq_id": rfq_id, "quotes": quotes}


def award_quote(
    rfq_id: str, quote_id: str, correlation_id: str = ""
) -> dict[str, Any]:
    """Award an RFQ to one SUBMITTED quote and raise the resulting PO.

    App-layer only. The PO is created through the existing ``create_po`` path
    so it lands in ``_POS`` and inherits the ``PO_CREATED`` audit entry; the
    RFQ/quote status change is then committed as ``QUOTE_AWARDED``. The PO is
    created first on purpose: if the second commit fails, the PO is left
    behind (retryable) rather than an awarded quote with no PO.

    The RFQ may be OPEN or CLOSED. Closing an RFQ stops it accepting new
    quotes, but the quotes already on it remain awardable - which is how
    procurement actually works - so a CLOSED RFQ is a legal award source. The
    award stamps ``rfq_id`` onto the PO so the linkage is visible from the PO
    side as well as the RFQ side.

    ``expected_delivery`` on the PO is derived from the quote's
    ``lead_time_days``; the PO amount is ``quantity * unit_price``.
    """
    if not isinstance(rfq_id, str) or not rfq_id.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "rfq_id"}
    rfq_id = rfq_id.strip()
    if not isinstance(quote_id, str) or not quote_id.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "quote_id"}
    quote_id = quote_id.strip()

    def check() -> None:
        with _LOCK:
            rfq = _RFQS.get(rfq_id)
            if rfq is None:
                raise ToolError(RFQ_NOT_FOUND, {"rfq_id": rfq_id})
            if rfq.get("status") not in (RFQ_STATUS_OPEN, RFQ_STATUS_CLOSED):
                raise ToolError(RFQ_NOT_OPEN, {"status": rfq.get("status")})
            quote = _QUOTES.get(quote_id)
            if quote is None or quote.get("rfq_id") != rfq_id:
                raise ToolError(
                    QUOTE_NOT_FOUND, {"rfq_id": rfq_id, "quote_id": quote_id}
                )
            if quote.get("status") != QUOTE_STATUS_SUBMITTED:
                raise ToolError(
                    QUOTE_NOT_SUBMITTED, {"status": quote.get("status")}
                )

    # Pre-validate so a bad award never creates a PO.
    try:
        check()
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    with _LOCK:
        rfq = _RFQS[rfq_id]
        quote = _QUOTES[quote_id]
        quantity = _as_float(rfq.get("quantity"))
        unit_price = _as_float(quote.get("unit_price"))
        vendor_id = quote.get("vendor_id")
        currency = quote.get("currency") or rfq.get("currency")
        lead_time_days = int(_as_float(quote.get("lead_time_days")))

    amount = round(quantity * unit_price, 2)
    expected_delivery = ""
    if lead_time_days:
        expected_delivery = (
            date.fromisoformat(_utc_today()) + timedelta(days=lead_time_days)
        ).isoformat()

    po_result = create_po(
        vendor_id=vendor_id,
        amount=amount,
        currency=currency,
        expected_delivery=expected_delivery,
        correlation_id=correlation_id,
    )
    if not po_result.get("ok"):
        return po_result
    po_id = po_result["po"]["po_id"]

    def mutation() -> None:
        with _LOCK:
            rfq = _RFQS[rfq_id]
            quote = _QUOTES[quote_id]
            stamp = _now()
            quote["status"] = QUOTE_STATUS_AWARDED
            quote["awarded_at"] = stamp
            quote["updated_at"] = stamp
            rfq["status"] = RFQ_STATUS_AWARDED
            rfq["awarded_quote_id"] = quote_id
            rfq["po_id"] = po_id
            rfq["updated_at"] = stamp
            # Stamp the linkage on the PO too, so the award is visible from
            # the PO side. ``create_po`` does not know about RFQs, so this is
            # the only place the back-reference is written.
            _POS[po_id]["rfq_id"] = rfq_id
            for other_id, other in _QUOTES.items():
                if (
                    other_id != quote_id
                    and other.get("rfq_id") == rfq_id
                    and other.get("status") == QUOTE_STATUS_SUBMITTED
                ):
                    other["status"] = QUOTE_STATUS_REJECTED
                    other["updated_at"] = stamp

    payload = {
        "rfq_id": rfq_id,
        "quote_id": quote_id,
        "po_id": po_id,
        "vendor_id": vendor_id,
        "amount": amount,
        "currency": currency,
    }

    try:
        _commit(mutation, "QUOTE_AWARDED", payload, correlation_id, check=check)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    with _LOCK:
        return {
            "ok": True,
            "rfq": dict(_RFQS[rfq_id]),
            "quote": dict(_QUOTES[quote_id]),
            "po": dict(_POS[po_id]),
        }
