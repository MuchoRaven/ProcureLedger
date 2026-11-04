"""
tools.py
========
ProcureLedger - tool layer.

This module is the only place that touches invoice, approval and audit state.
``agent.py`` reaches it exclusively through the default-deny ``_DISPATCH``
allowlist, so any public function here that is not in ``ALLOWED_TOOLS`` stays
app-layer only.

App-layer only (never exposed to the model)
-------------------------------------------
* ``ingest_invoice``  - creates invoice records from the ERP feed.
* ``approve_payout``  - records a human approval decision.
* ``_execute_payout`` - settles an approved payout. Private on purpose.

Concurrency
-----------
State lives in module globals guarded by ``_LOCK``. Run the server with a
single worker (``uvicorn ... --workers 1``); the audit chain is process-local.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_HASH = "0" * 64
AMOUNT_TOLERANCE = 0.01

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Raised by ``_fail`` for states the caller should never reach."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> None:
    """Abort the current tool call with a structured error code."""
    raise ToolError(code, detail)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()

_VENDORS: Dict[str, Dict[str, Any]] = {
    "VEND-001": {
        "vendor_id": "VEND-001",
        "name": "Northwind Components",
        "status": "ACTIVE",
        "risk_tier": "LOW",
        "payment_terms": "NET30",
    },
    "VEND-002": {
        "vendor_id": "VEND-002",
        "name": "Cobalt Freight",
        "status": "ACTIVE",
        "risk_tier": "HIGH",
        "payment_terms": "NET15",
    },
    "VEND-003": {
        "vendor_id": "VEND-003",
        "name": "Apex Industrial",
        "status": "INACTIVE",
        "risk_tier": "LOW",
        "payment_terms": "NET45",
    },
}

_POS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "po_id": "PO-1001",
        "vendor_id": "VEND-001",
        "amount": 12500.00,
        "currency": "USD",
        "status": "OPEN",
    },
    "PO-1002": {
        "po_id": "PO-1002",
        "vendor_id": "VEND-002",
        "amount": 8000.00,
        "currency": "USD",
        "status": "OPEN",
    },
    "PO-1003": {
        "po_id": "PO-1003",
        "vendor_id": "VEND-001",
        "amount": 3000.00,
        "currency": "USD",
        "status": "OPEN",
    },
}

_RECEIPTS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "po_id": "PO-1001",
        "amount": 12500.00,
        "received_at": "2024-05-02T09:00:00Z",
    },
    "PO-1002": {
        "po_id": "PO-1002",
        "amount": 8000.00,
        "received_at": "2024-05-03T11:30:00Z",
    },
    "PO-1003": {
        "po_id": "PO-1003",
        "amount": 2500.00,
        "received_at": "2024-05-04T14:15:00Z",
    },
}

_INVOICES: Dict[str, Dict[str, Any]] = {
    "INV-2001": {
        "invoice_id": "INV-2001",
        "vendor_id": "VEND-001",
        "po_id": "PO-1001",
        "amount": 12500.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:00:00Z",
        "updated_at": "2024-05-05T08:00:00Z",
    },
    "INV-2002": {
        "invoice_id": "INV-2002",
        "vendor_id": "VEND-002",
        "po_id": "PO-1002",
        "amount": 8400.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:05:00Z",
        "updated_at": "2024-05-05T08:05:00Z",
    },
    "INV-2003": {
        "invoice_id": "INV-2003",
        "vendor_id": "VEND-001",
        "po_id": "PO-1003",
        "amount": 3000.00,
        "currency": "USD",
        "status": "RECEIVED",
        "created_at": "2024-05-05T08:10:00Z",
        "updated_at": "2024-05-05T08:10:00Z",
    },
}

_APPROVALS: Dict[str, Dict[str, Any]] = {}

_AUDIT: List[Dict[str, Any]] = []
_AUDIT_SEQ = 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """UTC timestamp, millisecond precision, ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical(obj: Any) -> str:
    """Deterministic JSON encoding used for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(entry: Dict[str, Any]) -> str:
    """Hash the immutable fields of an audit entry (``hash`` excluded)."""
    body = {
        "seq": entry["seq"],
        "ts": entry["ts"],
        "event_type": entry["event_type"],
        "payload": entry["payload"],
        "correlation_id": entry["correlation_id"],
        "prev_hash": entry["prev_hash"],
    }
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _append_audit(
    event_type: str, payload: Dict[str, Any], correlation_id: str = ""
) -> Dict[str, Any]:
    """Append one link to the hash chain and return the stored entry."""
    global _AUDIT_SEQ
    with _LOCK:
        _AUDIT_SEQ += 1
        prev_hash = _AUDIT[-1]["hash"] if _AUDIT else GENESIS_HASH
        entry: Dict[str, Any] = {
            "seq": _AUDIT_SEQ,
            "ts": _now(),
            "event_type": event_type,
            "payload": payload,
            "correlation_id": correlation_id or "",
            "prev_hash": prev_hash,
        }
        entry["hash"] = _compute_hash(entry)
        _AUDIT.append(entry)
        return entry


def _transition(
    invoice_id: str,
    allowed_from: Tuple[str, ...],
    to_status: str,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Move an invoice between states, refusing illegal transitions."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            _fail("INVOICE_NOT_FOUND", invoice_id)
        if invoice["status"] not in allowed_from:
            _fail("INVALID_TRANSITION", f"{invoice['status']} -> {to_status}")
        invoice["status"] = to_status
        invoice["updated_at"] = _now()
        return dict(invoice)


def _reset_state() -> None:
    """Test hook: clear approvals and the audit chain, keep seed records."""
    global _AUDIT_SEQ
    with _LOCK:
        _APPROVALS.clear()
        _AUDIT.clear()
        _AUDIT_SEQ = 0
        for invoice in _INVOICES.values():
            invoice["status"] = "RECEIVED"
            invoice["updated_at"] = invoice["created_at"]

# ---------------------------------------------------------------------------
# Allowlisted tools
# ---------------------------------------------------------------------------


def lookup_vendor(vendor_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Check vendor status and risk tier. Read-only."""
    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        return {"ok": True, "vendor": dict(vendor)}


