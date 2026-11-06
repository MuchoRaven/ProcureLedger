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

Listing tools (``list_pos`` / ``list_requisitions``) deliberately live in
``app.py``, not here: they need the whole store, which is exactly the widening
``_snapshot_for`` refuses to do. Keeping them out of this module means they can
never drift into ``decisions.PROPOSAL_KINDS`` and become model-reachable.

``_snapshot_for`` is the only view of global state the sandbox is allowed to
see, so it is deliberately narrow: the requested record plus its immediate
dependencies, never the whole store.

The ``_apply_*`` helpers are the control-plane side of the proposal contract
defined in ``decisions.py``. They are the only place non-determinism (IDs,
timestamps) is allowed, and they re-validate under ``_LOCK`` before mutating
so a proposal that raced past ``_redeci`` cannot corrupt state.

Audit ordering
--------------
``_commit`` writes the audit link *before* running the mutation, so a failed
audit write leaves business state untouched. The mutation's fallible
validation is passed separately as ``check`` and runs first, so an illegal
transition is rejected before an audit entry is written for it.

``_persist_audit`` is re-exported from ``tool_modules._state`` so the test
suite can monkeypatch ``tools._persist_audit`` to simulate a disk failure;
``_state._append_audit`` resolves that attribute at call time.

Phase 0 plumbing
----------------
``_RFQS``, ``_QUOTES`` and ``_STOCK`` are declared in this module rather than
in ``tool_modules._state`` because the Phase 0 change is scoped to
``tools.py``. ``_state._reset_state`` therefore does not clear them;
``_reset_phase0_state`` does. Moving them into ``_state`` (and teaching
``_reset_state`` about them) is the follow-up that removes that caveat.

The error codes the new tools will raise are declared as constants below, so
the apply helpers in later phases and ``app._TOOL_ERROR_STATUS`` cannot drift
on a typo. Nothing raises them yet: Phase 0 is plumbing only.
"""

from __future__ import annotations

import math
import uuid
from datetime import date
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
# Phase 0 state: RFQs, quotes, stock
# ---------------------------------------------------------------------------
#
# These belong beside ``_POS``/``_REQUISITIONS`` in ``tool_modules._state``, but
# this change is scoped to ``tools.py``, so they are declared here. The caveat
# is that ``_state._reset_state`` does not clear them; a test that resets state
# must also call ``_reset_phase0_state`` below.
#
# They are guarded by the same ``_LOCK`` as the rest of the store, so a
# snapshot can never observe a half-applied mutation.

_RFQS: Dict[str, Dict[str, Any]] = {}
_QUOTES: Dict[str, Dict[str, Any]] = {}
_STOCK: Dict[str, Dict[str, Any]] = {}


def _reset_phase0_state() -> None:
    """Clear the Phase 0 dicts. Companion to ``_state._reset_state``.

    Exists only because the dicts live here rather than in ``_state``; once
    they move, this function goes away and ``_reset_state`` covers them.
    """
    with _LOCK:
        _RFQS.clear()
        _QUOTES.clear()
        _STOCK.clear()


# ---------------------------------------------------------------------------
# Phase 0 error codes
# ---------------------------------------------------------------------------
#
# Every one is raised as ``ToolError(code, detail)``. They are constants rather
# than bare strings so the apply helpers and ``app._TOOL_ERROR_STATUS`` cannot
# disagree on a typo. ``PHASE0_ERROR_CODES`` is the set a test can assert is
# fully mapped in ``app``.

RFQ_NOT_FOUND = "RFQ_NOT_FOUND"
RFQ_NOT_OPEN = "RFQ_NOT_OPEN"
RFQ_EXISTS = "RFQ_EXISTS"
RFQ_HAS_NO_QUOTES = "RFQ_HAS_NO_QUOTES"
QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
QUOTE_EXISTS = "QUOTE_EXISTS"
QUOTE_VENDOR_MISMATCH = "QUOTE_VENDOR_MISMATCH"
QUOTE_CURRENCY_MISMATCH = "QUOTE_CURRENCY_MISMATCH"
QUOTE_EXPIRED = "QUOTE_EXPIRED"
QUOTE_NOT_SUBMITTED = "QUOTE_NOT_SUBMITTED"
QUOTE_ALREADY_AWARDED = "QUOTE_ALREADY_AWARDED"
STOCK_ITEM_NOT_FOUND = "STOCK_ITEM_NOT_FOUND"
REQUISITION_ALREADY_CONVERTED = "REQUISITION_ALREADY_CONVERTED"

PHASE0_ERROR_CODES = frozenset(
    {
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
    }
)


def _utc_today() -> str:
    """Return today's UTC date as an ISO-8601 string."""
    return _now()[:10]


