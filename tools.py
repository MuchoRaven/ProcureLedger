"""
tools.py
========
Domain tools and the control-plane side of the proposal contract.

This module is now a facade: every tool body lives in a ``tool_modules``
submodule and is re-exported here, so ``agent``, ``app``, ``sandbox`` and the
tests keep reaching them through ``tools``. The re-exports are deliberate, not
incidental - ``sandbox`` calls ``tools._snapshot_for`` and
``tools._apply_*``, ``app`` reads ``tools._POS`` and ``tools.Principal``, and
the tests monkeypatch ``tools._persist_audit``.

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

The accounts-payable public tools (``lookup_vendor``, ``get_invoice``,
``get_invoice_aging``, ``run_three_way_match``, ``request_payout_approval``,
``ingest_invoice``, ``get_approval``, ``approve_payout``, ``_execute_payout``)
live in ``tool_modules._tools_ap`` and are re-exported below, so ``agent``,
``app`` and the tests keep reaching them through ``tools``.

The procurement public tools (``get_po``, ``get_requisition``, ``create_po``,
``record_goods_receipt``, ``close_po``, ``cancel_po``) live in
``tool_modules._tools_procurement`` and are re-exported below, so ``app`` and
the tests keep reaching them through ``tools``.

The sourcing public tools (``create_rfq``, ``submit_quote``, ``close_rfq``,
``cancel_rfq``, ``get_rfq``, ``get_quote``, ``list_quotes``, ``award_quote``)
live in ``tool_modules._tools_sourcing`` and are re-exported below, so ``app``
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
the apply step. The tools live in ``tool_modules._tools_sourcing``; the
validation helpers (``_check_create_rfq``, ``_check_submit_quote``) and the RFQ
status change (``_apply_rfq_status``) live in
``tool_modules._apply_sourcing``.

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
# ``tool_modules._apply_procurement``. They are re-exported here so
# ``sandbox`` and the tests keep reaching them through ``tools``.

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
# in ``tool_modules._apply_sourcing``. They are re-exported here so
# ``sandbox`` and the tests keep reaching them through ``tools``.

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
# Accounts-payable public tools
# ---------------------------------------------------------------------------
#
# ``lookup_vendor``, ``get_invoice``, ``get_invoice_aging``,
# ``run_three_way_match``, ``request_payout_approval``, ``ingest_invoice``,
# ``get_approval``, ``approve_payout`` and ``_execute_payout`` live in
# ``tool_modules._tools_ap``. They are re-exported here so ``agent``, ``app``
# and the tests keep reaching them through ``tools``.

from tool_modules._tools_ap import (  # noqa: F401 - re-exported
    lookup_vendor,
    get_invoice,
    get_invoice_aging,
    run_three_way_match,
    request_payout_approval,
    ingest_invoice,
    get_approval,
    approve_payout,
    _execute_payout,
)

# ---------------------------------------------------------------------------
# Procurement public tools
# ---------------------------------------------------------------------------
#
# ``get_po``, ``get_requisition``, ``create_po``, ``record_goods_receipt``,
# ``close_po`` and ``cancel_po`` live in ``tool_modules._tools_procurement``.
# They are re-exported here so ``app`` and the tests keep reaching them through
# ``tools``.

from tool_modules._tools_procurement import (  # noqa: F401 - re-exported
    get_po,
    get_requisition,
    create_po,
    record_goods_receipt,
    close_po,
    cancel_po,
)

# ---------------------------------------------------------------------------
# Sourcing public tools
# ---------------------------------------------------------------------------
#
# ``create_rfq``, ``submit_quote``, ``close_rfq``, ``cancel_rfq``, ``get_rfq``,
# ``get_quote``, ``list_quotes`` and ``award_quote`` live in
# ``tool_modules._tools_sourcing``. They are re-exported here so ``app`` and
# the tests keep reaching them through ``tools``.

from tool_modules._tools_sourcing import (  # noqa: F401 - re-exported
    create_rfq,
    submit_quote,
    close_rfq,
    cancel_rfq,
    get_rfq,
    get_quote,
    list_quotes,
    award_quote,
)
