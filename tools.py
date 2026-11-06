"""
tools.py
========
Domain tools. The snapshot builder here is the only thing the sandbox is
allowed to see of global state, so it is deliberately narrow: it returns the
requested record plus its immediate dependencies, never the whole store.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _POS,
    _RECEIPTS,
    _INVOICES,
    _APPROVALS,
    _REQUISITIONS,
    ToolError,
    _now,
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
