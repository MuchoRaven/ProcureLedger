"""
tool_modules/aging.py
=====================
Accounts-payable aging report for payment follow-ups.

``get_invoice_aging`` walks the unpaid invoices in ``tool_modules._state``,
resolves each one's due date, and buckets it by how many days overdue it is.

This is a *domain* helper, not a decider. Unlike ``decisions.py`` it may read
the clock (``as_of=""`` means "today, UTC") and it reads live state directly.
If it is ever wired into the sandbox path, the clock read must move to the
control plane and be frozen into the snapshot, exactly as
``tools._snapshot_for`` does for ``decisions._decide_get_invoice_aging``.

Due-date resolution
-------------------
1. ``invoice["due_date"]`` wins when present and valid. This is the field
   ``decisions._decide_get_invoice_aging`` already reads, so the two agree.
2. Otherwise the due date is ``invoice_date + vendor payment_terms``, where
   ``invoice_date`` is the first of ``invoice_date`` / ``received_at`` /
   ``created_at`` that parses, and ``payment_terms`` is read from the vendor
   record in ``_state``. ``"NET 30"``, ``"NET30"``, ``30`` and
   ``"2/10 NET 30"`` all parse to 30 days. A missing or unparseable value
   falls back to ``DEFAULT_PAYMENT_TERMS_DAYS``.
3. If neither works the invoice is reported under ``"undated"`` rather than
   dropped, so a follow-up is never silently lost.

Buckets
-------
``CURRENT``  not yet due (``days_overdue <= 0``)
``1-30``     1-30 days overdue
``31-60``    31-60 days overdue
``60+``      more than 60 days overdue

Unpaid statuses
---------------
``RECEIVED``, ``MATCHED``, ``PENDING_APPROVAL``, ``APPROVED``. The current
state machine only ever sets ``RECEIVED``/``EXCEPTION``/``MATCHED`` on an
invoice and keeps approval state in ``_APPROVALS``; ``PENDING_APPROVAL`` and
``APPROVED`` are accepted so the report keeps working if the invoice status
enum grows.

Amounts are summed as floats, matching the rest of the codebase. Totals
assume a single currency (the seed is all USD); per-invoice ``currency`` is
reported so a mixed-currency store is visible rather than silently summed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _INVOICES,
    ToolError,
    _now,
)

__all__ = [
    "AGING_BUCKETS",
    "DEFAULT_PAYMENT_TERMS_DAYS",
    "UNPAID_STATUSES",
    "get_invoice_aging",
]

#: Days added to the invoice date when the vendor has no usable payment terms.
DEFAULT_PAYMENT_TERMS_DAYS = 30

#: Invoice statuses that still owe money and therefore belong in the report.
UNPAID_STATUSES: Tuple[str, ...] = (
    "RECEIVED",
    "MATCHED",
    "PENDING_APPROVAL",
    "APPROVED",
)

#: ``(key, label)`` pairs, in report order.
AGING_BUCKETS: Tuple[Tuple[str, str], ...] = (
    ("CURRENT", "Current/Not Due"),
    ("1-30", "1-30 days overdue"),
    ("31-60", "31-60 days overdue"),
    ("60+", "60+ days overdue"),
)

_BUCKET_LABELS = dict(AGING_BUCKETS)

# ``NET 30`` / ``NET30`` / ``2/10 NET 30`` -> 30. The last form is a discount
# term; the net figure is the one that sets the due date.
_NET_RE = re.compile(r"NET\s*(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float:
    """Coerce a numeric field to float, treating anything else as 0.0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _parse_iso_date(value: Any) -> Optional[date]:
    """Parse an ISO date or ISO timestamp, returning ``None`` on failure.

    ``_now()`` produces a full timestamp, so the first ten characters are
    used; ``date.fromisoformat`` only accepts the date portion.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _payment_terms_days(value: Any) -> int:
    """Resolve a vendor ``payment_terms`` value to a non-negative day count."""
    if isinstance(value, bool):
        return DEFAULT_PAYMENT_TERMS_DAYS
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return DEFAULT_PAYMENT_TERMS_DAYS
        match = _NET_RE.search(text)
        if match:
            return max(0, int(match.group(1)))
        try:
            return max(0, int(float(text)))
        except ValueError:
            return DEFAULT_PAYMENT_TERMS_DAYS
    return DEFAULT_PAYMENT_TERMS_DAYS


def _resolve_reference_date(as_of: str) -> date:
    """Turn the ``as_of`` argument into a reference date.

    An empty string means today (UTC). A non-string or a non-ISO value raises
    ``ToolError("INVALID_ARGUMENTS")``, matching the error style used by the
    rest of the domain layer.
    """
    if as_of is None:
        as_of = ""
    if not isinstance(as_of, str):
        raise ToolError("INVALID_ARGUMENTS", {"as_of": "must be a string"})
    text = as_of.strip()
    if not text:
        return date.fromisoformat(_now()[:10])
    parsed = _parse_iso_date(text)
    if parsed is None:
        raise ToolError("INVALID_ARGUMENTS", {"as_of": f"not an ISO date: {text}"})
    return parsed


def _resolve_due_date(
    invoice: Dict[str, Any], vendor: Dict[str, Any]
) -> Optional[date]:
    """Return the invoice's due date, or ``None`` if it cannot be resolved."""
    explicit = _parse_iso_date(invoice.get("due_date"))
    if explicit is not None:
        return explicit

    issued: Optional[date] = None
    for key in ("invoice_date", "received_at", "created_at"):
        issued = _parse_iso_date(invoice.get(key))
        if issued is not None:
            break
    if issued is None:
        return None

    return issued + timedelta(days=_payment_terms_days(vendor.get("payment_terms")))


