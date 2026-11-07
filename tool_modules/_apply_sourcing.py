"""
tool_modules/_apply_sourcing.py
===============================
The sourcing half of the control plane: the validation helpers for RFQs and
quotes, and the RFQ status change.

These helpers used to live in ``tools.py``. They are moved here so the sourcing
apply path can be read without the whole tool layer. The bodies are unchanged.

Every helper follows the same contract as the rest of the control plane:
validate under ``_LOCK``, write the audit link before the mutation, and
generate IDs/timestamps only in the apply step. The ``_check_*`` helpers are
the fallible validation that ``_commit`` runs first, so a proposal that raced
past ``_redeci`` is rejected before an audit entry is written for it.

This module imports only ``_state`` (the store, its lock, and the status
values), ``_codes`` (the Phase 0/1 error codes and status values) and ``_util``
(``_utc_today``). It deliberately does not import ``tools``, ``decisions`` or
``sandbox``, so it cannot participate in an import cycle.
"""

from __future__ import annotations

from typing import Any

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _RFQS,
    _QUOTES,
    ToolError,
    _now,
    _commit,
)
from tool_modules._codes import (
    RFQ_NOT_FOUND,
    RFQ_NOT_OPEN,
    QUOTE_VENDOR_MISMATCH,
    QUOTE_CURRENCY_MISMATCH,
    QUOTE_EXPIRED,
    QUOTE_EXISTS,
    RFQ_STATUS_OPEN,
    RFQ_STATUS_CLOSED,
    RFQ_STATUS_CANCELLED,
    _LIVE_QUOTE_STATUSES,
)
from tool_modules._util import _utc_today


def _check_create_rfq(vendor_ids: list[str]) -> None:
    """Validate an RFQ-creation request without changing anything.

    Every named vendor must exist, be ACTIVE and not be HIGH risk. Run under
    ``_LOCK`` so a vendor that went inactive after the request was formed is
    rejected before an audit link is written for it.
    """
    with _LOCK:
        for vid in vendor_ids:
            vendor = _VENDORS.get(vid)
            if vendor is None:
                raise ToolError("VENDOR_NOT_FOUND", {"vendor_id": vid})
            if vendor.get("status") != "ACTIVE":
                raise ToolError(
                    "VENDOR_INACTIVE",
                    {"vendor_id": vid, "status": vendor.get("status")},
                )
            if vendor.get("risk_tier") == "HIGH":
                raise ToolError("VENDOR_HIGH_RISK", {"vendor_id": vid})


def _check_submit_quote(
    rfq_id: str, vendor_id: str, currency: str, valid_until: str
) -> None:
    """Validate a quote submission without changing anything.

    The guards are the ones that can race: the RFQ must still be OPEN, the
    vendor must still be on the RFQ, the currency must still match, the quote
    must not already be expired, and the vendor must not already have a live
    quote on this RFQ.
    """
    with _LOCK:
        rfq = _RFQS.get(rfq_id)
        if rfq is None:
            raise ToolError(RFQ_NOT_FOUND, {"rfq_id": rfq_id})
        if rfq.get("status") != RFQ_STATUS_OPEN:
            raise ToolError(RFQ_NOT_OPEN, {"status": rfq.get("status")})
        if vendor_id not in (rfq.get("vendor_ids") or []):
            raise ToolError(
                QUOTE_VENDOR_MISMATCH,
                {"rfq_id": rfq_id, "vendor_id": vendor_id},
            )
        if currency != rfq.get("currency"):
            raise ToolError(
                QUOTE_CURRENCY_MISMATCH,
                {"currency": currency, "rfq_currency": rfq.get("currency")},
            )
        today = _utc_today()
        if valid_until and valid_until < today:
            raise ToolError(
                QUOTE_EXPIRED, {"valid_until": valid_until, "as_of": today}
            )
        for quote in _QUOTES.values():
            if (
                quote.get("rfq_id") == rfq_id
                and quote.get("vendor_id") == vendor_id
                and quote.get("status") in _LIVE_QUOTE_STATUSES
            ):
                raise ToolError(
                    QUOTE_EXISTS, {"quote_id": quote.get("quote_id")}
                )


def _apply_rfq_status(
    rfq_id: str, to_status: str, event_type: str, correlation_id: str
) -> dict[str, Any]:
    """Close or cancel an OPEN RFQ.

    Only ``CLOSED`` and ``CANCELLED`` are accepted. The check re-reads the RFQ
    under ``_LOCK`` so a request that raced with an award cannot close an RFQ
    that has already moved on.
    """
    if not isinstance(rfq_id, str) or not rfq_id.strip():
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "rfq_id"}
    rfq_id = rfq_id.strip()
    if to_status not in (RFQ_STATUS_CLOSED, RFQ_STATUS_CANCELLED):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "to_status"}

    def check() -> None:
        with _LOCK:
            rfq = _RFQS.get(rfq_id)
            if rfq is None:
                raise ToolError(RFQ_NOT_FOUND, {"rfq_id": rfq_id})
            if rfq.get("status") != RFQ_STATUS_OPEN:
                raise ToolError(RFQ_NOT_OPEN, {"status": rfq.get("status")})

    def mutation() -> None:
        with _LOCK:
            rfq = _RFQS[rfq_id]
            rfq["status"] = to_status
            rfq["updated_at"] = _now()

    payload = {"rfq_id": rfq_id, "from": RFQ_STATUS_OPEN, "to": to_status}

    try:
        _commit(mutation, event_type, payload, correlation_id, check=check)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    with _LOCK:
        return {"ok": True, "rfq": dict(_RFQS[rfq_id])}
