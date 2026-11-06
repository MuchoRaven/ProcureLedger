"""
app.py
======
ProcureLedger - HTTP surface.

The agent is reachable only through ``/agent/run``. Human approval and payout
settlement are deliberately separate endpoints so a model can never settle a
payout on its own. ``/audit`` and ``/audit/verify`` read the on-disk chain.

Procurement records (POs and requisitions) are app-layer only. ``/pos`` and
``/requisitions`` expose them over HTTP, but nothing in ``tools`` or
``decisions`` puts them in ``decisions.PROPOSAL_KINDS``, so the model cannot
reach them. The listing helpers live here rather than in ``tools`` for the
same reason: a listing needs the whole store, which is exactly the widening
``tools._snapshot_for`` refuses to do.

Authentication
--------------
Every endpoint except ``/health`` requires ``Authorization: Bearer <token>``.
Three credentials exist and they are not interchangeable:

* ``PL_AGENT_TOKEN``    - role ``agent``.    ``/agent/run``.
* ``PL_OPERATOR_TOKEN`` - role ``operator``. Invoice ingest, approval, and
  procurement mutations (create PO, record receipt, close/cancel PO).
* ``PL_EXECUTOR_TOKEN`` - role ``executor``. Payout settlement.

The split is the point: the human-approval gate is meaningless if the agent's
own credential can reach ``/approvals/{id}/approve``, and separation of duties
is meaningless if the approver's credential can also settle. Tokens are
compared with ``hmac.compare_digest`` against every candidate, so a wrong token
does not leak which prefix matched.

``preflight_auth`` runs in the lifespan hook and refuses to boot if any token
is unset or if any two are equal - a deployment that forgot to configure them
must not come up wide open, and an equal pair would silently collapse two roles
into one.

The approver recorded on ``/approvals/{id}/approve`` is the authenticated
operator's ``sub``, not the request body. A body ``approver`` that disagrees is
rejected with 403 rather than silently ignored, so a caller cannot believe it
approved as someone else. The executor recorded on
``/approvals/{id}/execute`` is the authenticated executor's ``sub``, and
``tools._execute_payout`` refuses when it equals the approver.

Error contract
--------------
Every failure - auth, validation, business, integrity - returns one envelope::

    {"ok": false, "error": CODE, "detail": str, "correlation_id": str}

``detail`` is blanked on 5xx unless ``config.PL_DEBUG`` is set, because
``tools`` puts raw ``OSError`` text in ``ToolError.detail`` and that text
contains host paths. The same redaction is applied to the ``detail`` field
inside ``/audit/verify``'s 200 response, which reports an unreadable log
in-band.

Status mapping:

* ``AUTH_MISSING`` / ``AUTH_INVALID`` -> 401, ``AUTH_FORBIDDEN`` -> 403.
* ``tools.ToolError`` -> ``_TOOL_ERROR_STATUS``, default 400.
* Tool results that come back ``ok: false`` -> the same mapping, so
  ``GET /invoices/INV-NOPE`` is a 404 rather than a 200 carrying an error.
* Agent client/model init failures -> 503.
* Agent step-cap exhaustion -> 200 with ``ok: false``; the run completed.

Correlation IDs
---------------
``X-Correlation-ID`` is echoed on every response, including errors. The
inbound header is used when present, otherwise a ``uuid4`` is generated once
in middleware and stored on ``request.state``, so the dependency, the handlers
and the body all agree on one value. A non-empty ``correlation_id`` in the JSON
body still wins, for backwards compatibility with existing callers.

Startup
-------
``sandbox.preflight`` and ``preflight_auth`` run in the lifespan hook. The hook
also takes an exclusive ``fcntl.flock`` on ``<AUDIT_LOG_PATH>.lock``: the audit
chain is process-local, so a second worker would fork it. Run with
``--workers 1``.
"""

from __future__ import annotations

import fcntl
import hmac
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, confloat

import agent
import config
import sandbox
import tools

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

ROLE_AGENT = "agent"
ROLE_OPERATOR = "operator"
ROLE_EXECUTOR = "executor"