def _snapshot_for(
    tool: str,
    *,
    invoice_id: Optional[str] = None,
    vendor_id: Optional[str] = None,
    po_id: Optional[str] = None,
    requisition_id: Optional[str] = None,
    receipt_id: Optional[str] = None,
    approval_id: Optional[str] = None,
    rfq_id: Optional[str] = None,
    quote_id: Optional[str] = None,
    item_id: Optional[str] = None,
    as_of: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return a minimal, tool-scoped view of state for the sandbox.

    Only the record named by one of the ID keyword arguments is included,
    together with the dependencies the deciders in ``decisions`` actually
    read:

    * ``invoice_id``     -> invoice, its PO, its vendor, its receipts,
                            its approvals
    * ``po_id``          -> PO, its vendor, its receipts
    * ``vendor_id``      -> vendor
    * ``requisition_id`` -> requisition, its vendor, its PO, its RFQ
    * ``receipt_id``     -> receipt, its PO, that PO's vendor
    * ``rfq_id``         -> RFQ, its quotes, their vendors, its requisition
    * ``quote_id``       -> quote, its RFQ, its vendor
    * ``item_id``        -> stock item, its vendor

    ``as_of`` is always present: ``decisions._decide_get_invoice_aging`` falls
    back to it when the caller omits the argument. An explicit, valid ISO
    ``as_of`` is echoed back so the snapshot is a pure function of the
    arguments; otherwise today's UTC date is used.

    With no ID arguments the result is empty collections, never the full
    store. Unknown keyword arguments are ignored so callers can pass a whole
    ``arguments`` dict without pre-filtering.

    ``tool`` is accepted so call sites read symmetrically with
    ``decisions.decide``; the closure below is already minimal for every
    current decider.
    """
    vendors: Dict[str, Any] = {}
    pos: Dict[str, Any] = {}
    receipts: Dict[str, Any] = {}
    invoices: Dict[str, Any] = {}
    approvals: Dict[str, Any] = {}
    requisitions: Dict[str, Any] = {}
    rfqs: Dict[str, Any] = {}
    quotes: Dict[str, Any] = {}
    stock: Dict[str, Any] = {}

    with _LOCK:

        def add_vendor(vid: Optional[str]) -> None:
            if vid and vid in _VENDORS and vid not in vendors:
                vendors[vid] = dict(_VENDORS[vid])

        def add_po(pid: Optional[str]) -> None:
            if pid and pid in _POS and pid not in pos:
                pos[pid] = dict(_POS[pid])
                add_vendor(_POS[pid].get("vendor_id"))
                # Pull in the PO's receipts so a receipt-recording decider can
                # see what has already been received against this PO. Safe
                # against recursion: ``pos[pid]`` is set above, so the nested
                # ``add_po(pid)`` from ``add_receipt`` returns immediately.
                for rid, rec in _RECEIPTS.items():
                    if rec.get("po_id") == pid:
                        add_receipt(rid)

        def add_receipt(rid: Optional[str]) -> None:
            if rid and rid in _RECEIPTS and rid not in receipts:
                receipts[rid] = dict(_RECEIPTS[rid])
                add_po(_RECEIPTS[rid].get("po_id"))

        def add_approval(aid: Optional[str]) -> None:
            if aid and aid in _APPROVALS and aid not in approvals:
                approvals[aid] = dict(_APPROVALS[aid])

        def add_invoice(iid: Optional[str]) -> None:
            if not iid or iid not in _INVOICES or iid in invoices:
                return
            inv = dict(_INVOICES[iid])
            invoices[iid] = inv
            add_po(inv.get("po_id"))
            add_vendor(inv.get("vendor_id"))
            linked = inv.get("receipt_ids")
            if isinstance(linked, (list, tuple)):
                for rid in linked:
                    add_receipt(rid)
            add_receipt(inv.get("receipt_id"))
            # ``_decide_request_payout_approval`` scans approvals for this
            # invoice to reject duplicates, so they must be in the snapshot.
            for aid, appr in _APPROVALS.items():
                if appr.get("invoice_id") == iid:
                    add_approval(aid)

        def add_requisition(qid: Optional[str]) -> None:
            if not qid or qid not in _REQUISITIONS or qid in requisitions:
                return
            req = dict(_REQUISITIONS[qid])
            requisitions[qid] = req
            add_vendor(req.get("vendor_id"))
            add_po(req.get("po_id"))
            # A requisition that has been sourced carries the RFQ it was
            # raised from; pull it in so a convert decider can see the
            # awarded quote. Safe against recursion: ``requisitions[qid]`` is
            # set above, so the nested ``add_requisition`` from ``add_rfq``
            # returns immediately.
            add_rfq(req.get("rfq_id"))

        def add_rfq(rid: Optional[str]) -> None:
            if not rid or rid not in _RFQS or rid in rfqs:
                return
            rfq = dict(_RFQS[rid])
            rfqs[rid] = rfq
            add_vendor(rfq.get("vendor_id"))
            add_requisition(rfq.get("requisition_id"))
            # The awarded quote is the one a convert decider acts on, so it
            # must be present even when the caller only named the RFQ.
            add_quote(rfq.get("awarded_quote_id"))
            # Every quote on the RFQ, so an award decider can compare them.
            for qid, quote in _QUOTES.items():
                if quote.get("rfq_id") == rid:
                    add_quote(qid)

        def add_quote(qid: Optional[str]) -> None:
            if not qid or qid not in _QUOTES or qid in quotes:
                return
            quote = dict(_QUOTES[qid])
            quotes[qid] = quote
            add_vendor(quote.get("vendor_id"))
            add_rfq(quote.get("rfq_id"))

        def add_stock(iid: Optional[str]) -> None:
            if not iid or iid not in _STOCK or iid in stock:
                return
            item = dict(_STOCK[iid])
            stock[iid] = item
            add_vendor(item.get("vendor_id"))

        if invoice_id:
            add_invoice(invoice_id)
        if po_id:
            add_po(po_id)
        if vendor_id:
            add_vendor(vendor_id)
        if requisition_id:
            add_requisition(requisition_id)
        if receipt_id:
            add_receipt(receipt_id)
        if approval_id:
            add_approval(approval_id)
        if rfq_id:
            add_rfq(rfq_id)
        if quote_id:
            add_quote(quote_id)
        if item_id:
            add_stock(item_id)

    reference = _utc_today()
    if isinstance(as_of, str) and as_of.strip():
        try:
            reference = date.fromisoformat(as_of.strip()).isoformat()
        except ValueError:
            # ``decisions`` will reject the bad argument itself; keep the
            # snapshot self-consistent rather than raising here.
            pass

    return {
        "as_of": reference,
        "vendors": vendors,
        "pos": pos,
        "receipts": receipts,
        "invoices": invoices,
        "approvals": approvals,
        "requisitions": requisitions,
        "rfqs": rfqs,
        "quotes": quotes,
        "stock": stock,
    }


# ---------------------------------------------------------------------------
# Apply helpers (control-plane side of the proposal contract)
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float:
    """Coerce a numeric field to float, treating anything else as 0.0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _receipts_for_invoice(invoice: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the receipt records linked to ``invoice``.

    Invoices may carry either a single ``receipt_id`` or a ``receipt_ids``
    list; both are honoured and de-duplicated. Caller must hold ``_LOCK``.
    """
    receipts: List[Dict[str, Any]] = []
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
    invoice: Dict[str, Any],
    po: Dict[str, Any],
    receipts: List[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
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
) -> Tuple[bool, Dict[str, Any]]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


def lookup_vendor(vendor_id: str) -> Dict[str, Any]:
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


def get_invoice(invoice_id: str) -> Dict[str, Any]:
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


def get_invoice_aging(invoice_id: str, as_of: str = "") -> Dict[str, Any]:
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


def get_po(po_id: str) -> Dict[str, Any]:
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


def get_requisition(requisition_id: str) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
    """Record a goods receipt against an OPEN PO. App-layer only.

    The over-receipt control lives in ``_check_record_receipt`` and runs under
    ``_LOCK`` before the audit link is written.
    """
    return _apply_record_receipt(
        {"kind": "record_receipt", "po_id": po_id, "amount": amount},
        correlation_id,
    )


def close_po(po_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Close an OPEN PO. App-layer only."""
    return _apply_po_status(
        {"kind": "po_status", "po_id": po_id, "to_status": PO_STATUS_CLOSED},
        correlation_id,
    )


def cancel_po(po_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Cancel an OPEN PO. App-layer only."""
    return _apply_po_status(
        {"kind": "po_status", "po_id": po_id, "to_status": PO_STATUS_CANCELLED},
        correlation_id,
    )


def log_audit_event(
    event_type: str,
    payload: Dict[str, Any],
    correlation_id: str = "",
    source: str = "agent",
) -> Dict[str, Any]:
    """Append a note to the audit chain.

    ``source="tool"`` is the control plane's own bookkeeping and may use the
    reserved event types; ``source="agent"`` (the default) may not. Raises
    ``ToolError`` when the chain itself is unwritable - the caller decides
    whether that is fatal (``agent._audit`` treats it as such).
    """
    if not isinstance(event_type, str) or not event_type.strip():
        return {
            "ok": False,
            "error": "INVALID_EVENT_TYPE",
            "detail": "event_type must be a non-empty string",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "INVALID_PAYLOAD",
            "detail": "payload must be an object",
        }

    event_type = event_type.strip()
    if source != "tool" and event_type in _RESERVED_EVENT_TYPES:
        return {
            "ok": False,
            "error": "RESERVED_EVENT_TYPE",
            "detail": event_type,
        }

    entry = _append_audit(event_type, payload, correlation_id, source=source)
    return {
        "ok": True,
        "audit": {
            "seq": entry.get("seq"),
            "event_type": entry.get("event_type"),
            "hash": entry.get("hash"),
        },
    }


def ingest_invoice(
    invoice_id: str,
    vendor_id: str,
    po_id: str,
    amount: Any,
    currency: str = "USD",
    correlation_id: str = "",
) -> Dict[str, Any]:
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


def get_approval(approval_id: str) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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
) -> Dict[str, Any]:
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


def verify_audit_chain(from_file: bool = True) -> Dict[str, Any]:
    """Re-verify the hash chain.

    Reports an unreadable log in-band with ``ok: True`` and the raw error text
    in ``detail``; ``app.py`` redacts that field on 5xx-style failures. A
    broken link is reported with ``valid: False`` and the index it broke at.
    """
    try:
        entries = _load_audit_from_file() if from_file else _audit_entries()
    except ToolError as exc:
        return {
            "ok": True,
            "valid": False,
            "entries": 0,
            "detail": exc.detail,
        }

    prev = GENESIS_HASH
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "MALFORMED_ENTRY",
            }
        if entry.get("prev_hash") != prev:
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "PREV_HASH_MISMATCH",
            }
        if entry.get("hash") != _compute_hash(entry):
            return {
                "ok": True,
                "valid": False,
                "entries": len(entries),
                "broken_at": index,
                "reason": "HASH_MISMATCH",
            }
        prev = entry.get("hash", prev)

    return {"ok": True, "valid": True, "entries": len(entries)}