def _bucket_for(days_overdue: int) -> str:
    """Map days overdue to a bucket key. Negative means not yet due."""
    if days_overdue <= 0:
        return "CURRENT"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    return "60+"


def _row(
    invoice: Dict[str, Any],
    vendor: Dict[str, Any],
    due: Optional[date],
    days_overdue: Optional[int],
    bucket: str,
) -> Dict[str, Any]:
    """Build one report row."""
    return {
        "invoice_id": invoice.get("invoice_id"),
        "vendor_id": invoice.get("vendor_id"),
        "vendor_name": vendor.get("name", ""),
        "po_id": invoice.get("po_id"),
        "status": invoice.get("status"),
        "amount": _as_float(invoice.get("amount")),
        "currency": invoice.get("currency") or vendor.get("currency", ""),
        "due_date": due.isoformat() if due is not None else None,
        "days_overdue": days_overdue,
        "bucket": bucket,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_invoice_aging(as_of: str = "") -> Dict[str, Any]:
    """Bucket unpaid invoices by how far past due they are.

    ``as_of`` is an ISO date (``"2024-01-31"``); an empty string means today
    in UTC. Raises ``ToolError("INVALID_ARGUMENTS")`` if it is not a string or
    not an ISO date.

    Returns::

        {
          "as_of": "2024-01-31",
          "buckets": {
            "CURRENT": {"bucket", "label", "count", "total_amount", "invoices"},
            "1-30":    {...},
            "31-60":   {...},
            "60+":     {...},
          },
          "undated": {"count", "total_amount", "invoices"},
          "totals": {"count", "total_amount"},
        }

    ``totals`` covers the dated invoices only; ``undated`` is reported
    separately so its amount is never folded into a bucket it does not belong
    to. Invoices inside a bucket are ordered most-overdue first.
    """
    reference = _resolve_reference_date(as_of)

    # Copy out under the lock, then compute outside it: the report is a pure
    # function of the copied records and should not hold the lock while it
    # formats them.
    with _LOCK:
        invoices = [
            dict(inv)
            for inv in _INVOICES.values()
            if inv.get("status") in UNPAID_STATUSES
        ]
        vendors = {vid: dict(v) for vid, v in _VENDORS.items()}

    buckets: Dict[str, Dict[str, Any]] = {
        key: {
            "bucket": key,
            "label": label,
            "count": 0,
            "total_amount": 0.0,
            "invoices": [],
        }
        for key, label in AGING_BUCKETS
    }
    undated: List[Dict[str, Any]] = []

    for invoice in invoices:
        vendor = vendors.get(invoice.get("vendor_id"), {})
        due = _resolve_due_date(invoice, vendor)
        if due is None:
            undated.append(_row(invoice, vendor, None, None, "UNDATED"))
            continue

        days_overdue = (reference - due).days
        bucket = _bucket_for(days_overdue)
        buckets[bucket]["invoices"].append(
            _row(invoice, vendor, due, days_overdue, bucket)
        )

    total_count = 0
    total_amount = 0.0
    for bucket in buckets.values():
        bucket["invoices"].sort(
            key=lambda row: (-row["days_overdue"], row["invoice_id"] or "")
        )
        bucket["count"] = len(bucket["invoices"])
        bucket["total_amount"] = round(
            sum(row["amount"] for row in bucket["invoices"]), 2
        )
        total_count += bucket["count"]
        total_amount += bucket["total_amount"]

    undated.sort(key=lambda row: row["invoice_id"] or "")

    return {
        "as_of": reference.isoformat(),
        "buckets": buckets,
        "undated": {
            "count": len(undated),
            "total_amount": round(sum(row["amount"] for row in undated), 2),
            "invoices": undated,
        },
        "totals": {
            "count": total_count,
            "total_amount": round(total_amount, 2),
        },
    }