class AuthError(Exception):
    """Raised by the auth dependencies; rendered by ``handle_auth_error``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _principals() -> Dict[str, tools.Principal]:
    """Map token -> Principal.

    Rebuilt per call rather than cached at import so a test can monkeypatch
    ``config`` without reloading the module. Empty tokens are omitted, which is
    why ``preflight_auth`` must run before the app serves traffic.
    """
    table: Dict[str, tools.Principal] = {}
    agent_token = (config.PL_AGENT_TOKEN or "").strip()
    operator_token = (config.PL_OPERATOR_TOKEN or "").strip()
    executor_token = (config.PL_EXECUTOR_TOKEN or "").strip()

    if agent_token:
        table[agent_token] = tools.Principal(
            sub="agent", roles=frozenset({ROLE_AGENT})
        )
    if operator_token:
        name = (config.PL_OPERATOR_NAME or "operator").strip() or "operator"
        table[operator_token] = tools.Principal(
            sub=name, roles=frozenset({ROLE_OPERATOR})
        )
    if executor_token:
        name = (config.PL_EXECUTOR_NAME or "executor").strip() or "executor"
        table[executor_token] = tools.Principal(
            sub=name, roles=frozenset({ROLE_EXECUTOR})
        )
    return table


def preflight_auth() -> None:
    """Fail fast if the deployment has no usable credentials.

    Mirrors ``sandbox.preflight``: a host that cannot authenticate must fail at
    boot, not serve every request as an anonymous 401. The pairwise equality
    check matters because ``_principals`` is a dict - an identical pair would
    let one entry overwrite another, silently collapsing two roles into one.
    """
    agent_token = (config.PL_AGENT_TOKEN or "").strip()
    operator_token = (config.PL_OPERATOR_TOKEN or "").strip()
    executor_token = (config.PL_EXECUTOR_TOKEN or "").strip()

    pairs = [
        ("PL_AGENT_TOKEN", agent_token),
        ("PL_OPERATOR_TOKEN", operator_token),
        ("PL_EXECUTOR_TOKEN", executor_token),
    ]
    missing = [name for name, value in pairs if not value]
    if missing:
        raise RuntimeError(
            "authentication unavailable: missing " + ", ".join(missing)
        )
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            if hmac.compare_digest(pairs[i][1], pairs[j][1]):
                raise RuntimeError(
                    f"{pairs[i][0]} and {pairs[j][0]} must differ; "
                    "an identical pair would collapse two roles into one"
                )


def _authenticate(authorization: Optional[str]) -> tools.Principal:
    """Resolve a bearer token to a Principal, or raise ``AuthError``.

    Every candidate is compared, with no early exit, so the response time does
    not reveal how much of a guessed token was correct. Both sides are encoded
    to bytes because ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII
    ``str``.
    """
    if not authorization or not authorization.strip():
        raise AuthError("AUTH_MISSING", "Authorization header is required")

    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer" or not token.strip():
        raise AuthError("AUTH_INVALID", "expected 'Authorization: Bearer <token>'")

    token_bytes = token.strip().encode("utf-8")
    matched: Optional[tools.Principal] = None
    for candidate, principal in _principals().items():
        if hmac.compare_digest(candidate.encode("utf-8"), token_bytes):
            matched = principal

    if matched is None:
        raise AuthError("AUTH_INVALID", "token not recognised")
    return matched


def require_agent(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> tools.Principal:
    """Allow only the agent credential."""
    principal = _authenticate(authorization)
    if not principal.has_role(ROLE_AGENT):
        raise AuthError("AUTH_FORBIDDEN", "this endpoint requires the agent credential")
    return principal


def require_operator(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> tools.Principal:
    """Allow only the operator credential."""
    principal = _authenticate(authorization)
    if not principal.has_role(ROLE_OPERATOR):
        raise AuthError(
            "AUTH_FORBIDDEN", "this endpoint requires the operator credential"
        )
    return principal


def require_executor(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> tools.Principal:
    """Allow only the executor credential."""
    principal = _authenticate(authorization)
    if not principal.has_role(ROLE_EXECUTOR):
        raise AuthError(
            "AUTH_FORBIDDEN", "this endpoint requires the executor credential"
        )
    return principal


def require_any(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> tools.Principal:
    """Allow any credential. Used for read-only endpoints."""
    return _authenticate(authorization)


# ---------------------------------------------------------------------------
# Single-worker lock
# ---------------------------------------------------------------------------


def _acquire_single_worker_lock() -> Optional[Any]:
    """Take an exclusive lock on ``<AUDIT_LOG_PATH>.lock``.

    The audit chain is process-local, so two workers would fork it. ``flock``
    is advisory but sufficient here: every ProcureLedger process takes the same
    lock at boot, so a second worker fails fast instead of silently corrupting
    the chain. Returns the open handle (to be released on shutdown), or
    ``None`` when no audit log is configured.
    """
    path = config.AUDIT_LOG_PATH
    if not path:
        return None
    lock_path = path + ".lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            "another ProcureLedger worker holds the audit lock; "
            "run with --workers 1"
        )
    return handle


def _release_single_worker_lock(handle: Optional[Any]) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Refuse to serve if this host cannot sandbox, authenticate, or is a
    second worker."""
    sandbox.preflight()
    preflight_auth()
    lock_handle = _acquire_single_worker_lock()
    try:
        yield
    finally:
        _release_single_worker_lock(lock_handle)


