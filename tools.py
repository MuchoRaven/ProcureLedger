"""
tools.py
========
Domain tools and the control-plane side of the proposal contract.

Public tools (the model reaches these only through ``sandbox.dispatch``):

* ``lookup_vendor(vendor_id)``
* ``get_invoice(invoice_id)``
* ``get_invoice_aging(invoice_id, as_of="")``
* ``run_three_way_match(invoice_id, po_id, correlation_id="")``
* ``request_payout_approval(vendor_id, amount, po_id, invoice_id,
  correlation_id="")``
* ``log_audit_event(event_type, payload, correlation_id="", source="agent")``

App-layer only (never in ``decisions.PROPOSAL_KINDS``, so unreachable by the
model):

* ``ingest_invoice(invoice_id, vendor_id, po_id, amount, currency="USD",
  correlation_id="")``
* ``get_approval(approval_id)``
* ``approve_payout(approval_id, approver, correlation_id="")``
* ``_execute_payout(approval_id, executor, correlation_id="")``
* ``verify_audit_chain(from_file=True)``
* ``create_po(vendor_id, amount, currency="USD", expected_delivery="",
  requisition_id="", correlation_id="")``
* ``record_goods_receipt(po_id, amount, correlation_id="")``
* ``close_po(po_id, correlation_id="")`` / ``cancel_po(po_id,
  correlation_id="")``
* ``get_po(po_id)`` / ``get_requisition(requisition_id)``
* ``create_rfq(description, quantity, currency, vendor_ids, due_date,
  correlation_id)`` / ``submit_quote(rfq_id, vendor_id, unit_price, currency,
  lead_time_days, valid_until, correlation_id)``
* ``close_rfq(rfq_id, correlation_id)`` / ``cancel_rfq(rfq_id,
  correlation_id)`` / ``award_quote(rfq_id, quote_id, correlation_id)``
* ``get_rfq(rfq_id)`` / ``get_quote(quote_id)`` / ``list_quotes(rfq_id)``

Listing tools (``list_pos`` / ``list_requisitions``) deliberately live in
``app.py``, not here: they need the whole store, which is exactly the widening
``_snapshot_for`` refuses to do. Keeping them out of this module means they can
never drift into ``decisions.PROPOSAL_KINDS`` and become model-reachable.

``_snapshot_for`` is the only view of global state the sandbox is allowed to
see, so it is deliberately narrow: the requested record plus its immediate
dependencies, never the whole store. It lives in ``tool_modules._snapshot`` and
is re-exported below so ``sandbox`` can keep calling ``tools._snapshot_for``.

The ``_apply_*`` helpers are the control-plane side of the proposal contract
defined in ``decisions.py``. They are the only place non-determinism (IDs,
timestamps) is allowed, and they re-validate under ``_LOCK`` before mutating
so a proposal that raced past ``_redeci`` cannot corrupt state. The
accounts-payable ones (three-way match, payout approval, audit note) live in
``tool_modules._apply_ap``, the procurement ones (requisitions, POs, goods
receipts, PO/requisition status) live in ``tool_modules._apply_procurement``,
and the sourcing ones (RFQ/quote validation, RFQ status) live in
``tool_modules._apply_sourcing``; all are re-exported below.

The audit-chain tools (``log_audit_event``, ``verify_audit_chain``) live in
``tool_modules._tools_audit`` and are re-exported below, so ``agent``, ``app``
and the tests keep reaching them through ``tools``.

Audit ordering
--------------
``_commit`` writes the audit link *before* running the mutation, so a failed
audit write leaves business state untouched. The mutation's fallible
validation is passed separately as ``check`` and runs first, so an illegal
transition is rejected before an audit entry is written for it.

``_persist_audit`` is re-exported from ``tool_modules._state`` so the test
suite can monkeypatch ``tools._persist_audit`` to simulate a disk failure;
``_state._append_audit`` resolves that attribute at call time. The same
re-export is what keeps ``tools._load_audit_from_file`` and
``tools._audit_entries`` available to the tests now that the functions that
used them have moved out.

The error codes the Phase 0 and Phase 1 tools raise are declared as constants
in ``tool_modules._codes`` and re-exported below, so the apply helpers and
``app._TOOL_ERROR_STATUS`` cannot drift on a typo.

Phase 1 (RFQs and quotes)
-------------------------
``create_rfq`` / ``submit_quote`` / ``close_rfq`` / ``cancel_rfq`` /
``award_quote`` and the read helpers ``get_rfq`` / ``get_quote`` /
``list_quotes`` are app-layer only: they are not in
``decisions.PROPOSAL_KINDS``, so the model cannot reach them. They follow the
same contract as the rest of the control plane - validate under ``_LOCK``,
write the audit link before the mutation, and generate IDs/timestamps only in
the apply step. The validation helpers (``_check_create_rfq``,
``_check_submit_quote``) and the RFQ status change (``_apply_rfq_status``) live
in ``tool_modules._apply_sourcing``.

``award_quote`` creates its PO through the existing ``create_po`` path so the
award lands in ``_POS`` and inherits the ``PO_CREATED`` audit entry. The PO is
created first and the RFQ/quote status change is committed second; if the
second commit fails the PO is left behind (retryable) rather than an awarded
quote with no PO.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import decisions

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _RECEIPTS,
    _INVOICES,
    _APPROVALS,
    _REQUISITIONS,
    _RFQS,
    _QUOTES,
    _STOCK,
    _RESERVED_EVENT_TYPES,
    ToolError,
    Principal,
    _now,
    _append_audit,
    _commit,
    _transition,
    _check_transition,
    _load_audit_from_file,
    _audit_entries,
    _compute_hash,
    _persist_audit_entry as _persist_audit,
    _reset_state,
    _reset_phase0_state,  # noqa: F401 - re-exported for tests
    GENESIS_HASH,
    PO_STATUS_OPEN,
    PO_STATUS_CLOSED,
    PO_STATUS_CANCELLED,
    REQ_STATUS_OPEN,
    REQ_STATUS_CONVERTED,
    REQ_STATUS_CANCELLED,
    AMOUNT_TOLERANCE,
)

# ---------------------------------------------------------------------------
# Phase 0 error codes and Phase 1 status values
# ---------------------------------------------------------------------------
#
# The constants themselves live in ``tool_modules._codes`` so the control
# plane, the HTTP surface and the tests share one definition. They are
# re-exported here so ``tools.RFQ_NOT_FOUND`` and ``tools.PHASE0_ERROR_CODES``
# keep resolving exactly as before.

from tool_modules._codes import (  # noqa: F401 - re-exported
    RFQ_NOT_FOUND,
    RFQ_NOT_OPEN,
    RFQ_EXISTS,
    RFQ_HAS_NO_QUOTES,
    QUOTE_NOT_FOUND,
    QUOTE_EXISTS,
    QUOTE_VENDOR_MISMATCH,
    QUOTE_CURRENCY_MISMATCH,
    QUOTE_EXPIRED,
    QUOTE_NOT_SUBMITTED,
    QUOTE_ALREADY_AWARDED,
    STOCK_ITEM_NOT_FOUND,
    REQUISITION_ALREADY_CONVERTED,
    PHASE0_ERROR_CODES,
    RFQ_STATUS_OPEN,
    RFQ_STATUS_CLOSED,
    RFQ_STATUS_CANCELLED,
    RFQ_STATUS_AWARDED,
    QUOTE_STATUS_SUBMITTED,
    QUOTE_STATUS_WITHDRAWN,
    QUOTE_STATUS_AWARDED,
    QUOTE_STATUS_REJECTED,
    _LIVE_QUOTE_STATUSES,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
#
# ``_utc_today`` and ``_as_float`` live in ``tool_modules._util``. They are
# imported here so ``tools._utc_today`` / ``tools._as_float`` keep resolving
# for callers that reach them through this module.

from tool_modules._util import _as_float, _utc_today

# ``_snapshot_for`` lives in ``tool_modules._snapshot``. It is imported here so
# ``tools._snapshot_for`` keeps resolving for ``sandbox``, which calls it
# through this module.

from tool_modules._snapshot import _snapshot_for  # noqa: F401 - re-exported

# ---------------------------------------------------------------------------
# Accounts-payable apply helpers
# ---------------------------------------------------------------------------
#
# ``_receipts_for_invoice``, ``_three_way_match``, ``_evaluate_three_way_match``,
# ``_check_create_approval``, ``_apply_transition``, ``_apply_create_approval``
# and ``_apply_audit_note`` live in ``tool_modules._apply_ap``. They are
# re-exported here so ``sandbox`` and the tests keep reaching them through
# ``tools``.

from tool_modules._apply_ap import (  # noqa: F401 - re-exported
    _receipts_for_invoice,
    _three_way_match,
    _evaluate_three_way_match,
    _check_create_approval,
    _apply_transition,
    _apply_create_approval,
    _apply_audit_note,
)

# ---------------------------------------------------------------------------
# Procurement apply helpers
# ---------------------------------------------------------------------------
#
# ``_check_create_requisition``, ``_apply_create_requisition``,
# ``_check_create_po``, ``_apply_create_po``, ``_check_record_receipt``,
# ``_apply_record_receipt``, ``_apply_po_status``,
# ``_check_requisition_status`` and ``_apply_requisition_status`` live in
# ``tool_modules._apply_procurement``. They are re-exported here so the public
# wrappers below and the tests keep reaching them through ``tools``.

from tool_modules._apply_procurement import (  # noqa: F401 - re-exported
    _check_create_requisition,
    _apply_create_requisition,
    _check_create_po,
    _apply_create_po,
    _check_record_receipt,
    _apply_record_receipt,
    _apply_po_status,
    _check_requisition_status,
    _apply_requisition_status,
)

# ---------------------------------------------------------------------------
# Sourcing apply helpers
# ---------------------------------------------------------------------------
#
# ``_check_create_rfq``, ``_check_submit_quote`` and ``_apply_rfq_status`` live
# in ``tool_modules._apply_sourcing``. They are re-exported here so the public
# wrappers below and the tests keep reaching them through ``tools``.

from tool_modules._apply_sourcing import (  # noqa: F401 - re-exported
    _check_create_rfq,
    _check_submit_quote,
    _apply_rfq_status,
)

# ---------------------------------------------------------------------------
# Audit-chain tools
# ---------------------------------------------------------------------------
#
# ``log_audit_event`` and ``verify_audit_chain`` live in
# ``tool_modules._tools_audit``. They are re-exported here so ``agent``,
# ``app`` and the tests keep reaching them through ``tools``.

from tool_modules._tools_audit import (  # noqa: F401 - re-exported
    log_audit_event,
    verify_audit_chain,
)


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 1: RFQs and quotes (app-layer only)
# ---------------------------------------------------------------------------
#
# None of these are in ``decisions.PROPOSAL_KINDS``, so the model cannot reach
# them. They follow the same contract as the rest of the control plane:
# validate under ``_LOCK``, write the audit link before the mutation, and
# generate IDs/timestamps only in the apply step.


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
