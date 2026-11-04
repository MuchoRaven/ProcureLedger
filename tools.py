"""
tools.py
========
ProcureLedger - sandboxed tool layer.

Design rules enforced here
--------------------------
* Standard library only. No network, no database, no filesystem access beyond
  the append-only audit log.
* Every state change and every financial decision emits a structured JSON
  audit event into ``audit_log.jsonl``.
* The audit log is a SHA-256 hash chain (``prev_hash`` + canonical payload),
  written by a single thread-locked writer and fsync'd on every append.
* ``_execute_payout`` is PRIVATE. It is not part of the agent allowlist and
  must only ever be called from the application layer after a human approval.
* ``ingest_invoice`` is app-layer only. It is not part of the agent allowlist;
  the agent can never create an invoice.
* Public functions never raise for expected business failures; they return
  ``{"ok": False, "error": "<CODE>", ...}`` so the orchestrator can reason
  about them instead of catching exceptions.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration (soft-imported from config.py when present)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - config.py is optional at this layer
    import config as _config
except Exception:  # pragma: no cover
    _config = None


def _cfg(name: str, default: Any) -> Any:
    """Return ``config.<name>`` when available, else the local default."""
    if _config is not None and hasattr(_config, name):
        return getattr(_config, name)
    return default


AUDIT_LOG_PATH: str = _cfg("AUDIT_LOG_PATH", "audit_log.jsonl")
GENESIS_HASH: str = "0" * 64
MONEY_TOLERANCE: float = float(_cfg("MONEY_TOLERANCE", 0.01))
MAX_APPROVAL_AMOUNT: float = float(_cfg("MAX_APPROVAL_AMOUNT", 1_000_000.0))

# Identities that may never satisfy the human-in-the-loop gate.
HUMAN_APPROVER_DENYLIST = frozenset(
    {"", "agent", "llm", "model", "system", "auto", "bot", "procureledger-agent"}
)

# ---------------------------------------------------------------------------
# Invoice state machine
# ---------------------------------------------------------------------------

STATE_RECEIVED = "RECEIVED"
STATE_MATCHED = "MATCHED"
STATE_EXCEPTION = "EXCEPTION"
STATE_PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
STATE_PAID = "PAID"
STATE_REJECTED = "REJECTED"

ALL_STATES = frozenset(
    {
        STATE_RECEIVED,
        STATE_MATCHED,
        STATE_EXCEPTION,
        STATE_PENDING_HUMAN_APPROVAL,
        STATE_PAID,
        STATE_REJECTED,
    }
)

# Terminal states have no outgoing edges.
_ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    STATE_RECEIVED: frozenset({STATE_MATCHED, STATE_EXCEPTION, STATE_REJECTED}),
    STATE_MATCHED: frozenset(
        {STATE_PENDING_HUMAN_APPROVAL, STATE_EXCEPTION, STATE_REJECTED}
    ),
    STATE_EXCEPTION: frozenset({STATE_MATCHED, STATE_REJECTED}),
    STATE_PENDING_HUMAN_APPROVAL: frozenset({STATE_PAID, STATE_REJECTED}),
    STATE_PAID: frozenset(),
    STATE_REJECTED: frozenset(),
}

# Approval record lifecycle.
APPROVAL_PENDING = "PENDING"
APPROVAL_CONSUMED = "CONSUMED"
APPROVAL_REJECTED = "REJECTED"

# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

# Guards the in-memory stores. Re-entrant so nested helpers can lock freely.
_STATE_LOCK = threading.RLock()

# Single-writer guard for the audit log. Never acquired before _STATE_LOCK.
_AUDIT_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Mock data stores
# ---------------------------------------------------------------------------

_VENDORS: Dict[str, Dict[str, Any]] = {
    "VEND-001": {
        "vendor_id": "VEND-001",
        "name": "Northwind Industrial Supply",
        "status": "ACTIVE",
        "risk_tier": "LOW",
        "country": "US",
        "payment_terms": "NET30",
        "remit_to": "ACH ****4471",
        "onboarded_at": "2021-03-14",
    },
    "VEND-002": {
        "vendor_id": "VEND-002",
        "name": "Apex Global Trading Ltd",
        "status": "INACTIVE",
        "risk_tier": "HIGH",
        "country": "CY",
        "payment_terms": "PREPAID",
        "remit_to": "WIRE ****9902",
        "onboarded_at": "2024-01-08",
        "notes": "Sanctions screening flagged 2024-02-11; payouts frozen pending review.",
    },
    "VEND-003": {
        "vendor_id": "VEND-003",
        "name": "Cedar Valley Components",
        "status": "ACTIVE",
        "risk_tier": "MEDIUM",
        "country": "US",
        "payment_terms": "NET45",
        "remit_to": "ACH ****3310",
        "onboarded_at": "2022-07-30",
    },
}

_POS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "po_id": "PO-1001",
        "vendor_id": "VEND-001",
        "currency": "USD",
        "status": "OPEN",
        "issued_at": "2024-04-28",
        "lines": [
            {
                "sku": "SKU-100",
                "description": "Galvanised steel bracket",
                "qty": 100,
                "unit_price": 12.50,
            }
        ],
        "total": 1250.00,
    },
    "PO-1002": {
        "po_id": "PO-1002",
        "vendor_id": "VEND-001",
        "currency": "USD",
        "status": "OPEN",
        "issued_at": "2024-04-30",
        "lines": [
            {
                "sku": "SKU-200",
                "description": "Hydraulic seal kit",
                "qty": 50,
                "unit_price": 20.00,
            }
        ],
        "total": 1000.00,
    },
    "PO-1003": {
        "po_id": "PO-1003",
        "vendor_id": "VEND-002",
        "currency": "USD",
        "status": "OPEN",
        "issued_at": "2024-05-01",
        "lines": [
            {
                "sku": "SKU-300",
                "description": "Precision servo motor",
                "qty": 10,
                "unit_price": 500.00,
            }
        ],
        "total": 5000.00,
    },
    "PO-1004": {
        "po_id": "PO-1004",
        "vendor_id": "VEND-001",
        "currency": "USD",
        "status": "OPEN",
        "issued_at": "2024-05-03",
        "lines": [
            {
                "sku": "SKU-400",
                "description": "Aluminium extrusion, 3m",
                "qty": 25,
                "unit_price": 40.00,
            }
        ],
        "total": 1000.00,
    },
    "PO-1005": {
        "po_id": "PO-1005",
        "vendor_id": "VEND-003",
        "currency": "USD",
        "status": "OPEN",
        "issued_at": "2024-05-04",
        "lines": [
            {
                "sku": "SKU-500",
                "description": "Fastener assortment box",
                "qty": 200,
                "unit_price": 3.00,
            }
        ],
        "total": 600.00,
    },
}

# Keyed by PO ID. PO-1004 deliberately has no receipt (MISSING_GR branch).
_GOODS_RECEIPTS: Dict[str, Dict[str, Any]] = {
    "PO-1001": {
        "gr_id": "GR-5001",
        "po_id": "PO-1001",
        "received_at": "2024-05-02",
        "received_by": "dock-01",
        "lines": [{"sku": "SKU-100", "qty_received": 100}],
    },
    "PO-1002": {
        "gr_id": "GR-5002",
        "po_id": "PO-1002",
        "received_at": "2024-05-03",
        "received_by": "dock-02",
        "lines": [{"sku": "SKU-200", "qty_received": 50}],
    },
    "PO-1003": {
        "gr_id": "GR-5003",
        "po_id": "PO-1003",
        "received_at": "2024-05-04",
        "received_by": "dock-01",
        "lines": [{"sku": "SKU-300", "qty_received": 10}],
    },
    "PO-1005": {
        "gr_id": "GR-5005",
        "po_id": "PO-1005",
        "received_at": "2024-05-06",
        "received_by": "dock-03",
        "lines": [{"sku": "SKU-500", "qty_received": 150}],
    },
}

_INVOICES: Dict[str, Dict[str, Any]] = {
    "INV-2001": {
        "invoice_id": "INV-2001",
        "vendor_id": "VEND-001",
        "po_id": "PO-1001",
        "currency": "USD",
        "lines": [{"sku": "SKU-100", "qty": 100, "unit_price": 12.50}],
        "total": 1250.00,
        "status": STATE_RECEIVED,
        "created_at": "2024-05-05T09:12:00.000Z",
        "updated_at": "2024-05-05T09:12:00.000Z",
        "state_history": [],
        "payout_id": None,
    },
    "INV-2002": {
        "invoice_id": "INV-2002",
        "vendor_id": "VEND-001",
        "po_id": "PO-1002",
        "currency": "USD",
        "lines": [{"sku": "SKU-200", "qty": 50, "unit_price": 22.00}],
        "total": 1100.00,
        "status": STATE_RECEIVED,
        "created_at": "2024-05-05T09:20:00.000Z",
        "updated_at": "2024-05-05T09:20:00.000Z",
        "state_history": [],
        "payout_id": None,
    },
    "INV-2003": {
        "invoice_id": "INV-2003",
        "vendor_id": "VEND-002",
        "po_id": "PO-1003",
        "currency": "USD",
        "lines": [{"sku": "SKU-300", "qty": 10, "unit_price": 500.00}],
        "total": 5000.00,
        "status": STATE_RECEIVED,
        "created_at": "2024-05-05T10:02:00.000Z",
        "updated_at": "2024-05-05T10:02:00.000Z",
        "state_history": [],
        "payout_id": None,
    },
    "INV-2004": {
        "invoice_id": "INV-2004",
        "vendor_id": "VEND-001",
        "po_id": "PO-1004",
        "currency": "USD",
        "lines": [{"sku": "SKU-400", "qty": 25, "unit_price": 40.00}],
        "total": 1000.00,
        "status": STATE_RECEIVED,
        "created_at": "2024-05-06T08:45:00.000Z",
        "updated_at": "2024-05-06T08:45:00.000Z",
        "state_history": [],
        "payout_id": None,
    },
    "INV-2005": {
        "invoice_id": "INV-2005",
        "vendor_id": "VEND-003",
        "po_id": "PO-1005",
        "currency": "USD",
        "lines": [{"sku": "SKU-500", "qty": 200, "unit_price": 3.00}],
        "total": 600.00,
        "status": STATE_RECEIVED,
        "created_at": "2024-05-07T11:30:00.000Z",
        "updated_at": "2024-05-07T11:30:00.000Z",
        "state_history": [],
        "payout_id": None,
    },
}

_APPROVALS: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Audit log: hash-chained, single-writer, fsync'd
# ---------------------------------------------------------------------------

_AUDIT_SEQ: int = 0
_AUDIT_LAST_HASH: str = GENESIS_HASH


def _utc_now() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and a Z suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(obj: Any) -> str:
    """Deterministic serialisation used for hashing."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _compute_hash(prev_hash: str, body: Dict[str, Any]) -> str:
    """SHA-256 over ``prev_hash`` concatenated with the canonical record body."""
    return hashlib.sha256((prev_hash + _canonical_json(body)).encode("utf-8")).hexdigest()