app = FastAPI(title="ProcureLedger", lifespan=lifespan)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """Assign one correlation ID per request and echo it on the response.

    Stored on ``request.state`` so ``correlation_id_header``, the exception
    handlers and the response header all read the same value. Without this the
    dependency generated its own UUID and the body could disagree with the
    wire.
    """
    incoming = (request.headers.get("X-Correlation-ID") or "").strip()
    request.state.correlation_id = incoming or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    return response


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
    "AUDIT_LOG_UNREADABLE": 503,
    "AUDIT_WRITE_FAILED": 503,
    "INVOICE_NOT_FOUND": 404,
    "APPROVAL_NOT_FOUND": 404,
    "VENDOR_NOT_FOUND": 404,
    "PO_NOT_FOUND": 404,
    "REQUISITION_NOT_FOUND": 404,
    "INVALID_TRANSITION": 409,
    "INVOICE_EXISTS": 409,
    "INVOICE_NOT_MATCHED": 409,
    "INVOICE_NOT_PENDING_APPROVAL": 409,
    "INVOICE_NOT_APPROVED": 409,
    "INVALID_STATE": 409,
    "APPROVAL_NOT_PENDING": 409,
    "APPROVAL_NOT_APPROVED": 409,
    "APPROVAL_ALREADY_PENDING": 409,
    "SEPARATION_OF_DUTIES": 409,
    "VENDOR_INACTIVE": 409,
    "VENDOR_HIGH_RISK": 409,
    "VENDOR_MISMATCH": 409,
    "PO_INVOICE_MISMATCH": 409,
    "PO_EXISTS": 409,
    "PO_NOT_OPEN": 409,
    "RECEIPT_EXCEEDS_PO": 409,
    "REQUISITION_NOT_OPEN": 409,
    "AMOUNT_MISMATCH": 409,
}

# Status -> code for ``HTTPException`` raised by this module or by FastAPI.
_HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_INVALID",
    403: "AUTH_FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    503: "SERVICE_UNAVAILABLE",
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
    # Deprecated: the approver is taken from the authenticated principal. Kept
    # so existing callers do not break; a value that disagrees with the
    # principal is rejected rather than ignored.
    approver: str = ""
    correlation_id: str = ""


class ExecuteRequest(BaseModel):
    correlation_id: str = ""


class CreatePORequest(BaseModel):
    vendor_id: str
    amount: Amount
    currency: str = "USD"
    expected_delivery: str = ""
    requisition_id: str = ""
    correlation_id: str = ""


class CreateRequisitionRequest(BaseModel):
    description: str
    quantity: float
    amount: Amount
    vendor_id: str = ""
    currency: str = "USD"
    correlation_id: str = ""


class ReceiptRequest(BaseModel):
    amount: Amount
    correlation_id: str = ""


class ActionRequest(BaseModel):
    """Body for a state-change endpoint that takes no business fields."""

    correlation_id: str = ""

# ---------------------------------------------------------------------------
# Correlation ID plumbing
# ---------------------------------------------------------------------------


def correlation_id_header(
    request: Request,
    x_correlation_id: Optional[str] = Header(
        default=None, alias="X-Correlation-ID"
    ),
) -> str:
    """Return this request's trace ID.

    Prefers the value the middleware already assigned so the body and the
    response header cannot disagree. Falls back to the inbound header, then to
    a fresh UUID, so the dependency is still usable outside the middleware
    (for example when called directly in a test).
    """
    state_value = getattr(request.state, "correlation_id", "")
    if state_value:
        return state_value
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


def _cid(request: Request) -> str:
    """Correlation ID for an error response, tolerating a missing state."""
    return getattr(request.state, "correlation_id", "") or ""


def _redact(status: int, detail: Any) -> str:
    """Blank ``detail`` on 5xx unless ``PL_DEBUG`` is set.

    ``tools`` puts raw ``OSError`` text in ``ToolError.detail``; that text
    contains host paths and must not reach a client. 4xx detail is
    caller-actionable and is passed through.
    """
    text = "" if detail is None else str(detail)
    if status >= 500 and not config.PL_DEBUG:
        return ""
    return text


def _error_body(code: str, detail: str, correlation_id: str) -> Dict[str, Any]:
    """The single failure envelope used by every handler below."""
    return {
        "ok": False,
        "error": code,
        "detail": detail,
        "correlation_id": correlation_id,
    }


