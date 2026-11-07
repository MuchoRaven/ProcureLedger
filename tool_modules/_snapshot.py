"""
tool_modules/_snapshot.py
=========================
The sandbox's only view of global state.

``_snapshot_for`` used to live in ``tools.py``. It is moved here so the
snapshot logic can be read and tested without importing the whole tool layer.
The body is unchanged, including the nested ``add_*`` closures: they are the
recursion-safe way the snapshot pulls in a record's dependencies, and
flattening them would lose that.

This module imports only ``_state`` (the store and its lock) and ``_util``
(``_utc_today``). It deliberately does not import ``tools``, ``decisions`` or
``sandbox``, so it cannot participate in an import cycle.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

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
)
from tool_modules._util import _utc_today


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
) -> dict[str, Any]:
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
    vendors: dict[str, Any] = {}
    pos: dict[str, Any] = {}
    receipts: dict[str, Any] = {}
    invoices: dict[str, Any] = {}
    approvals: dict[str, Any] = {}
    requisitions: dict[str, Any] = {}
    rfqs: dict[str, Any] = {}
    quotes: dict[str, Any] = {}
    stock: dict[str, Any] = {}

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