def run_three_way_match(
    invoice_id: str, po_id: str, correlation_id: str = ""
) -> Dict[str, Any]:
    """Compare PO, goods receipt and invoice; set MATCHED or EXCEPTION."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }

        po = _POS.get(po_id)
        if po is None:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}

        if invoice["po_id"] != po_id:
            return {
                "ok": False,
                "error": "PO_INVOICE_MISMATCH",
                "invoice_po_id": invoice["po_id"],
                "po_id": po_id,
            }

        if invoice["vendor_id"] != po["vendor_id"]:
            return {
                "ok": False,
                "error": "VENDOR_MISMATCH",
                "invoice_vendor_id": invoice["vendor_id"],
                "po_vendor_id": po["vendor_id"],
            }

        if invoice["status"] not in ("RECEIVED", "EXCEPTION"):
            return {
                "ok": False,
                "error": "INVALID_STATE",
                "status": invoice["status"],
                "detail": "invoice is already matched or past the match stage",
            }

        mismatch_codes: List[str] = []
        receipt = _RECEIPTS.get(po_id)

        if receipt is None:
            mismatch_codes.append("RECEIPT_MISSING")
        elif receipt["amount"] + AMOUNT_TOLERANCE < po["amount"]:
            mismatch_codes.append("RECEIPT_SHORT")

        if abs(invoice["amount"] - po["amount"]) > AMOUNT_TOLERANCE:
            mismatch_codes.append("AMOUNT_MISMATCH")

        status = "MATCHED" if not mismatch_codes else "EXCEPTION"
        _transition(invoice_id, ("RECEIVED", "EXCEPTION"), status, correlation_id)
        _append_audit(
            "THREE_WAY_MATCH",
            {
                "invoice_id": invoice_id,
                "po_id": po_id,
                "status": status,
                "mismatch_codes": mismatch_codes,
            },
            correlation_id,
        )

        return {
            "ok": True,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "status": status,
            "mismatch_codes": mismatch_codes,
            "invoice_amount": invoice["amount"],
            "po_amount": po["amount"],
            "receipt_amount": receipt["amount"] if receipt else None,
        }


def request_payout_approval(
    vendor_id: str,
    amount: float,
    po_id: str,
    invoice_id: str,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Open a human approval gate. Does NOT move money."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}

    with _LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        if vendor["status"] != "ACTIVE":
            return {
                "ok": False,
                "error": "VENDOR_INACTIVE",
                "vendor_id": vendor_id,
                "status": vendor["status"],
            }
        if vendor["risk_tier"] == "HIGH":
            return {
                "ok": False,
                "error": "VENDOR_HIGH_RISK",
                "vendor_id": vendor_id,
                "risk_tier": vendor["risk_tier"],
            }

        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }
        if invoice["vendor_id"] != vendor_id:
            return {
                "ok": False,
                "error": "VENDOR_MISMATCH",
                "invoice_vendor_id": invoice["vendor_id"],
                "vendor_id": vendor_id,
            }
        if invoice["po_id"] != po_id:
            return {
                "ok": False,
                "error": "PO_INVOICE_MISMATCH",
                "invoice_po_id": invoice["po_id"],
                "po_id": po_id,
            }
        if invoice["status"] != "MATCHED":
            return {
                "ok": False,
                "error": "INVOICE_NOT_MATCHED",
                "status": invoice["status"],
            }
        if abs(float(amount) - invoice["amount"]) > AMOUNT_TOLERANCE:
            return {
                "ok": False,
                "error": "AMOUNT_MISMATCH",
                "requested": float(amount),
                "invoice_amount": invoice["amount"],
            }

        for existing in _APPROVALS.values():
            if (
                existing["invoice_id"] == invoice_id
                and existing["status"] in ("PENDING", "APPROVED")
            ):
                return {
                    "ok": False,
                    "error": "APPROVAL_ALREADY_PENDING",
                    "approval_id": existing["approval_id"],
                    "status": existing["status"],
                }

        approval_id = "APR-" + uuid.uuid4().hex[:8].upper()
        approval: Dict[str, Any] = {
            "approval_id": approval_id,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "vendor_id": vendor_id,
            "amount": float(amount),
            "currency": invoice.get("currency", "USD"),
            "status": "PENDING",
            "requested_by": "agent",
            "requested_at": _now(),
            "correlation_id": correlation_id,
            "approver": None,
            "decided_at": None,
            "executed_at": None,
        }
        _APPROVALS[approval_id] = approval
        _transition(invoice_id, ("MATCHED",), "PENDING_APPROVAL", correlation_id)
        _append_audit(
            "PAYOUT_APPROVAL_REQUESTED",
            {
                "approval_id": approval_id,
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "amount": approval["amount"],
            },
            correlation_id,
        )

        return {
            "ok": True,
            "approval_id": approval_id,
            "status": "PENDING",
            "requires_human_approval": True,
            "invoice_id": invoice_id,
            "vendor_id": vendor_id,
            "amount": approval["amount"],
            "currency": approval["currency"],
        }


def log_audit_event(
    event_type: str, payload: Dict[str, Any], correlation_id: str = ""
) -> Dict[str, Any]:
    """Record an agent reasoning note in the audit trail."""
    if not isinstance(event_type, str) or not event_type.strip():
        return {"ok": False, "error": "INVALID_EVENT_TYPE"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "INVALID_PAYLOAD"}

    entry = _append_audit(event_type.strip(), payload, correlation_id)
    return {"ok": True, "seq": entry["seq"], "hash": entry["hash"]}


def get_invoice(invoice_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Read an invoice record by ID."""
    with _LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
            }
        return {"ok": True, "invoice": dict(invoice)}


