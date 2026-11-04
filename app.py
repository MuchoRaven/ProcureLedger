"""
app.py
======
ProcureLedger - HTTP surface.

The agent is reachable only through ``/agent/run``. Human approval and payout
settlement are deliberately separate endpoints so a model can never settle a
payout on its own. ``/audit`` and ``/audit/verify`` read the on-disk chain.

Error contract
--------------
* ``tools.ToolError`` (business/integrity errors) -> structured JSON via the
  registered exception handler, with a status code derived from the error code.
* Agent client/model init failures -> HTTP 5xx carrying the agent's structured
  result, so a broken deployment is not reported as a successful run.
* Agent step-cap exhaustion -> HTTP 200 with ``ok: false``; the run completed,
  it just did not converge.

Correlation IDs
---------------
Every request is traceable. The ``X-Correlation-ID`` header is used when
present; otherwise a ``uuid4`` is generated. A non-empty ``correlation_id`` in
the JSON body still wins, for backwards compatibility with existing callers.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, confloat

import agent
import tools

app = FastAPI(title="ProcureLedger")

# Strict positive amount. ``strict=True`` rejects booleans (``True`` would
# otherwise coerce to ``1.0``) and strings, so the ``isinstance(amount, bool)``
# guard in ``tools.ingest_invoice`` can never be reached with a bool.
# ``confloat`` is used rather than ``Annotated[float, Field(...)]`` so this
# module works unchanged on both pydantic v1 and v2.
Amount = confloat(strict=True, gt=0)

# Agent errors that mean "the deployment is broken", not "the run finished".
_AGENT_FATAL_ERRORS = {"CLIENT_INIT_FAILED", "MODEL_CALL_FAILED"}

# Business error code -> HTTP status. Anything unmapped is a 400.
_TOOL_ERROR_STATUS = {
    "AUDIT_LOG_CORRUPT": 503,
    "INVOICE_NOT_FOUND": 404,
    "APPROVAL_NOT_FOUND": 404,
    "INVALID_TRANSITION": 409,
}

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    message: str
    correlation_id: str = ""


class IngestRequest(BaseModel):
    invoice_id: str
    vendor_id: str
    po_id: str
    amount: Amount
    currency: str = "USD"
    correlation_id: str = ""


class ApproveRequest(BaseModel):
    approver: str
    correlation_id: str = ""


class ExecuteRequest(BaseModel):
    correlation_id: str = ""

# ---------------------------------------------------------------------------
# Correlation ID plumbing
# ---------------------------------------------------------------------------


def correlation_id_header(
    x_correlation_id: Optional[str] = Header(
        default=None, alias="X-Correlation-ID"
    ),
) -> str:
    """Return the transport-level trace ID, or a fresh UUID if absent."""
    value = (x_correlation_id or "").strip()
    return value or uuid.uuid4().hex


def _resolve_correlation_id(header_value: str, body_value: str) -> str:
    """Pick the correlation ID for this request.

    A non-empty body value wins so existing callers keep their trace IDs; the
    header is the fallback; the dependency has already generated a UUID if
    neither was supplied, so the result is never empty.
    """
    body = (body_value or "").strip()
    if body:
        return body
    return header_value

# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(tools.ToolError)
def handle_tool_error(request: Request, exc: tools.ToolError) -> JSONResponse:
    """Turn business/integrity errors into structured JSON, never a traceback."""
    return JSONResponse(
        status_code=_TOOL_ERROR_STATUS.get(exc.code, 400),
        content={"ok": False, "error": exc.code, "detail": exc.detail},
    )

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.post("/agent/run")
def run_agent(req: RunRequest, cid: str = Depends(correlation_id_header)) -> Any:
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    try:
        result = agent.run(req.message, correlation_id=correlation_id)
    except RuntimeError as exc:
        # Defensive: agent.run reports failures in-band, but a client-init
        # failure must still surface as a 5xx rather than a silent 200.
        raise HTTPException(status_code=503, detail=str(exc))

    if not result.get("ok", False) and result.get("error") in _AGENT_FATAL_ERRORS:
        return JSONResponse(status_code=503, content=result)

    # MAX_STEPS_EXCEEDED and ordinary replies are 200 with ``ok`` in the body.
    return result


@app.get("/audit")
def read_audit() -> Dict[str, Any]:
    """Return the durable audit chain.

    Delegates to the tool layer's loader so a corrupt log raises
    ``ToolError("AUDIT_LOG_CORRUPT")`` and is rendered by the handler above as
    a structured 503, instead of an unhandled ``JSONDecodeError`` traceback.
    """
    entries = tools._load_audit_from_file()
    return {"ok": True, "entries": entries}


@app.get("/audit/verify")
def verify_audit(from_file: bool = True) -> Dict[str, Any]:
    return tools.verify_audit_chain(from_file=from_file)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> Dict[str, Any]:
    return tools.get_invoice(invoice_id)


@app.post("/invoices")
def ingest_invoice(
    req: IngestRequest, cid: str = Depends(correlation_id_header)
) -> Dict[str, Any]:
    return tools.ingest_invoice(
        req.invoice_id,
        req.vendor_id,
        req.po_id,
        req.amount,
        currency=req.currency,
        correlation_id=_resolve_correlation_id(cid, req.correlation_id),
    )


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str) -> Dict[str, Any]:
    return tools.get_approval(approval_id)


@app.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: str, req: ApproveRequest, cid: str = Depends(correlation_id_header)
) -> Dict[str, Any]:
    return tools.approve_payout(
        approval_id,
        req.approver,
        correlation_id=_resolve_correlation_id(cid, req.correlation_id),
    )


@app.post("/approvals/{approval_id}/execute")
def execute(
    approval_id: str, req: ExecuteRequest, cid: str = Depends(correlation_id_header)
) -> Dict[str, Any]:
    return tools._execute_payout(
        approval_id,
        correlation_id=_resolve_correlation_id(cid, req.correlation_id),
    )
