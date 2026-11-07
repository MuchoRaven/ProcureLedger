"""
decisions.py
============
Deterministic evaluation logic for ProcureLedger tool calls.

``decide`` is a pure function of ``(tool, arguments, snapshot)``. It runs twice
per call: once inside the sandbox against the read-only snapshot in the request
envelope, and once in the control plane against live state
(``sandbox._redeci``). The two proposals are compared for equality, so this
module must not read the clock, generate IDs, touch the filesystem, or import
``tools``/``config``. Anything non-deterministic belongs in ``tools._apply_*``.

``get_invoice_aging`` is the one decider that needs a notion of "today". It
never reads the clock: the reference date is either the caller's ``as_of``
argument or the date the control plane froze into the snapshot under
``snapshot["as_of"]``. Both are ISO date strings, so the arithmetic is pure and
the sandbox and the control plane always agree. The snapshot must also carry a
``due_date`` on each invoice; see ``tools._snapshot_for``.

Proposal contract (consumed by ``sandbox._apply`` and ``tools._apply_*``):

    read            {"kind": "read", "data": {...}}
    transition      {"kind": "transition", "invoice_id": str, "po_id": str}
    create_approval {"kind": "create_approval", "vendor_id": str,
                     "amount": float, "po_id": str, "invoice_id": str}
    audit_note      {"kind": "audit_note", "event_type": str, "payload": dict}
    create_po       {"kind": "create_po", "vendor_id": str, "amount": float,
                     "currency": str, "expected_delivery": str,
                     "requisition_id": str | None}
    record_receipt  {"kind": "record_receipt", "po_id": str, "amount": float}

Mutation proposals (``transition``, ``create_approval``, ``audit_note``,
``create_po``, ``record_receipt``) carry inputs only - never generated IDs,
timestamps or derived status - so the sandbox's proposal and the control
plane's re-decision compare equal. Read proposals carry deterministic derived
data, which compares equal for the same reason.

Registration
------------
Every tool needs two entries:

* ``PROPOSAL_KINDS[tool]`` - the proposal kinds the tool may emit. This is
  also the agent's allowlist (``agent.ALLOWED_TOOLS``), so a tool listed here
  is a name the model may attempt.
* ``SNAPSHOT_SCOPES[tool]`` - the selector keys the tool reads from its
  arguments. ``sandbox._snapshot`` forwards only these to
  ``tools._snapshot_for``; everything else is business data, not a selector.

The two tables are checked for key equality at import so they cannot drift.

Phase 1 boundary: RFQs and quotes are operator-owned
----------------------------------------------------
``create_rfq`` / ``submit_quote`` / ``close_rfq`` / ``cancel_rfq`` /
``award_quote`` and the read helpers ``get_rfq`` / ``get_quote`` /
``list_quotes`` are deliberately **absent** from ``PROPOSAL_KINDS`` and
``SNAPSHOT_SCOPES``. They are reachable only through ``app.py``'s
operator-only endpoints, exactly like the existing PO boundary
(``create_po`` is agent-facing, but ``close_po`` / ``cancel_po`` /
``record_goods_receipt``'s siblings in ``app.py`` are not).

Consequences, all intentional:

* The model cannot name an RFQ or quote tool, so ``sandbox.dispatch`` rejects
  it before any snapshot is built. There is no decider to write and no
  proposal kind to validate.
* ``SNAPSHOT_SCOPES`` is unchanged in this phase. Adding an RFQ tool would
  require a new selector key (``rfq_id`` / ``quote_id``) and a matching
  ``tools._snapshot_for`` branch; keeping the tables untouched is what makes
  "operator-owned" true rather than aspirational.
* ``award_quote`` raises its PO through the existing ``create_po`` path, so
  the award inherits the ``PO_CREATED`` audit entry. The ``rfq_id`` linkage on
  that PO must be stamped by ``award_quote``'s own mutation in ``tools.py`` -
  **not** by widening ``create_po``'s proposal, which would force
  ``SNAPSHOT_SCOPES["create_po"]`` to grow an ``rfq_id`` selector and break
  the "unchanged in this phase" rule.

``PHASE1_OPERATOR_ONLY_TOOLS`` names those tools so the boundary is testable
and the import-time guard below fails loudly if one is ever added to either
table. That guard is the point: a silent widening of the model's reach is
exactly the failure this phase is meant to prevent.

Vendor comparison
-----------------
``get_vendor_quotes`` and ``compare_vendors`` are agent-facing reads. Both are
registered here, so ``agent.ALLOWED_TOOLS`` includes them and the model may
call them. Their deciders reproduce ``tool_modules._tools_vendor_compare``
exactly - same filtering, same rounding, same tie-break - because a read whose
proposal differs from the live one is reported as ``stale``. The duplication
is forced: ``decisions`` must stay importable inside the sandbox, where
importing ``_tools_vendor_compare`` would pull in ``_state`` and the audit log.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, Optional, Tuple

CONTRACT_VERSION = "1.0.0"

# Match tolerance. Must equal ``tools.AMOUNT_TOLERANCE``; the two are asserted
# equal in ``test_sandbox_wiring.py`` so they cannot drift. It lives here as
# well because ``decisions`` must stay importable inside the sandbox, where
# importing ``tools`` would pull in ``config`` and the audit-log recovery.
AMOUNT_TOLERANCE = 0.01

# Tool -> proposal kinds that tool may emit. ``sandbox._validate`` rejects any
# envelope whose proposal kind is not listed here.
#
# Phase 1 RFQ/quote tools are deliberately absent; see the module docstring and
# ``PHASE1_OPERATOR_ONLY_TOOLS`` below. Do not add them here without also
# adding a decider, a ``SNAPSHOT_SCOPES`` entry and a ``tools._snapshot_for``
# branch - and without deciding that the model should be able to source
# procurement, which this phase explicitly does not.
PROPOSAL_KINDS: Dict[str, Tuple[str, ...]] = {
    "lookup_vendor": ("read",),
    "get_invoice": ("read",),
    "get_invoice_aging": ("read",),
    "run_three_way_match": ("transition",),
    "request_payout_approval": ("create_approval",),
    "log_audit_event": ("audit_note",),
    "create_po": ("create_po",),
    "record_goods_receipt": ("record_receipt",),
    "get_vendor_quotes": ("read",),
    "compare_vendors": ("read",),
}

# Tool -> selector keys the tool reads from its arguments. ``sandbox._snapshot``
# forwards only these to ``tools._snapshot_for``; every other argument is
# business data. ``as_of`` is not an ID but is a selector for the aging
# decider, so it is listed where it is read.
#
# This is a dict, not a tuple: ``sandbox.dispatch`` only does
# ``if tool not in decisions.SNAPSHOT_SCOPES``, and ``in`` on a dict tests
# keys, so that call site is unchanged.
#
# Unchanged in Phase 1 on purpose: no ``rfq_id`` / ``quote_id`` selector exists
# because no agent-facing tool reads one. See the module docstring.
SNAPSHOT_SCOPES: Dict[str, Tuple[str, ...]] = {
    "lookup_vendor": ("vendor_id",),
    "get_invoice": ("invoice_id",),
    "get_invoice_aging": ("invoice_id", "as_of"),
    "run_three_way_match": ("invoice_id", "po_id"),
    "request_payout_approval": ("vendor_id", "po_id", "invoice_id"),
    "log_audit_event": (),
    "create_po": ("vendor_id", "requisition_id"),
    "record_goods_receipt": ("po_id",),
    # ``sku`` is the one selector that widens rather than narrows: a vendor
    # comparison needs every quote for the SKU, not one. ``as_of`` is listed
    # for ``compare_vendors`` because its expiry check reads it; the decider
    # falls back to ``snapshot["as_of"]`` when the caller omits it.
    "get_vendor_quotes": ("sku",),
    "compare_vendors": ("sku", "as_of"),
}

# Phase 1 tools that are operator-owned and must never become agent-facing.
#
# They live in ``tools`` and are reachable only through ``app.py``'s
# operator-only endpoints. Naming them here - rather than leaving their absence
# from the tables implicit - makes the boundary explicit, testable, and
# enforced by the guard below. ``award_quote`` is included even though it
# delegates to the agent-facing ``create_po``: the *tool name* the model could
# attempt is what matters, and that name must stay out of ``PROPOSAL_KINDS``.
PHASE1_OPERATOR_ONLY_TOOLS = frozenset(
    {
        "create_rfq",
        "get_rfq",
        "submit_quote",
        "get_quote",
        "list_quotes",
        "close_rfq",
        "cancel_rfq",
        "award_quote",
    }
)

# Guard against drift: every tool in ``PROPOSAL_KINDS`` must declare its
# selector keys here, and vice versa. A mismatch is a programming error, so
# fail at import rather than at the first tool call.
if set(SNAPSHOT_SCOPES) != set(PROPOSAL_KINDS):
    raise RuntimeError(
        "SNAPSHOT_SCOPES and PROPOSAL_KINDS disagree: "
        f"only in PROPOSAL_KINDS={sorted(set(PROPOSAL_KINDS) - set(SNAPSHOT_SCOPES))}, "
        f"only in SNAPSHOT_SCOPES={sorted(set(SNAPSHOT_SCOPES) - set(PROPOSAL_KINDS))}"
    )

# Guard the Phase 1 boundary. An RFQ/quote tool appearing in either table would
# silently widen the model's reach - the exact failure this phase prevents - so
# fail at import rather than at the first dispatch.
_leaked_proposal_kinds = PHASE1_OPERATOR_ONLY_TOOLS & set(PROPOSAL_KINDS)
if _leaked_proposal_kinds:
    raise RuntimeError(
        "Phase 1 RFQ/quote tools must stay operator-owned, but these are in "
        f"PROPOSAL_KINDS: {sorted(_leaked_proposal_kinds)}"
    )

_leaked_snapshot_scopes = PHASE1_OPERATOR_ONLY_TOOLS & set(SNAPSHOT_SCOPES)
if _leaked_snapshot_scopes:
    raise RuntimeError(
        "Phase 1 RFQ/quote tools must stay operator-owned, but these are in "
        f"SNAPSHOT_SCOPES: {sorted(_leaked_snapshot_scopes)}"
    )


def _error(code: str, detail: str = "") -> Dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}


def _proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "proposal": proposal}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_float(value: Any) -> float:
    """Coerce a numeric field to float, treating anything else as 0.0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _aging_bucket(days_outstanding: int) -> str:
    """Map days past due to a standard AP aging bucket.

    ``days_outstanding`` is negative when the invoice is not yet due.
    """
    if days_outstanding <= 0:
        return "CURRENT"
    if days_outstanding <= 30:
        return "1-30"
    if days_outstanding <= 60:
        return "31-60"
    if days_outstanding <= 90:
        return "61-90"
    return "90+"