def _respond(result: Any, correlation_id: str) -> Any:
    """Render a tool result, mapping ``ok: false`` to a real HTTP status.

    Without this, ``GET /invoices/INV-NOPE`` returned HTTP 200 carrying
    ``{"ok": false, "error": "INVOICE_NOT_FOUND"}`` - a 404 reported as
    success, which any client checking only the status code would miss.
    """
    if not isinstance(result, dict):
        return JSONResponse(
            status_code=500,
            content=_error_body("INVALID_TOOL_RESULT", "", correlation_id),
        )

    body = dict(result)
    body["correlation_id"] = correlation_id

    if body.get("ok", False):
        return body

    code = str(body.get("error") or "TOOL_ERROR")
    status = _TOOL_ERROR_STATUS.get(code, 400)
    if status >= 500 and not config.PL_DEBUG:
        body.pop("detail", None)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(AuthError)
def handle_auth_error(request: Request, exc: AuthError) -> JSONResponse:
    """401 for a missing or unrecognised token, 403 for a wrong role."""
    status = 401 if exc.code in ("AUTH_MISSING", "AUTH_INVALID") else 403
    return JSONResponse(
        status_code=status,
        content=_error_body(exc.code, exc.detail, _cid(request)),
    )


@app.exception_handler(tools.ToolError)
def handle_tool_error(request: Request, exc: tools.ToolError) -> JSONResponse:
    """Turn business/integrity errors into structured JSON, never a traceback."""
    status = _TOOL_ERROR_STATUS.get(exc.code, 400)
    return JSONResponse(
        status_code=status,
        content=_error_body(exc.code, _redact(status, exc.detail), _cid(request)),
    )


@app.exception_handler(HTTPException)
def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """Render ``HTTPException`` in the same envelope as everything else.

    FastAPI's default handler emits ``{"detail": ...}``, which gave the API two
    shapes for the same class of failure.
    """
    code = _HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(
            code, _redact(exc.status_code, exc.detail), _cid(request)
        ),
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render 422s in the same envelope, with a readable field list."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        message = str(err.get("msg", ""))
        parts.append(f"{loc}: {message}" if loc else message)
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", "; ".join(parts), _cid(request)),
    )

# ---------------------------------------------------------------------------
# App-layer listings
# ---------------------------------------------------------------------------
#
# These live here, not in ``tools``, on purpose. A listing needs the whole
# store, which is exactly the widening ``tools._snapshot_for`` refuses to do;
# keeping them out of ``tools`` means they can never drift into
# ``decisions.PROPOSAL_KINDS`` and become model-reachable. They read the
# module-level dicts under ``tools._LOCK`` so a concurrent mutation cannot be
# observed half-applied.


def list_pos() -> Dict[str, Any]:
    """Return every PO, sorted by ID. App-layer only."""
    with tools._LOCK:
        return {
            "ok": True,
            "pos": [dict(po) for _, po in sorted(tools._POS.items())],
        }


def list_requisitions() -> Dict[str, Any]:
    """Return every requisition, sorted by ID. App-layer only."""
    with tools._LOCK:
        return {
            "ok": True,
            "requisitions": [
                dict(req) for _, req in sorted(tools._REQUISITIONS.items())
            ],
        }

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, Any]:
    """Unauthenticated liveness probe. Deliberately reveals nothing else."""
    return {"ok": True}


@app.post("/agent/run")
def run_agent(
    req: RunRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_agent),
) -> Any:
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    try:
        result = agent.run(
            req.message, correlation_id=correlation_id, principal=principal
        )
    except RuntimeError as exc:
        # Defensive: agent.run reports failures in-band, but a client-init
        # failure must still surface as a 5xx rather than a silent 200.
        raise HTTPException(status_code=503, detail=str(exc))

    body = (
        dict(result)
        if isinstance(result, dict)
        else {"ok": False, "error": "INVALID_AGENT_RESULT"}
    )
    body["correlation_id"] = correlation_id

    if not body.get("ok", False) and body.get("error") in _AGENT_FATAL_ERRORS:
        if not config.PL_DEBUG:
            body.pop("detail", None)
        return JSONResponse(status_code=503, content=body)

    # MAX_STEPS_EXCEEDED and ordinary replies are 200 with ``ok`` in the body.
    return body


