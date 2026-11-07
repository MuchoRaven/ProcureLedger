"""
tool_modules/_tools_vendor_compare.py
=====================================
SKU-scoped vendor-quote reads: list every quote for a SKU, and rank the
vendors that quoted it.

Two read tools live here:

* ``get_vendor_quotes(sku)`` - every quote for the SKU, with the vendor's name
  and status joined in, ordered by ``quote_id``.
* ``compare_vendors(sku, weights=None, as_of="")`` - the same quotes, filtered
  to the ones that can actually be bought from, scored on price, lead time and
  risk, and returned as a ranking.

Both report a missing SKU in-band with ``NO_QUOTES_FOR_SKU`` rather than an
empty list, so a typo in the SKU is distinguishable from a SKU nobody has
quoted yet - the same reasoning as ``_tools_sourcing.list_quotes``.

Scoring
-------
``compare_vendors`` is a weighted min-max ranking. Each criterion is
normalised across the eligible candidates to ``[0, 1]``, where 0 is the best
value in the set, then combined as::

    score = w_price * n_price + w_lead * n_lead + w_risk * n_risk

Lower is better, so the ranking is ascending by ``score``. A criterion that is
identical across every candidate normalises to 0.0 for all of them - it cannot
separate them, so it must not contribute a spurious difference.

Weights default to ``{"price": 0.5, "lead_time": 0.3, "risk": 0.2}``. A caller
may pass a subset; omitted keys default to 0.0. The weights are normalised to
sum to 1 and rounded to six decimal places before use, so the reported weights
and the reported scores always agree. Anything that is not a dict of
non-negative finite numbers with a positive total is ``INVALID_WEIGHTS``.

Risk tiers map to ``LOW=0``, ``MEDIUM=1``, ``HIGH=2``; an unrecognised tier is
treated as ``MEDIUM`` rather than dropped. HIGH-risk vendors are *ranked*, not
excluded: the comparison is informational, and the HIGH-risk block belongs at
the payout gate (``_apply_ap._check_create_approval``), not here.

Eligibility
-----------
A quote is ranked only when its own status is ``ACTIVE``, its vendor exists
and is ``ACTIVE``, its ``valid_until`` has not passed, and its ``unit_price``
is a positive finite number. Everything else is reported under ``excluded``
with a reason, so a candidate is never silently dropped - the same choice
``tool_modules.aging`` makes for an undated invoice.

Determinism
-----------
``as_of`` is the reference date for the expiry check; an empty value means
today (UTC), matching ``_tools_ap.get_invoice_aging``. Candidates are sorted
by ``quote_id`` *before* any arithmetic, so the float sums are computed in the
same order on every call. That matters because
``decisions._decide_compare_vendors`` must reproduce this output exactly from
the sandbox snapshot: a read whose proposal differs from the live one is
reported as ``stale``, and a scoring routine that iterated a dict in insertion
order would drift the moment two quotes were inserted in a different order.

This module imports ``_state`` (the stores and their lock), ``_codes`` (the two
error codes) and ``_util`` (``_as_float``, ``_utc_today``). It does not import
``tools``, ``decisions`` or ``sandbox``, so it cannot participate in an import
cycle.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Optional

from tool_modules._state import (
    _LOCK,
    _VENDORS,
    _VENDOR_QUOTES,
)
from tool_modules._codes import (
    NO_QUOTES_FOR_SKU,
    INVALID_WEIGHTS,
)
from tool_modules._util import _as_float, _utc_today

#: Default criterion weights. Price dominates, then lead time, then risk.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "price": 0.5,
    "lead_time": 0.3,
    "risk": 0.2,
}

#: The criteria, in a fixed order so the reported weights are canonical.
_WEIGHT_KEYS: tuple[str, ...] = ("price", "lead_time", "risk")

#: Risk tier -> numeric score. Lower is better, matching the other criteria.
_RISK_SCORE: dict[str, float] = {
    "LOW": 0.0,
    "MEDIUM": 1.0,
    "HIGH": 2.0,
}

#: Score for a tier this module does not recognise. Treated as MEDIUM rather
#: than dropped: an unknown tier is a data gap, not a disqualification.
_DEFAULT_RISK_SCORE = 1.0

#: Decimal places the normalised weights and the scores are rounded to. Fixed
#: so the direct call and the sandboxed decider cannot disagree on a float.
_PRECISION = 6


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _risk_score(value: Any) -> float:
    """Map a risk tier to its numeric score, defaulting to MEDIUM."""
    if isinstance(value, str):
        return _RISK_SCORE.get(value.strip().upper(), _DEFAULT_RISK_SCORE)
    return _DEFAULT_RISK_SCORE


def _normalise(values: list[float]) -> list[float]:
    """Min-max normalise ``values`` to ``[0, 1]``, best (lowest) first.

    A criterion that is identical across every candidate returns all zeros:
    it cannot separate them, so it must not contribute a spurious difference.
    """
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high - low <= 0:
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _resolve_weights(
    weights: Any,
) -> tuple[Optional[dict[str, float]], str]:
    """Resolve the caller's weights to a normalised, rounded dict.

    Returns ``(weights, "")`` on success or ``(None, reason)`` on failure. The
    weights are normalised to sum to 1 and rounded to ``_PRECISION`` places
    *before* they are used, so the reported weights and the reported scores
    always agree and the arithmetic is reproducible on both sides of the
    sandbox boundary.
    """
    if weights is None:
        raw = dict(_DEFAULT_WEIGHTS)
    elif not isinstance(weights, dict):
        return None, "weights must be an object"
    else:
        raw = {}
        for key, value in weights.items():
            if key not in _WEIGHT_KEYS:
                return None, f"unknown weight {key!r}"
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None, f"weight {key!r} must be a number"
            value = float(value)
            if not math.isfinite(value):
                return None, f"weight {key!r} must be finite"
            if value < 0:
                return None, f"weight {key!r} must be >= 0"
            raw[key] = value
        for key in _WEIGHT_KEYS:
            raw.setdefault(key, 0.0)

    total = sum(raw[key] for key in _WEIGHT_KEYS)
    if total <= 0:
        return None, "weights must not all be zero"
    return {
        key: round(raw[key] / total, _PRECISION) for key in _WEIGHT_KEYS
    }, ""


def _quote_row(quote: dict[str, Any], vendor: dict[str, Any]) -> dict[str, Any]:
    """Flatten one quote and its vendor into a report row.

    Flat rather than nested, matching ``tool_modules.aging._row``: a caller
    reading a comparison table should not have to walk two levels to find the
    vendor's name.
    """
    return {
        "quote_id": quote.get("quote_id"),
        "vendor_id": quote.get("vendor_id"),
        "vendor_name": vendor.get("name", ""),
        "vendor_status": vendor.get("status", ""),
        "unit_price": _as_float(quote.get("unit_price")),
        "currency": quote.get("currency") or vendor.get("currency", ""),
        "lead_time_days": int(_as_float(quote.get("lead_time_days"))),
        "moq": int(_as_float(quote.get("moq"))),
        "valid_until": quote.get("valid_until") or "",
        "payment_terms": (
            quote.get("payment_terms") or vendor.get("payment_terms", "")
        ),
        "risk_tier": quote.get("risk_tier") or vendor.get("risk_tier", ""),
        "status": quote.get("status", ""),
    }


def _ineligibility(
    quote: dict[str, Any],
    vendor: dict[str, Any],
    reference: date,
) -> str:
    """Return why ``quote`` cannot be ranked, or ``""`` if it can.

    The order is deliberate: an orphaned quote is reported as such before its
    own status is considered, because a missing vendor is the more fundamental
    problem. A malformed ``valid_until`` is treated as "not expired" rather
    than as a disqualification, mirroring ``_followup._is_late``: a bad date
    field is a data gap, and dropping the candidate would hide it.
    """
    if not vendor:
        return "VENDOR_NOT_FOUND"
    if quote.get("status") != "ACTIVE":
        return "QUOTE_INACTIVE"
    if vendor.get("status") != "ACTIVE":
        return "VENDOR_INACTIVE"

    valid_until = quote.get("valid_until")
    if isinstance(valid_until, str) and valid_until.strip():
        try:
            if date.fromisoformat(valid_until.strip()) < reference:
                return "QUOTE_EXPIRED"
        except ValueError:
            pass

    price = quote.get("unit_price")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return "INVALID_PRICE"
    if not math.isfinite(float(price)) or float(price) <= 0:
        return "INVALID_PRICE"
    return ""


def _quotes_for_sku(sku: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the SKU's quotes and the vendors they reference.

    Caller must hold ``_LOCK``. The quotes are sorted by ``quote_id`` here, at
    the single point they are read, so every caller iterates them in the same
    order and the float arithmetic downstream is reproducible.
    """
    quotes = [
        dict(quote)
        for quote in _VENDOR_QUOTES.values()
        if quote.get("sku") == sku
    ]
    quotes.sort(key=lambda quote: quote.get("quote_id") or "")
    vendors = {
        vendor_id: dict(_VENDORS[vendor_id])
        for vendor_id in {quote.get("vendor_id") for quote in quotes}
        if vendor_id in _VENDORS
    }
    return quotes, vendors


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