# ---------------------------------------------------------------------------
# Per-tool deciders
# ---------------------------------------------------------------------------


def _decide_lookup_vendor(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    vendor_id = arguments.get("vendor_id")
    if not _nonempty_str(vendor_id):
        return _error("INVALID_ARGUMENTS", "vendor_id must be a non-empty string")
    vendor = snapshot.get("vendors", {}).get(vendor_id)
    if vendor is None:
        return _error("VENDOR_NOT_FOUND", vendor_id)
    return _proposal({"kind": "read", "data": {"vendor": dict(vendor)}})


def _decide_get_invoice(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    invoice_id = arguments.get("invoice_id")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    return _proposal({"kind": "read", "data": {"invoice": dict(invoice)}})


def _decide_get_invoice_aging(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    invoice_id = arguments.get("invoice_id")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")

    as_of = arguments.get("as_of", "")
    if as_of is None:
        as_of = ""
    if not isinstance(as_of, str):
        return _error("INVALID_ARGUMENTS", "as_of must be a string")
    as_of = as_of.strip()

    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)

    # Resolve the reference date without reading the clock: an explicit
    # ``as_of`` wins, otherwise fall back to the date the control plane froze
    # into the snapshot.
    reference = as_of or snapshot.get("as_of", "")
    if not _nonempty_str(reference):
        return _error("INVALID_SNAPSHOT", "snapshot is missing as_of")
    try:
        reference_date = date.fromisoformat(reference)
    except ValueError:
        return _error("INVALID_ARGUMENTS", f"as_of is not an ISO date: {reference}")

    due = invoice.get("due_date")
    if not _nonempty_str(due):
        return _error("AGING_UNAVAILABLE", f"invoice {invoice_id} has no due_date")
    try:
        due_date = date.fromisoformat(due)
    except ValueError:
        return _error("AGING_UNAVAILABLE", f"invoice {invoice_id} has a bad due_date")

    days_outstanding = (reference_date - due_date).days
    return _proposal(
        {
            "kind": "read",
            "data": {
                "aging": {
                    "invoice_id": invoice_id,
                    "as_of": reference_date.isoformat(),
                    "due_date": due_date.isoformat(),
                    "days_outstanding": days_outstanding,
                    "bucket": _aging_bucket(days_outstanding),
                }
            },
        }
    )


def _decide_run_three_way_match(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    invoice_id = arguments.get("invoice_id")
    po_id = arguments.get("po_id")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    if not _nonempty_str(po_id):
        return _error("INVALID_ARGUMENTS", "po_id must be a non-empty string")

    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    po = snapshot.get("pos", {}).get(po_id)
    if po is None:
        return _error("PO_NOT_FOUND", po_id)
    if invoice.get("po_id") != po_id:
        return _error(
            "PO_INVOICE_MISMATCH",
            f"invoice {invoice_id} is on {invoice.get('po_id')}",
        )
    if invoice.get("vendor_id") != po.get("vendor_id"):
        return _error(
            "VENDOR_MISMATCH",
            f"invoice {invoice.get('vendor_id')} vs PO {po.get('vendor_id')}",
        )
    if invoice.get("status") not in ("RECEIVED", "EXCEPTION"):
        return _error("INVALID_STATE", f"invoice status is {invoice.get('status')}")

    # Receipts are deliberately not checked here: a short receipt is a legal
    # transition that ``tools.run_three_way_match`` records as EXCEPTION. The
    # proposal carries inputs only, so the outcome is recomputed on apply.
    return _proposal({"kind": "transition", "invoice_id": invoice_id, "po_id": po_id})


def _decide_request_payout_approval(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    vendor_id = arguments.get("vendor_id")
    po_id = arguments.get("po_id")
    invoice_id = arguments.get("invoice_id")
    amount = arguments.get("amount")

    if not _nonempty_str(vendor_id):
        return _error("INVALID_ARGUMENTS", "vendor_id must be a non-empty string")
    if not _nonempty_str(po_id):
        return _error("INVALID_ARGUMENTS", "po_id must be a non-empty string")
    if not _nonempty_str(invoice_id):
        return _error("INVALID_ARGUMENTS", "invoice_id must be a non-empty string")
    # ``json.loads`` accepts NaN/Infinity by default; reject them here so the
    # proposal stays comparable (NaN != NaN would fail the equality check).
    if not _is_number(amount) or not math.isfinite(float(amount)):
        return _error("INVALID_AMOUNT", "amount must be a finite number")
    if amount <= 0:
        return _error("INVALID_AMOUNT", "amount must be greater than zero")

    vendor = snapshot.get("vendors", {}).get(vendor_id)
    if vendor is None:
        return _error("VENDOR_NOT_FOUND", vendor_id)
    if vendor.get("status") != "ACTIVE":
        return _error("VENDOR_INACTIVE", f"{vendor_id} is {vendor.get('status')}")
    if vendor.get("risk_tier") == "HIGH":
        return _error("VENDOR_HIGH_RISK", f"{vendor_id} is HIGH risk")

    invoice = snapshot.get("invoices", {}).get(invoice_id)
    if invoice is None:
        return _error("INVOICE_NOT_FOUND", invoice_id)
    if invoice.get("vendor_id") != vendor_id:
        return _error(
            "VENDOR_MISMATCH",
            f"invoice {invoice.get('vendor_id')} vs {vendor_id}",
        )
    if invoice.get("po_id") != po_id:
        return _error(
            "PO_INVOICE_MISMATCH",
            f"invoice {invoice.get('po_id')} vs {po_id}",
        )
    if invoice.get("status") != "MATCHED":
        return _error(
            "INVOICE_NOT_MATCHED", f"invoice status is {invoice.get('status')}"
        )
    if abs(float(amount) - float(invoice.get("amount", 0.0))) > AMOUNT_TOLERANCE:
        return _error(
            "AMOUNT_MISMATCH",
            f"requested {amount} vs invoice {invoice.get('amount')}",
        )

    for existing in snapshot.get("approvals", {}).values():
        if (
            existing.get("invoice_id") == invoice_id
            and existing.get("status") in ("PENDING", "APPROVED")
        ):
            return _error(
                "APPROVAL_ALREADY_PENDING",
                f"{existing.get('approval_id')} is {existing.get('status')}",
            )

    return _proposal(
        {
            "kind": "create_approval",
            "vendor_id": vendor_id,
            "amount": float(amount),
            "po_id": po_id,
            "invoice_id": invoice_id,
        }
    )


def _decide_log_audit_event(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    event_type = arguments.get("event_type")
    payload = arguments.get("payload")
    if not _nonempty_str(event_type) or not event_type.strip():
        return _error("INVALID_EVENT_TYPE", "event_type must be a non-empty string")
    if not isinstance(payload, dict):
        return _error("INVALID_PAYLOAD", "payload must be an object")
    return _proposal(
        {
            "kind": "audit_note",
            "event_type": event_type.strip(),
            "payload": payload,
        }
    )


def _decide_create_po(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a PO-creation request and emit a ``create_po`` proposal.

    Mirrors ``tools._check_create_po``: the vendor must exist and be ACTIVE,
    and a linked requisition must exist and still be OPEN. The proposal carries
    inputs only - the PO ID and timestamps are generated in
    ``tools._apply_create_po`` - so the sandbox and the control plane compare
    equal. ``expected_delivery`` is normalised to ISO here so the proposal is
    canonical; ``_apply_create_po`` normalises it again, which is a no-op.

    ``rfq_id`` is deliberately not a field here. An award's RFQ linkage is
    stamped by ``tools.award_quote``'s own mutation, not by widening this
    proposal - doing the latter would require an ``rfq_id`` selector in
    ``SNAPSHOT_SCOPES["create_po"]`` and break the Phase 1 "unchanged" rule.
    """
    vendor_id = arguments.get("vendor_id")
    amount = arguments.get("amount")
    currency = arguments.get("currency", "USD")
    expected_delivery = arguments.get("expected_delivery", "")
    requisition_id = arguments.get("requisition_id") or None

    if not _nonempty_str(vendor_id):
        return _error("INVALID_ARGUMENTS", "vendor_id must be a non-empty string")
    if not _is_number(amount) or not math.isfinite(float(amount)):
        return _error("INVALID_AMOUNT", "amount must be a finite number")
    if amount <= 0:
        return _error("INVALID_AMOUNT", "amount must be greater than zero")
    if not _nonempty_str(currency) or not currency.strip():
        return _error("INVALID_ARGUMENTS", "currency must be a non-empty string")
    if expected_delivery is None:
        expected_delivery = ""
    if not isinstance(expected_delivery, str):
        return _error("INVALID_ARGUMENTS", "expected_delivery must be a string")
    expected_delivery = expected_delivery.strip()
    if expected_delivery:
        try:
            expected_delivery = date.fromisoformat(expected_delivery).isoformat()
        except ValueError:
            return _error(
                "INVALID_ARGUMENTS", "expected_delivery is not an ISO date"
            )
    if requisition_id is not None and not _nonempty_str(requisition_id):
        return _error(
            "INVALID_ARGUMENTS", "requisition_id must be a non-empty string"
        )

    vendor = snapshot.get("vendors", {}).get(vendor_id)
    if vendor is None:
        return _error("VENDOR_NOT_FOUND", vendor_id)
    if vendor.get("status") != "ACTIVE":
        return _error("VENDOR_INACTIVE", f"{vendor_id} is {vendor.get('status')}")

    if requisition_id is not None:
        requisition = snapshot.get("requisitions", {}).get(requisition_id)
        if requisition is None:
            return _error("REQUISITION_NOT_FOUND", requisition_id)
        if requisition.get("status") != "OPEN":
            return _error(
                "REQUISITION_NOT_OPEN",
                f"{requisition_id} is {requisition.get('status')}",
            )

    return _proposal(
        {
            "kind": "create_po",
            "vendor_id": vendor_id,
            "amount": float(amount),
            "currency": currency.strip(),
            "expected_delivery": expected_delivery,
            "requisition_id": requisition_id,
        }
    )


def _decide_record_goods_receipt(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate a goods-receipt request and emit a ``record_receipt`` proposal.

    Mirrors ``tools._check_record_receipt``: the PO must exist and be OPEN, and
    the cumulative received amount (existing receipts plus this one) must not
    exceed the PO amount beyond ``AMOUNT_TOLERANCE``. The over-receipt check is
    only possible because ``tools._snapshot_for`` pulls a PO's receipts into
    the snapshot alongside the PO itself. The proposal carries inputs only -
    the receipt ID and timestamp are generated in
    ``tools._apply_record_receipt``.
    """
    po_id = arguments.get("po_id")
    amount = arguments.get("amount")

    if not _nonempty_str(po_id):
        return _error("INVALID_ARGUMENTS", "po_id must be a non-empty string")
    if not _is_number(amount) or not math.isfinite(float(amount)):
        return _error("INVALID_AMOUNT", "amount must be a finite number")
    if amount <= 0:
        return _error("INVALID_AMOUNT", "amount must be greater than zero")

    po = snapshot.get("pos", {}).get(po_id)
    if po is None:
        return _error("PO_NOT_FOUND", po_id)
    if po.get("status") != "OPEN":
        return _error("PO_NOT_OPEN", f"{po_id} is {po.get('status')}")

    received = sum(
        _as_float(receipt.get("amount"))
        for receipt in snapshot.get("receipts", {}).values()
        if receipt.get("po_id") == po_id
    )
    po_amount = _as_float(po.get("amount"))
    if received + float(amount) > po_amount + AMOUNT_TOLERANCE:
        return _error(
            "RECEIPT_EXCEEDS_PO",
            f"po {po_id} amount {po_amount}, received {received}, "
            f"requested {float(amount)}",
        )

    return _proposal(
        {"kind": "record_receipt", "po_id": po_id, "amount": float(amount)}
    )


# ---------------------------------------------------------------------------
# Vendor-comparison deciders
# ---------------------------------------------------------------------------
#
# These mirror ``tool_modules._tools_vendor_compare`` exactly. The duplication
# is deliberate: ``decisions`` must stay importable inside the sandbox, where
# importing ``_tools_vendor_compare`` would pull in ``_state`` and the audit
# log. The two are kept in step by a test that asserts the decider's proposal
# equals the tool's result for the same snapshot.
#
# Every detail matters, because ``sandbox._redeci`` compares a read's proposal
# against the live re-decision and flags any difference as ``stale``:
#
# * candidates are sorted by ``quote_id`` before any arithmetic, so the float
#   sums are order-independent;
# * weights are normalised and rounded to ``_PRECISION`` before use, so the
#   reported weights and the reported scores always agree;
# * a criterion that is constant across the candidates normalises to 0.0 for
#   all of them, so it cannot contribute a spurious difference;
# * ties break on ``(score, unit_price, lead_time_days, vendor_id)``, so the
#   order is total.

_WEIGHT_KEYS: Tuple[str, ...] = ("price", "lead_time", "risk")

_DEFAULT_WEIGHTS: Dict[str, float] = {
    "price": 0.5,
    "lead_time": 0.3,
    "risk": 0.2,
}

_RISK_SCORE: Dict[str, float] = {
    "LOW": 0.0,
    "MEDIUM": 1.0,
    "HIGH": 2.0,
}

_DEFAULT_RISK_SCORE = 1.0

_PRECISION = 6


def _risk_score(value: Any) -> float:
    """Map a risk tier to its numeric score, defaulting to MEDIUM."""
    if isinstance(value, str):
        return _RISK_SCORE.get(value.strip().upper(), _DEFAULT_RISK_SCORE)
    return _DEFAULT_RISK_SCORE


def _normalise(values: list) -> list:
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


def _resolve_weights(weights: Any) -> Tuple[Optional[Dict[str, float]], str]:
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
            if not _is_number(value):
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
    return {key: round(raw[key] / total, _PRECISION) for key in _WEIGHT_KEYS}, ""


def _vendor_quote_row(
    quote: Dict[str, Any], vendor: Dict[str, Any]
) -> Dict[str, Any]:
    """Flatten one quote and its vendor into a report row."""
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


def _vendor_quote_ineligibility(
    quote: Dict[str, Any],
    vendor: Dict[str, Any],
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
    if not _is_number(price):
        return "INVALID_PRICE"
    if not math.isfinite(float(price)) or float(price) <= 0:
        return "INVALID_PRICE"
    return ""


def _vendor_quotes_for_sku(
    snapshot: Dict[str, Any], sku: str
) -> Tuple[list, Dict[str, Any]]:
    """Return the SKU's quotes and the snapshot's vendor table.

    The quotes are sorted by ``quote_id`` here, at the single point they are
    read, so every caller iterates them in the same order and the float
    arithmetic downstream is reproducible.
    """
    quotes = [
        dict(quote)
        for quote in snapshot.get("vendor_quotes", {}).values()
        if quote.get("sku") == sku
    ]
    quotes.sort(key=lambda quote: quote.get("quote_id") or "")
    return quotes, snapshot.get("vendors", {})


def _decide_get_vendor_quotes(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    """Return every vendor quote for ``sku``, ordered by ``quote_id``.

    No filtering: inactive quotes and inactive vendors are included, each row
    carrying its own ``status`` and the vendor's ``vendor_status``, so the
    caller can see the whole picture. ``compare_vendors`` is the tool that
    filters, and it reports what it dropped.

    A SKU with no quotes at all is ``NO_QUOTES_FOR_SKU`` rather than an empty
    list, so a typo is distinguishable from a SKU nobody has quoted yet.
    """
    sku = arguments.get("sku")
    if not _nonempty_str(sku) or not sku.strip():
        return _error("INVALID_ARGUMENTS", "sku must be a non-empty string")
    sku = sku.strip()

    quotes, vendors = _vendor_quotes_for_sku(snapshot, sku)
    if not quotes:
        return _error("NO_QUOTES_FOR_SKU", sku)

    return _proposal(
        {
            "kind": "read",
            "data": {
                "sku": sku,
                "quotes": [
                    _vendor_quote_row(quote, vendors.get(quote.get("vendor_id"), {}))
                    for quote in quotes
                ],
            },
        }
    )


def _decide_compare_vendors(
    arguments: Dict[str, Any], snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    """Rank the vendors that quoted ``sku`` on price, lead time and risk.

    ``weights`` is an optional partial dict of criterion weights; see the
    module docstring for the scoring. ``as_of`` is an optional ISO date used
    for the expiry check; an empty value falls back to the date the control
    plane froze into the snapshot, so the decider never reads the clock.

    A SKU with no quotes at all is ``NO_QUOTES_FOR_SKU``. A SKU whose quotes
    are all ineligible is ``ok: True`` with an empty ``ranking`` and a
    populated ``excluded``: the SKU exists, so reporting it as missing would
    be a lie, and the reasons are what the caller needs.
    """
    sku = arguments.get("sku")
    if not _nonempty_str(sku) or not sku.strip():
        return _error("INVALID_ARGUMENTS", "sku must be a non-empty string")
    sku = sku.strip()

    resolved, weights_error = _resolve_weights(arguments.get("weights"))
    if resolved is None:
        return _error("INVALID_WEIGHTS", weights_error)

    as_of = arguments.get("as_of", "")
    if as_of is None:
        as_of = ""
    if not isinstance(as_of, str):
        return _error("INVALID_ARGUMENTS", "as_of must be a string")
    reference = as_of.strip() or snapshot.get("as_of", "")
    if not _nonempty_str(reference):
        return _error("INVALID_SNAPSHOT", "snapshot is missing as_of")
    try:
        reference_date = date.fromisoformat(reference)
    except ValueError:
        return _error("INVALID_ARGUMENTS", f"as_of is not an ISO date: {reference}")

    quotes, vendors = _vendor_quotes_for_sku(snapshot, sku)
    if not quotes:
        return _error("NO_QUOTES_FOR_SKU", sku)

    eligible: list = []
    excluded: list = []
    for quote in quotes:
        vendor = vendors.get(quote.get("vendor_id"), {})
        reason = _vendor_quote_ineligibility(quote, vendor, reference_date)
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
        return _proposal(
            {
                "kind": "read",
                "data": {
                    "sku": sku,
                    "as_of": reference_date.isoformat(),
                    "weights": resolved,
                    "ranking": [],
                    "excluded": excluded,
                },
            }
        )

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

    rows: list = []
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

    return _proposal(
        {
            "kind": "read",
            "data": {
                "sku": sku,
                "as_of": reference_date.isoformat(),
                "weights": resolved,
                "ranking": ranking,
                "excluded": excluded,
            },
        }
    )


_DECIDERS = {
    "lookup_vendor": _decide_lookup_vendor,
    "get_invoice": _decide_get_invoice,
    "get_invoice_aging": _decide_get_invoice_aging,
    "run_three_way_match": _decide_run_three_way_match,
    "request_payout_approval": _decide_request_payout_approval,
    "log_audit_event": _decide_log_audit_event,
    "create_po": _decide_create_po,
    "record_goods_receipt": _decide_record_goods_receipt,
    "get_vendor_quotes": _decide_get_vendor_quotes,
    "compare_vendors": _decide_compare_vendors,
}


def decide(tool: str, arguments: Any, snapshot: Any) -> Dict[str, Any]:
    """Evaluate one tool call against a read-only snapshot.

    Returns ``{"ok": True, "proposal": {...}}`` or
    ``{"ok": False, "error": str, "detail": str}``. Pure: no I/O, no clock, no
    ID generation, so the sandbox and the control plane always agree.
    """
    decider = _DECIDERS.get(tool)
    if decider is None:
        return _error("TOOL_NOT_ALLOWED", str(tool))
    if not isinstance(arguments, dict):
        return _error("INVALID_ARGUMENTS", "arguments must be an object")
    if not isinstance(snapshot, dict):
        return _error("INVALID_SNAPSHOT", "snapshot must be an object")
    return decider(arguments, snapshot)