def get_approval(approval_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Read an approval record by ID."""
    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        return {"ok": True, "approval": dict(approval)}


def verify_audit_chain(correlation_id: str = "") -> Dict[str, Any]:
    """Recompute the audit hash chain and report the first divergence."""
    with _LOCK:
        prev_hash = GENESIS_HASH
        for index, entry in enumerate(_AUDIT):
            expected_seq = index + 1
            if entry.get("seq") != expected_seq:
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "SEQ_GAP",
                    "head_hash": _AUDIT[-1]["hash"] if _AUDIT else GENESIS_HASH,
                }
            if entry.get("prev_hash") != prev_hash:
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "PREV_HASH_MISMATCH",
                    "head_hash": _AUDIT[-1]["hash"] if _AUDIT else GENESIS_HASH,
                }
            try:
                recomputed: Optional[str] = _compute_hash(entry)
            except (KeyError, TypeError):
                recomputed = None
            if recomputed != entry.get("hash"):
                return {
                    "ok": True,
                    "valid": False,
                    "checked": index,
                    "diverged_at": entry.get("seq"),
                    "reason": "HASH_MISMATCH",
                    "head_hash": _AUDIT[-1]["hash"] if _AUDIT else GENESIS_HASH,
                }
            prev_hash = entry["hash"]

        return {
            "ok": True,
            "valid": True,
            "checked": len(_AUDIT),
            "head_hash": prev_hash,
        }

# ---------------------------------------------------------------------------
# App-layer only - never reachable from the agent
# ---------------------------------------------------------------------------


def ingest_invoice(
    invoice_id: str,
    vendor_id: str,
    po_id: str,
    amount: float,
    currency: str = "USD",
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Create an invoice record from the ERP feed."""
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        return {"ok": False, "error": "INVALID_INVOICE_ID"}
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}
    if amount <= 0:
        return {"ok": False, "error": "INVALID_AMOUNT", "amount": amount}

    with _LOCK:
        if invoice_id in _INVOICES:
            return {"ok": False, "error": "INVOICE_EXISTS", "invoice_id": invoice_id}
        if vendor_id not in _VENDORS:
            return {"ok": False, "error": "VENDOR_NOT_FOUND", "vendor_id": vendor_id}
        if po_id not in _POS:
            return {"ok": False, "error": "PO_NOT_FOUND", "po_id": po_id}

        now = _now()
        invoice: Dict[str, Any] = {
            "invoice_id": invoice_id,
            "vendor_id": vendor_id,
            "po_id": po_id,
            "amount": float(amount),
            "currency": currency or "USD",
            "status": "RECEIVED",
            "created_at": now,
            "updated_at": now,
        }
        _INVOICES[invoice_id] = invoice
        _append_audit(
            "INVOICE_INGESTED",
            {
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "po_id": po_id,
                "amount": invoice["amount"],
            },
            correlation_id,
        )
        return {"ok": True, "invoice": dict(invoice)}