@app.get("/audit")
def read_audit(
    limit: int = 100,
    offset: int = 0,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Dict[str, Any]:
    """Return a page of the durable audit chain.

    Delegates to the tool layer's loader so a corrupt or unreadable log raises
    ``ToolError`` and is rendered by the handler above as a structured 503,
    instead of an unhandled ``JSONDecodeError``/``OSError`` traceback.

    ``limit`` is capped at 1000 so a single request cannot pull an unbounded
    chain into memory. ``total`` is the full chain length, so a client can page
    without guessing.
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=400, detail="limit must be between 1 and 1000"
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    entries = tools._load_audit_from_file()
    return {
        "ok": True,
        "entries": entries[offset : offset + limit],
        "total": len(entries),
        "limit": limit,
        "offset": offset,
        "correlation_id": cid,
    }


@app.get("/audit/verify")
def verify_audit(
    from_file: bool = True,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Dict[str, Any]:
    """Re-verify the chain.

    ``verify_audit_chain`` reports an unreadable log in-band with ``ok: true``
    and the raw ``OSError`` text in ``detail``, so the redaction that
    ``_redact`` applies to error responses has to be applied here too.
    """
    result = dict(tools.verify_audit_chain(from_file=from_file))
    result["correlation_id"] = cid
    if not config.PL_DEBUG and result.get("detail"):
        result.pop("detail", None)
    return result


@app.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(tools.get_invoice(invoice_id), cid)


@app.post("/invoices")
def ingest_invoice(
    req: IngestRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.ingest_invoice(
            req.invoice_id,
            req.vendor_id,
            req.po_id,
            req.amount,
            currency=req.currency,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )


@app.get("/pos")
def pos_index(
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    """List every PO. App-layer only; the model cannot reach this."""
    return _respond(list_pos(), cid)


@app.get("/pos/{po_id}")
def get_po(
    po_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(tools.get_po(po_id), cid)


@app.post("/pos")
def create_po(
    req: CreatePORequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Create a PO. Operator-only; never reachable by the model."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.create_po(
            req.vendor_id,
            req.amount,
            currency=req.currency,
            expected_delivery=req.expected_delivery,
            requisition_id=req.requisition_id,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )


@app.post("/pos/{po_id}/receipts")
def record_goods_receipt(
    po_id: str,
    req: ReceiptRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Record a goods receipt against an OPEN PO. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.record_goods_receipt(
            po_id, req.amount, correlation_id=correlation_id
        ),
        correlation_id,
    )


@app.post("/pos/{po_id}/close")
def close_po(
    po_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Close an OPEN PO. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.close_po(po_id, correlation_id=correlation_id), correlation_id
    )


@app.post("/pos/{po_id}/cancel")
def cancel_po(
    po_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Cancel an OPEN PO. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.cancel_po(po_id, correlation_id=correlation_id), correlation_id
    )


@app.get("/requisitions")
def requisition_index(
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    """List every requisition. App-layer only; the model cannot reach this."""
    return _respond(list_requisitions(), cid)


@app.post("/requisitions")
def create_requisition(
    req: CreateRequisitionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Raise a purchase requisition. Operator-only; never reachable by the model."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.create_requisition(
            req.description,
            req.quantity,
            req.amount,
            vendor_id=req.vendor_id,
            currency=req.currency,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )


@app.get("/requisitions/{requisition_id}")
def get_requisition(
    requisition_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(tools.get_requisition(requisition_id), cid)


@app.post("/requisitions/{requisition_id}/cancel")
def cancel_requisition(
    requisition_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Cancel an OPEN requisition. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools.cancel_requisition(
            requisition_id, correlation_id=correlation_id
        ),
        correlation_id,
    )


@app.get("/approvals/{approval_id}")
def get_approval(
    approval_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(tools.get_approval(approval_id), cid)


@app.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: str,
    req: ApproveRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Record a human approval decision. Does NOT move money.

    The approver is the authenticated operator, not the request body. A body
    value that disagrees is rejected rather than ignored, so a caller cannot
    believe it approved under someone else's name.
    """
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    identity = principal.sub

    claimed = (req.approver or "").strip()
    if claimed and claimed != identity:
        raise AuthError(
            "AUTH_FORBIDDEN",
            "approver does not match the authenticated operator",
        )

    return _respond(
        tools.approve_payout(approval_id, identity, correlation_id=correlation_id),
        correlation_id,
    )


@app.post("/approvals/{approval_id}/execute")
def execute(
    approval_id: str,
    req: ExecuteRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_executor),
) -> Any:
    """Settle an approved payout. Executor-only, never reachable by the model.

    Separation of duties is enforced in ``tools._execute_payout`` under the
    state lock, so a direct caller cannot bypass it.
    """
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        tools._execute_payout(
            approval_id, principal.sub, correlation_id=correlation_id
        ),
        correlation_id,
    )
