"""
tool_modules/_codes.py
======================
Data-only module: the Phase 0/1 and follow-up error codes and status values.

These constants used to live in ``tools.py``. They are moved here so the
control plane (``tools``), the HTTP surface (``app``) and the tests can share
one definition without importing the whole tool layer. Nothing in this module
imports anything but ``__future__``; it is pure data.

The string values are unchanged, so ``tools.RFQ_NOT_FOUND`` and friends keep
resolving to the same strings they always did.

Phase 0 caveat
--------------
The error codes the Phase 0 and Phase 1 tools raise are declared here as
constants and re-exported by ``tools``, so the apply helpers and
``app._TOOL_ERROR_STATUS`` cannot drift on a typo. ``PHASE0_ERROR_CODES`` is
the set a test can assert is fully mapped in ``app``: every member must be a
key in ``_TOOL_ERROR_STATUS``, or a Phase 0 failure surfaces as a 400.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Follow-up error codes
# ---------------------------------------------------------------------------

PO_NOT_LATE = "PO_NOT_LATE"


# ---------------------------------------------------------------------------
# Phase 1 status values
# ---------------------------------------------------------------------------
#
# Constants rather than bare strings so the apply helpers, the read helpers and
# the tests cannot drift on a typo.

RFQ_STATUS_OPEN = "OPEN"
RFQ_STATUS_CLOSED = "CLOSED"
RFQ_STATUS_CANCELLED = "CANCELLED"
RFQ_STATUS_AWARDED = "AWARDED"

QUOTE_STATUS_SUBMITTED = "SUBMITTED"
QUOTE_STATUS_WITHDRAWN = "WITHDRAWN"
QUOTE_STATUS_AWARDED = "AWARDED"
QUOTE_STATUS_REJECTED = "REJECTED"

# A quote is "live" while it can still be awarded; only these statuses block a
# second quote from the same vendor on the same RFQ.
_LIVE_QUOTE_STATUSES = (QUOTE_STATUS_SUBMITTED, QUOTE_STATUS_AWARDED)