def approve_payout(
    approval_id: str, approver: str, correlation_id: str = ""
) -> Dict[str, Any]:
    """Record a human approval decision. Does NOT move money."""
    if not isinstance(approver, str) or not approver.strip():
        return {"ok": False, "error": "INVALID_APPROVER"}

    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        if approval["status"] != "PENDING":
            return {
                "ok": False,
                "error": "APPROVAL_NOT_PENDING",
                "status": approval["status"],
            }

        invoice = _INVOICES.get(approval["invoice_id"])
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": approval["invoice_id"],
            }
        if invoice["status"] != "PENDING_APPROVAL":
            return {
                "ok": False,
                "error": "INVOICE_NOT_PENDING_APPROVAL",
                "status": invoice["status"],
            }

        approval["status"] = "APPROVED"
        approval["approver"] = approver.strip()
        approval["decided_at"] = _now()
        _transition(
            approval["invoice_id"], ("PENDING_APPROVAL",), "APPROVED", correlation_id
        )
        _append_audit(
            "PAYOUT_APPROVED",
            {
                "approval_id": approval_id,
                "approver": approval["approver"],
                "amount": approval["amount"],
            },
            correlation_id,
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": "APPROVED",
            "approver": approval["approver"],
        }


def _execute_payout(approval_id: str, correlation_id: str = "") -> Dict[str, Any]:
    """Settle an approved payout. Private: never exposed to the model."""
    with _LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        if approval["status"] != "APPROVED":
            return {
                "ok": False,
                "error": "APPROVAL_NOT_APPROVED",
                "status": approval["status"],
            }

        invoice = _INVOICES.get(approval["invoice_id"])
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": approval["invoice_id"],
            }
        if invoice["status"] != "APPROVED":
            return {
                "ok": False,
                "error": "INVOICE_NOT_APPROVED",
                "status": invoice["status"],
            }

        approval["status"] = "EXECUTED"
        approval["executed_at"] = _now()
        _transition(invoice["invoice_id"], ("APPROVED",), "PAID", correlation_id)
        _append_audit(
            "PAYOUT_EXECUTED",
            {
                "approval_id": approval_id,
                "invoice_id": invoice["invoice_id"],
                "vendor_id": approval["vendor_id"],
                "amount": approval["amount"],
            },
            correlation_id,
        )
        return {
            "ok": True,
            "approval_id": approval_id,
            "invoice_id": invoice["invoice_id"],
            "amount": approval["amount"],
            "status": "PAID",
            "settled_at": approval["executed_at"],
        }