def _normalize_correlation_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or "UNSPECIFIED"


def _append_audit(
    event_type: str, payload: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
    """Append one tamper-evident record to the audit log.

    This is the ONLY writer of ``audit_log.jsonl``. It is serialised by
    ``_AUDIT_LOCK`` and fsync'd before returning, so a crash cannot leave a
    half-written record that silently breaks the chain.
    """
    global _AUDIT_SEQ, _AUDIT_LAST_HASH

    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    correlation_id = _normalize_correlation_id(correlation_id)

    with _AUDIT_LOCK:
        seq = _AUDIT_SEQ + 1
        ts = _utc_now()
        body: Dict[str, Any] = {
            "seq": seq,
            "ts": ts,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "payload": payload,
        }
        prev_hash = _AUDIT_LAST_HASH
        record_hash = _compute_hash(prev_hash, body)

        record = dict(body)
        record["prev_hash"] = prev_hash
        record["hash"] = record_hash

        line = _canonical_json(record)

        directory = os.path.dirname(os.path.abspath(AUDIT_LOG_PATH))
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        _AUDIT_SEQ = seq
        _AUDIT_LAST_HASH = record_hash

        return {"seq": seq, "ts": ts, "hash": record_hash, "prev_hash": prev_hash}


def _recover_audit_tail() -> None:
    """Resume the hash chain from the last record on disk.

    A corrupt tail is fatal: continuing would produce a chain that cannot be
    verified, which defeats the purpose of the log.
    """
    global _AUDIT_SEQ, _AUDIT_LAST_HASH

    if not os.path.exists(AUDIT_LOG_PATH):
        return

    last_line: Optional[str] = None
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last_line = line.strip()

    if last_line is None:
        return

    try:
        record = json.loads(last_line)
        seq = int(record["seq"])
        digest = str(record["hash"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "audit_log.jsonl tail is unreadable (%s); refusing to continue the "
            "hash chain" % exc
        ) from exc

    _AUDIT_SEQ = seq
    _AUDIT_LAST_HASH = digest


_recover_audit_tail()


def verify_audit_chain() -> Dict[str, Any]:
    """Recompute the whole chain and report the first divergence, if any."""
    with _AUDIT_LOCK:
        if not os.path.exists(AUDIT_LOG_PATH):
            return {"ok": True, "records": 0, "message": "no audit log yet"}

        prev_hash = GENESIS_HASH
        expected_seq = 1
        count = 0

        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    return {
                        "ok": False,
                        "records": count,
                        "error": "UNPARSEABLE_RECORD",
                        "line": lineno,
                    }

                body = {
                    key: record.get(key)
                    for key in ("seq", "ts", "event_type", "correlation_id", "payload")
                }

                if record.get("seq") != expected_seq:
                    return {
                        "ok": False,
                        "records": count,
                        "error": "SEQ_GAP",
                        "line": lineno,
                        "expected_seq": expected_seq,
                    }
                if record.get("prev_hash") != prev_hash:
                    return {
                        "ok": False,
                        "records": count,
                        "error": "PREV_HASH_MISMATCH",
                        "line": lineno,
                    }
                if _compute_hash(prev_hash, body) != record.get("hash"):
                    return {
                        "ok": False,
                        "records": count,
                        "error": "HASH_MISMATCH",
                        "line": lineno,
                    }

                prev_hash = record["hash"]
                expected_seq += 1
                count += 1

        return {"ok": True, "records": count, "head_hash": prev_hash}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _money_eq(left: Any, right: Any, tolerance: float = MONEY_TOLERANCE) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _fail(tool: str, error: str, correlation_id: str, **extra: Any) -> Dict[str, Any]:
    """Build a structured failure and record it as an auditable decision."""
    payload: Dict[str, Any] = {"tool": tool, "error": error}
    payload.update(extra)
    _append_audit("TOOL_CALL_REJECTED", payload, correlation_id)

    result: Dict[str, Any] = {
        "ok": False,
        "error": error,
        "tool": tool,
        "correlation_id": correlation_id,
    }
    result.update(extra)
    return result


def _find_pending_approval(invoice_id: str) -> Optional[Dict[str, Any]]:
    for approval in _APPROVALS.values():
        if (
            approval.get("invoice_id") == invoice_id
            and approval.get("status") == APPROVAL_PENDING
        ):
            return approval
    return None


def _transition(
    invoice_id: str,
    new_state: str,
    *,
    reason: str,
    correlation_id: str,
    actor: str = "system",
) -> Dict[str, Any]:
    """Guard and apply a single invoice state transition.

    Returns a structured result; never raises. Illegal transitions are
    recorded as ``STATE_TRANSITION_DENIED`` so attempted bypasses are visible
    in the audit trail.
    """
    correlation_id = _normalize_correlation_id(correlation_id)

    with _STATE_LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {
                "ok": False,
                "error": "INVOICE_NOT_FOUND",
                "invoice_id": invoice_id,
                "correlation_id": correlation_id,
            }

        if new_state not in ALL_STATES:
            return {
                "ok": False,
                "error": "UNKNOWN_STATE",
                "state": new_state,
                "invoice_id": invoice_id,
                "correlation_id": correlation_id,
            }

        current = invoice.get("status")

        if current == new_state:
            return {
                "ok": True,
                "noop": True,
                "from": current,
                "to": new_state,
                "invoice_id": invoice_id,
                "correlation_id": correlation_id,
            }

        allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            _append_audit(
                "STATE_TRANSITION_DENIED",
                {
                    "invoice_id": invoice_id,
                    "from": current,
                    "to": new_state,
                    "reason": reason,
                    "actor": actor,
                },
                correlation_id,
            )
            return {
                "ok": False,
                "error": "ILLEGAL_TRANSITION",
                "from": current,
                "to": new_state,
                "invoice_id": invoice_id,
                "correlation_id": correlation_id,
            }

        timestamp = _utc_now()
        invoice["status"] = new_state
        invoice["updated_at"] = timestamp
        invoice.setdefault("state_history", []).append(
            {
                "from": current,
                "to": new_state,
                "at": timestamp,
                "reason": reason,
                "actor": actor,
                "correlation_id": correlation_id,
            }
        )

        _append_audit(
            "STATE_TRANSITION",
            {
                "invoice_id": invoice_id,
                "from": current,
                "to": new_state,
                "reason": reason,
                "actor": actor,
            },
            correlation_id,
        )

        return {
            "ok": True,
            "noop": False,
            "from": current,
            "to": new_state,
            "invoice_id": invoice_id,
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------------------
# Public, agent-allowlisted tools
# ---------------------------------------------------------------------------


def lookup_vendor(vendor_id: str) -> Dict[str, Any]:
    """Return a vendor record plus a payout-eligibility verdict.

    Read-only. Returns a copy so the orchestrator cannot mutate the store.
    """
    if not isinstance(vendor_id, str) or not vendor_id.strip():
        return {"ok": False, "error": "INVALID_VENDOR_ID", "vendor_id": vendor_id}

    vendor_id = vendor_id.strip()

    with _STATE_LOCK:
        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return {
                "ok": False,
                "error": "VENDOR_NOT_FOUND",
                "vendor_id": vendor_id,
            }

        record = dict(vendor)

    eligible = record.get("status") == "ACTIVE" and record.get("risk_tier") != "HIGH"

    block_reason: Optional[str] = None
    if not eligible:
        if record.get("status") != "ACTIVE":
            block_reason = "VENDOR_INACTIVE"
        else:
            block_reason = "VENDOR_HIGH_RISK"

    return {
        "ok": True,
        "vendor": record,
        "eligible_for_payout": eligible,
        "block_reason": block_reason,
    }


def run_three_way_match(
    invoice_id: str, po_id: str, correlation_id: str = ""
) -> Dict[str, Any]:
    """Deterministically compare Purchase Order, Goods Receipt and Invoice.

    Pure with respect to its inputs: it reads the stores, computes a verdict,
    and applies the resulting ``RECEIVED -> MATCHED | EXCEPTION`` transition.
    It never raises for business failures.

    ``correlation_id`` is optional so the required positional signature is
    unchanged; the agent dispatch wrapper injects it.
    """
    correlation_id = _normalize_correlation_id(correlation_id)
    tool = "run_three_way_match"

    if not isinstance(invoice_id, str) or not invoice_id.strip():
        return _fail(tool, "INVALID_INVOICE_ID", correlation_id, invoice_id=invoice_id)
    if not isinstance(po_id, str) or not po_id.strip():
        return _fail(tool, "INVALID_PO_ID", correlation_id, po_id=po_id)

    invoice_id = invoice_id.strip()
    po_id = po_id.strip()

    with _STATE_LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return _fail(tool, "INVOICE_NOT_FOUND", correlation_id, invoice_id=invoice_id)

        po = _POS.get(po_id)
        if po is None:
            return _fail(tool, "PO_NOT_FOUND", correlation_id, po_id=po_id)

        if invoice.get("status") not in (
            STATE_RECEIVED,
            STATE_MATCHED,
            STATE_EXCEPTION,
        ):
            return _fail(
                tool,
                "INVALID_STATE_FOR_MATCH",
                correlation_id,
                invoice_id=invoice_id,
                status=invoice.get("status"),
            )

        codes: List[str] = []

        # --- Document linkage -------------------------------------------
        if invoice.get("po_id") != po_id:
            codes.append("PO_MISMATCH")
        if invoice.get("vendor_id") != po.get("vendor_id"):
            codes.append("VENDOR_MISMATCH")
        if invoice.get("currency") != po.get("currency"):
            codes.append("CURRENCY_MISMATCH")

        goods_receipt = _GOODS_RECEIPTS.get(po_id)
        if goods_receipt is None:
            codes.append("MISSING_GR")

        # --- Line-level comparison --------------------------------------
        po_lines = {line["sku"]: line for line in po.get("lines", [])}
        invoice_lines = {line["sku"]: line for line in invoice.get("lines", [])}
        gr_lines = {
            line["sku"]: line for line in (goods_receipt or {}).get("lines", [])
        }

        line_results: List[Dict[str, Any]] = []
        for sku in sorted(set(po_lines) | set(invoice_lines)):
            po_line = po_lines.get(sku)
            invoice_line = invoice_lines.get(sku)
            gr_line = gr_lines.get(sku)

            issues: List[str] = []

            if po_line is None:
                issues.append("LINE_NOT_ON_PO")
            if invoice_line is None:
                issues.append("LINE_MISSING_FROM_INVOICE")

            if po_line is not None and invoice_line is not None:
                if not _money_eq(invoice_line.get("qty"), po_line.get("qty")):
                    issues.append("QTY_MISMATCH")
                if not _money_eq(
                    invoice_line.get("unit_price"), po_line.get("unit_price")
                ):
                    issues.append("PRICE_MISMATCH")

            if goods_receipt is not None:
                if gr_line is None:
                    issues.append("GR_LINE_MISSING")
                elif invoice_line is not None and not _money_eq(
                    gr_line.get("qty_received"), invoice_line.get("qty")
                ):
                    issues.append("GR_QTY_MISMATCH")

            codes.extend(issues)
            line_results.append(
                {
                    "sku": sku,
                    "po_qty": po_line.get("qty") if po_line else None,
                    "po_unit_price": po_line.get("unit_price") if po_line else None,
                    "invoice_qty": invoice_line.get("qty") if invoice_line else None,
                    "invoice_unit_price": (
                        invoice_line.get("unit_price") if invoice_line else None
                    ),
                    "gr_qty_received": (
                        gr_line.get("qty_received") if gr_line else None
                    ),
                    "issues": issues,
                }
            )

        # --- Totals ------------------------------------------------------
        invoice_total = round(float(invoice.get("total", 0.0)), 2)
        po_total = round(float(po.get("total", 0.0)), 2)
        invoice_line_sum = round(
            sum(
                float(line.get("qty", 0)) * float(line.get("unit_price", 0))
                for line in invoice.get("lines", [])
            ),
            2,
        )

        if not _money_eq(invoice_total, invoice_line_sum):
            codes.append("INVOICE_TOTAL_MISMATCH")
        if not _money_eq(invoice_total, po_total):
            codes.append("TOTAL_MISMATCH")

        codes = _dedupe(codes)
        target_state = STATE_MATCHED if not codes else STATE_EXCEPTION

        transition = _transition(
            invoice_id,
            target_state,
            reason="THREE_WAY_MATCH",
            correlation_id=correlation_id,
            actor=tool,
        )
        if not transition.get("ok"):
            return _fail(
                tool,
                transition.get("error", "TRANSITION_FAILED"),
                correlation_id,
                invoice_id=invoice_id,
                from_state=transition.get("from"),
                to_state=transition.get("to"),
            )

        result: Dict[str, Any] = {
            "ok": True,
            "tool": tool,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "gr_id": goods_receipt.get("gr_id") if goods_receipt else None,
            "status": target_state,
            "matched": not codes,
            "mismatch_codes": codes,
            "lines": line_results,
            "totals": {
                "invoice_total": invoice_total,
                "invoice_line_sum": invoice_line_sum,
                "po_total": po_total,
                "currency": invoice.get("currency"),
            },
            "correlation_id": correlation_id,
        }

        _append_audit(
            "THREE_WAY_MATCH_COMPLETED",
            {
                "invoice_id": invoice_id,
                "po_id": po_id,
                "gr_id": result["gr_id"],
                "status": target_state,
                "mismatch_codes": codes,
                "invoice_total": invoice_total,
                "po_total": po_total,
            },
            correlation_id,
        )

        return result


def request_payout_approval(
    vendor_id: str,
    amount: float,
    po_id: str,
    invoice_id: str,
    correlation_id: str,
) -> Dict[str, Any]:
    """Open a human approval gate for a payout. Does NOT move money.

    On success the invoice moves to ``PENDING_HUMAN_APPROVAL`` and an approval
    record is created. Settlement requires ``_execute_payout`` from the app
    layer with a named human approver.
    """
    correlation_id = _normalize_correlation_id(correlation_id)
    tool = "request_payout_approval"

    for name, value in (
        ("vendor_id", vendor_id),
        ("po_id", po_id),
        ("invoice_id", invoice_id),
    ):
        if not isinstance(value, str) or not value.strip():
            return _fail(tool, "INVALID_%s" % name.upper(), correlation_id)

    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return _fail(tool, "INVALID_AMOUNT", correlation_id, amount=amount)

    vendor_id = vendor_id.strip()
    po_id = po_id.strip()
    invoice_id = invoice_id.strip()
    amount = round(float(amount), 2)

    with _STATE_LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return _fail(tool, "INVOICE_NOT_FOUND", correlation_id, invoice_id=invoice_id)

        po = _POS.get(po_id)
        if po is None:
            return _fail(tool, "PO_NOT_FOUND", correlation_id, po_id=po_id)

        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return _fail(tool, "VENDOR_NOT_FOUND", correlation_id, vendor_id=vendor_id)

        if invoice.get("vendor_id") != vendor_id:
            return _fail(
                tool,
                "VENDOR_MISMATCH",
                correlation_id,
                invoice_id=invoice_id,
                expected=invoice.get("vendor_id"),
                received=vendor_id,
            )

        if invoice.get("po_id") != po_id:
            return _fail(
                tool,
                "PO_MISMATCH",
                correlation_id,
                invoice_id=invoice_id,
                expected=invoice.get("po_id"),
                received=po_id,
            )

        if not _money_eq(amount, invoice.get("total")):
            return _fail(
                tool,
                "AMOUNT_MISMATCH",
                correlation_id,
                invoice_id=invoice_id,
                requested_amount=amount,
                invoice_total=invoice.get("total"),
            )

        if amount > MAX_APPROVAL_AMOUNT:
            return _fail(
                tool,
                "AMOUNT_EXCEEDS_POLICY_LIMIT",
                correlation_id,
                invoice_id=invoice_id,
                requested_amount=amount,
                limit=MAX_APPROVAL_AMOUNT,
            )

        # --- Vendor eligibility: terminal branch -------------------------
        if vendor.get("status") != "ACTIVE" or vendor.get("risk_tier") == "HIGH":
            block_reason = (
                "VENDOR_INACTIVE"
                if vendor.get("status") != "ACTIVE"
                else "VENDOR_HIGH_RISK"
            )
            transition = _transition(
                invoice_id,
                STATE_REJECTED,
                reason=block_reason,
                correlation_id=correlation_id,
                actor=tool,
            )
            _append_audit(
                "PAYOUT_APPROVAL_DENIED",
                {
                    "invoice_id": invoice_id,
                    "vendor_id": vendor_id,
                    "amount": amount,
                    "reason": block_reason,
                    "transition_ok": transition.get("ok"),
                },
                correlation_id,
            )
            return {
                "ok": False,
                "error": block_reason,
                "tool": tool,
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "status": (
                    transition.get("to")
                    if transition.get("ok")
                    else invoice.get("status")
                ),
                "correlation_id": correlation_id,
            }

        # --- Idempotency: an open gate is returned, not duplicated -------
        if invoice.get("status") == STATE_PENDING_HUMAN_APPROVAL:
            existing = _find_pending_approval(invoice_id)
            if existing is not None:
                return {
                    "ok": True,
                    "idempotent": True,
                    "tool": tool,
                    "approval_id": existing["approval_id"],
                    "invoice_id": invoice_id,
                    "status": STATE_PENDING_HUMAN_APPROVAL,
                    "amount": existing["amount"],
                    "currency": existing["currency"],
                    "requires_human_approval": True,
                    "correlation_id": correlation_id,
                }
            return _fail(
                tool,
                "APPROVAL_STATE_INCONSISTENT",
                correlation_id,
                invoice_id=invoice_id,
            )

        if invoice.get("status") != STATE_MATCHED:
            return _fail(
                tool,
                "INVALID_STATE_FOR_APPROVAL",
                correlation_id,
                invoice_id=invoice_id,
                status=invoice.get("status"),
            )

        approval_id = "APR-" + uuid.uuid4().hex[:12].upper()
        requested_at = _utc_now()

        approval: Dict[str, Any] = {
            "approval_id": approval_id,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "vendor_id": vendor_id,
            "amount": amount,
            "currency": invoice.get("currency"),
            "status": APPROVAL_PENDING,
            "requested_at": requested_at,
            "requested_by": "agent",
            "correlation_id": correlation_id,
            "decided_at": None,
            "decided_by": None,
            "payout_id": None,
        }
        _APPROVALS[approval_id] = approval

        transition = _transition(
            invoice_id,
            STATE_PENDING_HUMAN_APPROVAL,
            reason="PAYOUT_APPROVAL_REQUESTED",
            correlation_id=correlation_id,
            actor=tool,
        )
        if not transition.get("ok"):
            del _APPROVALS[approval_id]
            return _fail(
                tool,
                transition.get("error", "TRANSITION_FAILED"),
                correlation_id,
                invoice_id=invoice_id,
            )

        _append_audit(
            "PAYOUT_APPROVAL_REQUESTED",
            {
                "approval_id": approval_id,
                "invoice_id": invoice_id,
                "po_id": po_id,
                "vendor_id": vendor_id,
                "amount": amount,
                "currency": approval["currency"],
            },
            correlation_id,
        )

        return {
            "ok": True,
            "idempotent": False,
            "tool": tool,
            "approval_id": approval_id,
            "invoice_id": invoice_id,
            "po_id": po_id,
            "vendor_id": vendor_id,
            "amount": amount,
            "currency": approval["currency"],
            "status": STATE_PENDING_HUMAN_APPROVAL,
            "requires_human_approval": True,
            "message": (
                "Payout is gated. A human approver must call the settlement "
                "endpoint; no funds have moved."
            ),
            "correlation_id": correlation_id,
        }


def log_audit_event(
    event_type: str, payload: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
    """Record a semantic event (agent reasoning, decisions, notes)."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "INVALID_PAYLOAD"}

    correlation_id = _normalize_correlation_id(correlation_id)

    try:
        receipt = _append_audit(event_type, payload, correlation_id)
    except (ValueError, OSError) as exc:
        return {
            "ok": False,
            "error": "AUDIT_WRITE_FAILED",
            "detail": str(exc),
            "correlation_id": correlation_id,
        }

    return {
        "ok": True,
        "tool": "log_audit_event",
        "seq": receipt["seq"],
        "ts": receipt["ts"],
        "hash": receipt["hash"],
        "prev_hash": receipt["prev_hash"],
        "correlation_id": correlation_id,
    }


# ---------------------------------------------------------------------------
# App-layer intake - NOT allowlisted, application layer only
# ---------------------------------------------------------------------------


def ingest_invoice(
    invoice_id: str,
    vendor_id: str,
    po_id: str,
    lines: List[Dict[str, Any]],
    total: float,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Create an invoice in ``RECEIVED``. App layer only; NOT allowlisted.

    The agent must never be able to conjure an invoice into existence, so this
    function is deliberately absent from ``agent.ALLOWED_TOOLS``. It validates
    the payload, refuses duplicate IDs, checks that the vendor and PO exist and
    agree with each other, then writes the record and an ``INVOICE_RECEIVED``
    audit event.
    """
    correlation_id = _normalize_correlation_id(correlation_id)
    tool = "ingest_invoice"

    for name, value in (
        ("invoice_id", invoice_id),
        ("vendor_id", vendor_id),
        ("po_id", po_id),
    ):
        if not isinstance(value, str) or not value.strip():
            return _fail(tool, "INVALID_%s" % name.upper(), correlation_id)

    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return _fail(tool, "INVALID_TOTAL", correlation_id, total=total)

    if not isinstance(lines, list) or not lines:
        return _fail(tool, "INVALID_LINES", correlation_id)

    normalized_lines: List[Dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            return _fail(tool, "INVALID_LINES", correlation_id)

        sku = line.get("sku")
        qty = line.get("qty")
        unit_price = line.get("unit_price")

        if not isinstance(sku, str) or not sku.strip():
            return _fail(tool, "INVALID_LINE_SKU", correlation_id)
        if isinstance(qty, bool) or not isinstance(qty, (int, float)):
            return _fail(tool, "INVALID_LINE_QTY", correlation_id, sku=sku)
        if isinstance(unit_price, bool) or not isinstance(unit_price, (int, float)):
            return _fail(tool, "INVALID_LINE_UNIT_PRICE", correlation_id, sku=sku)

        normalized_lines.append(
            {
                "sku": sku.strip(),
                "qty": qty,
                "unit_price": round(float(unit_price), 2),
            }
        )

    invoice_id = invoice_id.strip()
    vendor_id = vendor_id.strip()
    po_id = po_id.strip()
    total = round(float(total), 2)

    with _STATE_LOCK:
        if invoice_id in _INVOICES:
            return _fail(
                tool, "DUPLICATE_INVOICE_ID", correlation_id, invoice_id=invoice_id
            )

        vendor = _VENDORS.get(vendor_id)
        if vendor is None:
            return _fail(tool, "VENDOR_NOT_FOUND", correlation_id, vendor_id=vendor_id)

        po = _POS.get(po_id)
        if po is None:
            return _fail(tool, "PO_NOT_FOUND", correlation_id, po_id=po_id)

        if po.get("vendor_id") != vendor_id:
            return _fail(
                tool,
                "VENDOR_PO_MISMATCH",
                correlation_id,
                vendor_id=vendor_id,
                po_id=po_id,
                po_vendor_id=po.get("vendor_id"),
            )

        timestamp = _utc_now()
        invoice: Dict[str, Any] = {
            "invoice_id": invoice_id,
            "vendor_id": vendor_id,
            "po_id": po_id,
            "currency": po.get("currency"),
            "lines": normalized_lines,
            "total": total,
            "status": STATE_RECEIVED,
            "created_at": timestamp,
            "updated_at": timestamp,
            "state_history": [],
            "payout_id": None,
        }
        _INVOICES[invoice_id] = invoice

        _append_audit(
            "INVOICE_RECEIVED",
            {
                "invoice_id": invoice_id,
                "vendor_id": vendor_id,
                "po_id": po_id,
                "total": total,
                "currency": invoice["currency"],
                "line_count": len(normalized_lines),
            },
            correlation_id,
        )

        return {
            "ok": True,
            "tool": tool,
            "invoice_id": invoice_id,
            "status": STATE_RECEIVED,
            "invoice": json.loads(json.dumps(invoice, default=str)),
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------------------
# Private settlement path - NOT allowlisted, app layer only
# ---------------------------------------------------------------------------


def _execute_payout(
    approval_id: str, approver: str, correlation_id: str
) -> Dict[str, Any]:
    """Settle an approved payout. Callable ONLY from the application layer.

    Guards, in order:
      1. The approver must be a named human (denylist rejects agent/system).
      2. The approval must exist and still be ``PENDING``.
      3. The invoice must be in ``PENDING_HUMAN_APPROVAL``.
      4. The approved amount must still equal the invoice total.
      5. Idempotency: a consumed approval returns the original payout rather
         than disbursing twice.
    """
    correlation_id = _normalize_correlation_id(correlation_id)
    tool = "_execute_payout"

    if not isinstance(approval_id, str) or not approval_id.strip():
        return _fail(tool, "INVALID_APPROVAL_ID", correlation_id)

    approval_id = approval_id.strip()
    approver_clean = str(approver or "").strip()

    if approver_clean.lower() in HUMAN_APPROVER_DENYLIST:
        _append_audit(
            "PAYOUT_EXECUTION_DENIED",
            {
                "approval_id": approval_id,
                "reason": "NON_HUMAN_APPROVER",
                "approver": approver_clean,
            },
            correlation_id,
        )
        return {
            "ok": False,
            "error": "NON_HUMAN_APPROVER",
            "tool": tool,
            "approval_id": approval_id,
            "correlation_id": correlation_id,
        }

    with _STATE_LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return _fail(tool, "APPROVAL_NOT_FOUND", correlation_id, approval_id=approval_id)

        invoice = _INVOICES.get(approval.get("invoice_id"))
        if invoice is None:
            return _fail(
                tool,
                "INVOICE_NOT_FOUND",
                correlation_id,
                invoice_id=approval.get("invoice_id"),
            )

        # --- Idempotency: already settled --------------------------------
        if approval.get("status") == APPROVAL_CONSUMED:
            _append_audit(
                "PAYOUT_DUPLICATE_SUPPRESSED",
                {
                    "approval_id": approval_id,
                    "invoice_id": invoice.get("invoice_id"),
                    "payout_id": approval.get("payout_id"),
                    "approver": approver_clean,
                },
                correlation_id,
            )
            return {
                "ok": True,
                "idempotent": True,
                "tool": tool,
                "approval_id": approval_id,
                "invoice_id": invoice.get("invoice_id"),
                "payout_id": approval.get("payout_id"),
                "amount": approval.get("amount"),
                "currency": approval.get("currency"),
                "status": invoice.get("status"),
                "message": "Approval already consumed; no second disbursement.",
                "correlation_id": correlation_id,
            }

        if approval.get("status") != APPROVAL_PENDING:
            return _fail(
                tool,
                "APPROVAL_NOT_PENDING",
                correlation_id,
                approval_id=approval_id,
                status=approval.get("status"),
            )

        if invoice.get("status") == STATE_PAID:
            return _fail(
                tool,
                "INVOICE_ALREADY_PAID",
                correlation_id,
                invoice_id=invoice.get("invoice_id"),
                payout_id=invoice.get("payout_id"),
            )

        if invoice.get("status") != STATE_PENDING_HUMAN_APPROVAL:
            return _fail(
                tool,
                "INVALID_STATE_FOR_PAYOUT",
                correlation_id,
                invoice_id=invoice.get("invoice_id"),
                status=invoice.get("status"),
            )

        if not _money_eq(approval.get("amount"), invoice.get("total")):
            return _fail(
                tool,
                "AMOUNT_MISMATCH",
                correlation_id,
                approval_id=approval_id,
                approved_amount=approval.get("amount"),
                invoice_total=invoice.get("total"),
            )

        payout_id = "PAY-" + uuid.uuid4().hex[:12].upper()

        transition = _transition(
            invoice["invoice_id"],
            STATE_PAID,
            reason="HUMAN_APPROVED_PAYOUT",
            correlation_id=correlation_id,
            actor=approver_clean,
        )
        if not transition.get("ok"):
            return _fail(
                tool,
                transition.get("error", "TRANSITION_FAILED"),
                correlation_id,
                invoice_id=invoice.get("invoice_id"),
            )

        decided_at = _utc_now()
        approval["status"] = APPROVAL_CONSUMED
        approval["decided_at"] = decided_at
        approval["decided_by"] = approver_clean
        approval["payout_id"] = payout_id
        invoice["payout_id"] = payout_id

        _append_audit(
            "PAYOUT_EXECUTED",
            {
                "payout_id": payout_id,
                "approval_id": approval_id,
                "invoice_id": invoice.get("invoice_id"),
                "po_id": approval.get("po_id"),
                "vendor_id": approval.get("vendor_id"),
                "amount": approval.get("amount"),
                "currency": approval.get("currency"),
                "approver": approver_clean,
                "decided_at": decided_at,
            },
            correlation_id,
        )

        return {
            "ok": True,
            "idempotent": False,
            "tool": tool,
            "payout_id": payout_id,
            "approval_id": approval_id,
            "invoice_id": invoice.get("invoice_id"),
            "amount": approval.get("amount"),
            "currency": approval.get("currency"),
            "status": STATE_PAID,
            "approver": approver_clean,
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------------------
# Read-only helpers for the application layer
# ---------------------------------------------------------------------------


def get_invoice(invoice_id: str) -> Dict[str, Any]:
    """Return a copy of an invoice record, or a structured not-found."""
    with _STATE_LOCK:
        invoice = _INVOICES.get(invoice_id)
        if invoice is None:
            return {"ok": False, "error": "INVOICE_NOT_FOUND", "invoice_id": invoice_id}
        return {"ok": True, "invoice": json.loads(json.dumps(invoice, default=str))}


def get_approval(approval_id: str) -> Dict[str, Any]:
    """Return a copy of an approval record, or a structured not-found."""
    with _STATE_LOCK:
        approval = _APPROVALS.get(approval_id)
        if approval is None:
            return {
                "ok": False,
                "error": "APPROVAL_NOT_FOUND",
                "approval_id": approval_id,
            }
        return {"ok": True, "approval": dict(approval)}
