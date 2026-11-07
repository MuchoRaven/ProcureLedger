"""
tools.py
========
Facade over ``tool_modules``. Every tool body lives in a submodule; this
module only re-exports, so ``agent``, ``app``, ``sandbox`` and the tests keep
reaching them through ``tools``. The private names are temporary - drop them
once those callers import from ``tool_modules`` directly.

The Phase 0 caveat lives in ``tool_modules._codes``; the ``_persist_audit``
re-export note lives in ``tool_modules._state``.

The two SKU-scoped vendor-quote reads (``get_vendor_quotes`` and
``compare_vendors``) are re-exported here for the same reason as every other
tool: ``agent._DISPATCH`` is built at import as
``{name: getattr(tools, name) for name in ALLOWED_TOOLS}``, and
``ALLOWED_TOOLS`` is derived from ``decisions.PROPOSAL_KINDS``. Registering
either tool there without a matching re-export here would raise
``AttributeError`` at import and take the whole app down, not just the new
tool. Both are reads, so ``sandbox._apply`` returns their proposal's ``data``
directly and never calls back into this module.
"""

from __future__ import annotations

from tool_modules._state import (
    _LOCK,
    _POS,
    _RECEIPTS,
    _INVOICES,
    _APPROVALS,
    _REQUISITIONS,
    ToolError,
    Principal,
    _load_audit_from_file,
    _audit_entries,
    _persist_audit_entry as _persist_audit,
    _reset_state,
    AMOUNT_TOLERANCE,
)

from tool_modules._codes import PHASE0_ERROR_CODES

from tool_modules._util import _as_float, _utc_today

from tool_modules._snapshot import _snapshot_for

from tool_modules._apply_ap import (
    _receipts_for_invoice,
    _three_way_match,
    _evaluate_three_way_match,
    _check_create_approval,
    _apply_transition,
    _apply_create_approval,
    _apply_audit_note,
)

from tool_modules._apply_procurement import (
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

from tool_modules._apply_sourcing import (
    _check_create_rfq,
    _check_submit_quote,
    _apply_rfq_status,
)

from tool_modules._tools_audit import log_audit_event, verify_audit_chain

from tool_modules._tools_ap import (
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

from tool_modules._tools_procurement import (
    get_po,
    get_requisition,
    create_po,
    record_goods_receipt,
    close_po,
    cancel_po,
)

from tool_modules._tools_sourcing import (
    create_rfq,
    submit_quote,
    close_rfq,
    cancel_rfq,
    get_rfq,
    get_quote,
    list_quotes,
    award_quote,
)

from tool_modules._tools_vendor_compare import (
    get_vendor_quotes,
    compare_vendors,
)

__all__ = [
    # Public tools
    "lookup_vendor", "get_invoice", "get_invoice_aging", "run_three_way_match",
    "request_payout_approval", "ingest_invoice", "get_approval",
    "approve_payout", "_execute_payout",
    "get_po", "get_requisition", "create_po", "record_goods_receipt",
    "close_po", "cancel_po",
    "create_rfq", "submit_quote", "close_rfq", "cancel_rfq", "get_rfq",
    "get_quote", "list_quotes", "award_quote",
    "get_vendor_quotes", "compare_vendors",
    "log_audit_event", "verify_audit_chain",
    # Snapshot
    "_snapshot_for",
    # Apply helpers (sandbox and tests)
    "_receipts_for_invoice", "_three_way_match", "_evaluate_three_way_match",
    "_check_create_approval", "_apply_transition", "_apply_create_approval",
    "_apply_audit_note",
    "_check_create_requisition", "_apply_create_requisition",
    "_check_create_po", "_apply_create_po", "_check_record_receipt",
    "_apply_record_receipt", "_apply_po_status", "_check_requisition_status",
    "_apply_requisition_status",
    "_check_create_rfq", "_check_submit_quote", "_apply_rfq_status",
    # Shared helpers
    "_as_float", "_utc_today",
    # State and types (temporary; drop once callers import from tool_modules)
    "_LOCK", "_POS", "_RECEIPTS", "_INVOICES", "_APPROVALS", "_REQUISITIONS",
    "ToolError", "Principal",
    "_load_audit_from_file", "_audit_entries", "_persist_audit",
    "_reset_state",
    "AMOUNT_TOLERANCE",
    # Codes
    "PHASE0_ERROR_CODES",
]
