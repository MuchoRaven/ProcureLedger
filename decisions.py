"""
decisions.py
============
Deterministic evaluation logic for ProcureLedger agent proposals.
"""

from typing import Any, Dict, List

CONTRACT_VERSION = "1.0.0"

PROPOSAL_KINDS = {
    "PAYOUT_APPROVAL": "Approval required before settling any invoice payout.",
    "RECONCILIATION": "Three-way match verification between PO, invoice, and receipt.",
}

SNAPSHOT_SCOPES = ["vendor", "invoice", "purchase_order", "goods_receipt"]


def decide(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically evaluate a proposed action against safety and business rules.
    Returns a structured decision result.
    """
    kind = proposal.get("kind")
    payload = proposal.get("payload", {})

    if kind == "PAYOUT_APPROVAL":
        amount = payload.get("amount", 0.0)
        if amount <= 0:
            return {"approved": False, "reason": "Invalid payout amount: must be greater than zero."}
        if amount > 50000.0:
            return {"approved": False, "reason": "Payout exceeds single-transaction automated threshold ($50,000). Requires executive sign-off."}
        return {"approved": True, "reason": "Payout amount within acceptable limits. Awaiting human gate."}

    if kind == "RECONCILIATION":
        return {"approved": True, "reason": "Reconciliation proposal accepted for processing."}

    return {"approved": False, "reason": f"Unknown proposal kind: {kind}"}
