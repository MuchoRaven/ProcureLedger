"""
tools.py
========
Domain tools. The snapshot builder here is the only thing the sandbox is
allowed to see of global state, so it is deliberately narrow: it returns the
requested record plus its immediate dependencies, never the whole store.

The ``_apply_*`` helpers are the control-plane side of the proposal contract
defined in ``decisions.py``. They are the only place non-determinism (IDs,
timestamps) is allowed, and they re-validate under ``_LOCK`` before mutating
so a proposal that raced past ``_redeci`` cannot corrupt state.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

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
    _now,
    _append_audit,
    _commit,
    _transition,
    PO_STATUS_OPEN,
    PO_STATUS_CLOSED,
    PO_STATUS_CANCELLED,
    REQ_STATUS_OPEN,
    REQ_STATUS_CONVERTED,
    REQ_STATUS_CANCELLED,
    AMOUNT_TOLERANCE,
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
    as_of: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Return a minimal, tool-scoped view of state for the sandbox.

    Only the record named by one of the ID keyword arguments is included,
    together with the dependencies the deciders in ``decisions`` actually
    read:

    * ``invoice_id``     -> invoice, its PO, its vendor, its receipts,
                            its approvals
    * ``po_id``          -> PO, its vendor
    * ``vendor_id``      -> vendor
    * ``requisition_id`` -> requisition, its vendor, its PO
    * ``receipt_id``     -> receipt, its PO, that PO's vendor

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

    with _LOCK:

        def add_vendor(vid: Optional[str]) -> None:
            if vid and vid in _VENDORS and vid not in vendors:
                vendors[vid] = dict(_VENDORS[vid])

        def add_po(pid: Optional[str]) -> None:
            if pid and pid in _POS and pid not in pos:
                pos[pid] = dict(_POS[pid])
                add_vendor(_POS[pid].get("vendor_id"))

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

    approval_id = "APR-" + uuid.uuid4().hex[:12].upper()
    now = _now()
    record = {
        "approval_id": approval_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "invoice_id": invoice_id,
        "amount": float(amount),
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
    }

    def mutation() -> None:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            raise ToolError("INVOICE_NOT_FOUND", {"invoice_id": invoice_id})
        if invoice.get("status") != "MATCHED":
            raise ToolError(
                "INVOICE_NOT_MATCHED", {"status": invoice.get("status")}
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
        _APPROVALS[approval_id] = record

    payload = {
        "approval_id": approval_id,
        "vendor_id": vendor_id,
        "po_id": po_id,
        "invoice_id": invoice_id,
        "amount": float(amount),
    }

    try:
        _commit(mutation, "PAYOUT_APPROVAL_REQUESTED", payload, correlation_id)
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {"ok": True, "approval": dict(record)}


def _apply_audit_note(
    proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
    """Apply an ``audit_note`` proposal.

    Reserved event types are written by the control plane only, so a model
    that tries to forge one is rejected here rather than in the decider
    (``decisions`` cannot import ``_state`` and so cannot see the reserved
    set). The note is appended with ``source="agent"`` so it is
    distinguishable from control-plane events in the chain.
    """
    event_type = proposal.get("event_type")
    payload = proposal.get("payload")
    if not isinstance(event_type, str) or not event_type:
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "event_type"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "detail": "payload"}
    if event_type in _RESERVED_EVENT_TYPES:
        return {
            "ok": False,
            "error": "RESERVED_EVENT_TYPE",
            "detail": event_type,
        }

    try:
        entry = _append_audit(event_type, payload, correlation_id, source="agent")
    except ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}

    return {
        "ok": True,
        "audit": {
            "seq": entry.get("seq"),
            "event_type": entry.get("event_type"),
            "hash": entry.get("hash"),
        },
    }