def get_vendor_quotes(sku: str) -> dict[str, Any]:
    """Return every vendor quote for ``sku``, ordered by ``quote_id``.

    No filtering: inactive quotes and inactive vendors are included, each row
    carrying its own ``status`` and the vendor's ``vendor_status``, so the
    caller can see the whole picture. ``compare_vendors`` is the tool that
    filters, and it reports what it dropped.

    A SKU with no quotes at all is ``NO_QUOTES_FOR_SKU`` rather than an empty
    list, so a typo is distinguishable from a SKU nobody has quoted yet.
    """
    if not isinstance(sku, str) or not sku.strip():
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "sku must be a non-empty string",
        }
    sku = sku.strip()

    with _LOCK:
        quotes, vendors = _quotes_for_sku(sku)

    if not quotes:
        return {"ok": False, "error": NO_QUOTES_FOR_SKU, "detail": sku}

    return {
        "ok": True,
        "sku": sku,
        "quotes": [
            _quote_row(quote, vendors.get(quote.get("vendor_id"), {}))
            for quote in quotes
        ],
    }


def compare_vendors(
    sku: str, weights: Any = None, as_of: str = ""
) -> dict[str, Any]:
    """Rank the vendors that quoted ``sku`` on price, lead time and risk.

    ``weights`` is an optional partial dict of criterion weights; see the
    module docstring for the scoring. ``as_of`` is an optional ISO date used
    for the expiry check; an empty value means today (UTC).

    Returns::

        {
          "ok": True,
          "sku": "SKU-HYD-001",
          "as_of": "2024-03-01",
          "weights": {"price": 0.5, "lead_time": 0.3, "risk": 0.2},
          "ranking": [
            {"rank": 1, "quote_id", "vendor_id", "vendor_name", "unit_price",
             "currency", "lead_time_days", "moq", "risk_tier",
             "payment_terms", "score", "components": {...}},
            ...
          ],
          "excluded": [{"quote_id", "vendor_id", "reason"}, ...],
        }

    A SKU with no quotes at all is ``NO_QUOTES_FOR_SKU``. A SKU whose quotes
    are all ineligible is ``ok: True`` with an empty ``ranking`` and a
    populated ``excluded``: the SKU exists, so reporting it as missing would
    be a lie, and the reasons are what the caller needs.
    """
    if not isinstance(sku, str) or not sku.strip():
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "sku must be a non-empty string",
        }
    sku = sku.strip()

    resolved, weights_error = _resolve_weights(weights)
    if resolved is None:
        return {"ok": False, "error": INVALID_WEIGHTS, "detail": weights_error}

    if as_of is None:
        as_of = ""
    if not isinstance(as_of, str):
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": "as_of must be a string",
        }
    reference = as_of.strip() or _utc_today()
    try:
        reference_date = date.fromisoformat(reference)
    except ValueError:
        return {
            "ok": False,
            "error": "INVALID_ARGUMENTS",
            "detail": f"as_of is not an ISO date: {reference}",
        }

    with _LOCK:
        quotes, vendors = _quotes_for_sku(sku)

    if not quotes:
        return {"ok": False, "error": NO_QUOTES_FOR_SKU, "detail": sku}

    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    excluded: list[dict[str, Any]] = []
    for quote in quotes:
        vendor = vendors.get(quote.get("vendor_id"), {})
        reason = _ineligibility(quote, vendor, reference_date)
        if reason:
            excluded.append(
                {
                    "quote_id": quote.get("quote_id"),
                    "vendor_id": quote.get("vendor_id"),
                    "reason": reason,
                }
            )
        else:
            eligible.append((quote, vendor))

    if not eligible:
        return {
            "ok": True,
            "sku": sku,
            "as_of": reference_date.isoformat(),
            "weights": resolved,
            "ranking": [],
            "excluded": excluded,
        }

    # ``eligible`` is already in ``quote_id`` order, so these three lists are
    # built in a fixed order and the normalisation below is reproducible.
    prices = [_as_float(quote.get("unit_price")) for quote, _ in eligible]
    leads = [
        float(int(_as_float(quote.get("lead_time_days"))))
        for quote, _ in eligible
    ]
    risks = [
        _risk_score(quote.get("risk_tier") or vendor.get("risk_tier"))
        for quote, vendor in eligible
    ]

    norm_price = _normalise(prices)
    norm_lead = _normalise(leads)
    norm_risk = _normalise(risks)

    rows: list[dict[str, Any]] = []
    for index, (quote, vendor) in enumerate(eligible):
        components = {
            "price": round(norm_price[index], _PRECISION),
            "lead_time": round(norm_lead[index], _PRECISION),
            "risk": round(norm_risk[index], _PRECISION),
        }
        score = round(
            components["price"] * resolved["price"]
            + components["lead_time"] * resolved["lead_time"]
            + components["risk"] * resolved["risk"],
            _PRECISION,
        )
        rows.append(
            {
                "quote_id": quote.get("quote_id"),
                "vendor_id": quote.get("vendor_id"),
                "vendor_name": vendor.get("name", ""),
                "unit_price": _as_float(quote.get("unit_price")),
                "currency": quote.get("currency") or vendor.get("currency", ""),
                "lead_time_days": int(_as_float(quote.get("lead_time_days"))),
                "moq": int(_as_float(quote.get("moq"))),
                "risk_tier": (
                    quote.get("risk_tier") or vendor.get("risk_tier", "")
                ),
                "payment_terms": (
                    quote.get("payment_terms")
                    or vendor.get("payment_terms", "")
                ),
                "score": score,
                "components": components,
            }
        )

    # Ties break on the raw criteria, then on ``vendor_id``, so the order is
    # total and two runs over the same state cannot disagree.
    rows.sort(
        key=lambda row: (
            row["score"],
            row["unit_price"],
            row["lead_time_days"],
            row["vendor_id"] or "",
        )
    )

    ranking = [
        {"rank": rank, **row} for rank, row in enumerate(rows, start=1)
    ]

    return {
        "ok": True,
        "sku": sku,
        "as_of": reference_date.isoformat(),
        "weights": resolved,
        "ranking": ranking,
        "excluded": excluded,
    }
