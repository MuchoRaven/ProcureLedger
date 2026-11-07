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
Every endpoint except ``/health`` and ``/ready`` requires
``Authorization: Bearer <token>``. Three credentials exist and they are not
interchangeable:

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
Every failure - auth, validation, business, integrity, or an unexpected
exception - returns one envelope::

    {"ok": false, "error": CODE, "detail": str, "correlation_id": str}

``detail`` is blanked on 5xx unless ``config.PL_DEBUG`` is set, because
``tools`` puts raw ``OSError`` text in ``ToolError.detail`` and that text
contains host paths. The same redaction is applied to the ``detail`` field
inside ``/audit/verify``'s 200 response, which reports an unreadable log
in-band.

Status mapping:

* ``AUTH_MISSING`` / ``AUTH_INVALID`` -> 401, ``AUTH_FORBIDDEN`` -> 403.
* ``RATE_LIMITED`` -> 429, with ``Retry-After`` set to the time until the
  next token.
* ``tools.ToolError`` -> ``_TOOL_ERROR_STATUS``, default 400.
* Tool results that come back ``ok: false`` -> the same mapping, so
  ``GET /invoices/INV-NOPE`` is a 404 rather than a 200 carrying an error.
* Agent client/model init failures -> 503.
* Agent step-cap exhaustion -> 200 with ``ok: false``; the run completed.
* Anything unhandled -> 500 ``INTERNAL_ERROR``, still in the envelope.

Correlation IDs
---------------
``X-Correlation-ID`` is echoed on every response, including errors. The
inbound header is used when present, otherwise a ``uuid4`` is generated once
in middleware and stored on ``request.state``, so the dependency, the handlers
and the body all agree on one value. A non-empty ``correlation_id`` in the JSON
body still wins, for backwards compatibility with existing callers.

Rate limiting
-------------
``_authenticate`` charges one token per request against a per-principal bucket
(``PL_RATE_LIMIT_PER_MIN``, burst ``PL_RATE_LIMIT_BURST``). Keying on the
authenticated principal rather than the client IP means a shared NAT does not
starve one caller because another is busy, and a spoofed ``X-Forwarded-For``
cannot buy a fresh budget.

The limiter is in-process, which is correct only because the app is pinned to
one worker (see Startup). With N workers each would enforce its own budget and
the effective limit would be N times the configured one.

Unauthenticated endpoints (``/health``, ``/ready``) are not limited: there is
no principal to key on, and doing it by IP needs a proxy-trust decision that
belongs with the deployment, not here.

Startup
-------
The lifespan hook takes an exclusive ``fcntl.flock`` on
``<AUDIT_LOG_PATH>.lock`` *first*, then runs ``sandbox.preflight`` and
``preflight_auth``. The lock comes first so a second worker fails before it
pays for the sandbox smoke test.

The lock is the only thing pinning the process to one worker, and it has to
be: ``tools._LOCK`` and the audit chain are process-local, so a second worker
would fork the chain and interleave mutations. Run with ``--workers 1``. A
misconfigured ``--workers N`` does not corrupt anything - the extra workers
fail to boot - but uvicorn's supervisor will restart them, so the symptom is a
crash loop rather than a forked chain. That is the intended trade: loud and
harmless beats quiet and corrupting.

If ``AUDIT_LOG_PATH`` is empty the lock is skipped and the single-worker
guarantee is not enforced; a warning is logged at boot.

Follow-up scheduler
-------------------
When ``PL_FOLLOWUP_INTERVAL_S`` is positive, ``lifespan`` starts
``_followup_loop`` as a background task and cancels it on shutdown. The pass
runs in a worker thread (``asyncio.to_thread``) because ``run_followup`` does
an fsync per flagged PO; running it on the event loop would block every
request for the duration of the pass. The pass is idempotent, so a
misconfigured multi-worker deployment that double-runs it is harmless.
"""

from __future__ import annotations

import asyncio
import datetime
import fcntl
import hmac
import logging
import math
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, confloat, conint

import agent
import config
import sandbox
import tools

from tool_modules import (
    _followup,
    _purchases,
    _tools_ap,
    _tools_audit,
    _tools_procurement,
    _tools_sourcing,
)

from tool_modules._state import _RFQS

from tool_modules._codes import (
    PO_NOT_LATE,
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
    NO_QUOTES_FOR_SKU,
    INVALID_WEIGHTS,
)

_LOG = logging.getLogger(__name__)

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

    ``auth_method`` is set explicitly so the sandbox request envelope and the
    audit trail record how the caller authenticated, rather than falling back
    to ``"service"`` for every credential.
    """
    table: Dict[str, tools.Principal] = {}
    agent_token = (config.PL_AGENT_TOKEN or "").strip()
    operator_token = (config.PL_OPERATOR_TOKEN or "").strip()
    executor_token = (config.PL_EXECUTOR_TOKEN or "").strip()

    if agent_token:
        table[agent_token] = tools.Principal(
            sub="agent",
            roles=frozenset({ROLE_AGENT}),
            auth_method="bearer",
        )
    if operator_token:
        name = (config.PL_OPERATOR_NAME or "operator").strip() or "operator"
        table[operator_token] = tools.Principal(
            sub=name,
            roles=frozenset({ROLE_OPERATOR}),
            auth_method="bearer",
        )
    if executor_token:
        name = (config.PL_EXECUTOR_NAME or "executor").strip() or "executor"
        table[executor_token] = tools.Principal(
            sub=name,
            roles=frozenset({ROLE_EXECUTOR}),
            auth_method="bearer",
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


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised when a principal has spent its request budget."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("RATE_LIMITED")
        self.code = "RATE_LIMITED"
        self.retry_after = retry_after


class _TokenBucket:
    """One principal's budget: ``capacity`` tokens, refilled continuously."""

    __slots__ = ("capacity", "refill_per_s", "tokens", "updated")

    def __init__(self, capacity: float, refill_per_s: float, now: float) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.tokens = capacity
        self.updated = now

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(
            self.capacity, self.tokens + elapsed * self.refill_per_s
        )
        self.updated = now

    def take(self, now: float) -> bool:
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def seconds_until_token(self, now: float) -> float:
        self._refill(now)
        if self.refill_per_s <= 0:
            return 60.0
        return max(0.0, (1.0 - self.tokens) / self.refill_per_s)


class _RateLimiter:
    """Per-principal token buckets, guarded by a lock.

    The auth dependencies are sync functions, so FastAPI runs them in a
    threadpool; the lock is what makes the bucket updates safe there.

    ``_buckets`` is keyed by ``Principal.sub``, which is bounded by the number
    of configured credentials, so it does not need eviction.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[str, _TokenBucket] = {}

    def charge(self, key: str) -> Optional[float]:
        """Spend one token. Returns ``None`` if allowed, else seconds to wait."""
        per_min = config.PL_RATE_LIMIT_PER_MIN
        if per_min <= 0:
            return None
        burst = max(1, config.PL_RATE_LIMIT_BURST)
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(float(burst), per_min / 60.0, now)
                self._buckets[key] = bucket
            if bucket.take(now):
                return None
            return bucket.seconds_until_token(now)

    def reset(self) -> None:
        """Drop every bucket. For tests that change the configured limits."""
        with self._lock:
            self._buckets.clear()


_RATE_LIMITER = _RateLimiter()


def _authenticate(authorization: Optional[str]) -> tools.Principal:
    """Resolve a bearer token to a Principal, or raise ``AuthError``.

    Every candidate is compared, with no early exit, so the response time does
    not reveal how much of a guessed token was correct. Both sides are encoded
    to bytes because ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII
    ``str``.

    The rate-limit charge happens after the match, so a wrong token cannot
    spend a real principal's budget.
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

    retry_after = _RATE_LIMITER.charge(matched.sub)
    if retry_after is not None:
        raise RateLimitError(retry_after)
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

    The audit chain and ``tools._LOCK`` are process-local, so two workers would
    fork the chain and interleave mutations. ``flock`` is advisory but
    sufficient: every ProcureLedger process takes the same lock at boot, so a
    second worker fails fast instead of silently corrupting the chain.

    Returns the open handle (released on shutdown), or ``None`` when no audit
    log is configured - in which case the single-worker guarantee is *not*
    enforced and a warning is logged.
    """
    path = config.AUDIT_LOG_PATH
    if not path:
        _LOG.warning(
            "AUDIT_LOG_PATH is empty: the single-worker lock is disabled and "
            "the audit chain is not protected from a second worker"
        )
        return None

    lock_path = path + ".lock"
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot open the audit lock file {lock_path}: {exc}"
        ) from exc

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


async def _followup_loop() -> None:
    """Run the follow-up pass on a fixed interval.

    ``asyncio.to_thread`` is required, not optional: ``run_followup`` does an
    fsync per flagged PO, and calling it directly on the event loop would
    block every request for the duration of the pass.

    The interval is re-read each iteration so a runtime change to
    ``config.PL_FOLLOWUP_INTERVAL_S`` takes effect on the next tick. The task
    is only started when the value is ``> 0``, so the ``sleep(0)`` spin that a
    zero value would cause is not reachable in normal operation.
    """
    while True:
        await asyncio.sleep(config.PL_FOLLOWUP_INTERVAL_S)
        try:
            await asyncio.to_thread(_followup.run_followup)
        except Exception:
            _LOG.exception("follow-up pass failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Refuse to serve if this host cannot sandbox, authenticate, or is a
    second worker.

    The lock is taken first so a duplicate worker fails before it pays for the
    sandbox smoke test in ``sandbox.preflight``.

    The follow-up scheduler is started only when ``PL_FOLLOWUP_INTERVAL_S`` is
    positive, and is cancelled before the lock is released so a restart does
    not race a still-running pass.
    """
    lock_handle = _acquire_single_worker_lock()
    followup_task: Optional[asyncio.Task] = None
    try:
        sandbox.preflight()
        preflight_auth()
        if config.PL_FOLLOWUP_INTERVAL_S > 0:
            followup_task = asyncio.create_task(_followup_loop())
        yield
    finally:
        if followup_task is not None:
            followup_task.cancel()
            await asyncio.gather(followup_task, return_exceptions=True)
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

# Same constraints for a count. ``quantity`` was a bare ``float``, so a
# negative or boolean quantity passed validation and reached the store.
Quantity = confloat(strict=True, gt=0)

# Non-negative whole days. ``strict=True`` rejects booleans and strings, so the
# ``isinstance(lead_time_days, bool)`` guard in ``tools.submit_quote`` cannot be
# reached with a bool. ``conint`` is used for the same v1/v2 reason as above.
LeadTimeDays = conint(strict=True, ge=0)

# Agent errors that mean "the deployment is broken", not "the run finished".
_AGENT_FATAL_ERRORS = {"CLIENT_INIT_FAILED", "MODEL_CALL_FAILED"}

# Business error code -> HTTP status. Anything unmapped is a 400.
_TOOL_ERROR_STATUS = {
    "AUDIT_LOG_CORRUPT": 503,
    "AUDIT_LOG_UNREADABLE": 503,
    # ``_persist_audit_entry`` raises this when the append fails. It was
    # unmapped, so a disk failure on audit write surfaced as a 400 - a client
    # error for a server-side outage.
    "AUDIT_LOG_UNWRITABLE": 503,
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
    # Phase 0: RFQs, quotes, stock. ``*_NOT_FOUND`` -> 404; the rest are state
    # conflicts -> 409. ``RFQ_HAS_NO_QUOTES`` matches none of the suffix rules
    # but is a conflict (the RFQ exists, it just cannot be awarded yet), so it
    # is 409 rather than 404.
    RFQ_NOT_FOUND: 404,
    QUOTE_NOT_FOUND: 404,
    STOCK_ITEM_NOT_FOUND: 404,
    RFQ_NOT_OPEN: 409,
    RFQ_EXISTS: 409,
    RFQ_HAS_NO_QUOTES: 409,
    QUOTE_EXISTS: 409,
    QUOTE_VENDOR_MISMATCH: 409,
    QUOTE_CURRENCY_MISMATCH: 409,
    QUOTE_EXPIRED: 409,
    QUOTE_NOT_SUBMITTED: 409,
    QUOTE_ALREADY_AWARDED: 409,
    REQUISITION_ALREADY_CONVERTED: 409,
    # Follow-up: a PO that is not late is a conflict, not a missing record.
    PO_NOT_LATE: 409,
    # Vendor comparison. ``NO_QUOTES_FOR_SKU`` is a missing record - the SKU
    # exists in the catalogue but nobody has quoted it - so 404, matching
    # ``RFQ_NOT_FOUND``. ``INVALID_WEIGHTS`` is a caller error, so 400; it is
    # listed explicitly rather than left to the default so the mapping is
    # visible next to the code it maps.
    NO_QUOTES_FOR_SKU: 404,
    INVALID_WEIGHTS: 400,
}

# Status -> code for ``HTTPException`` raised by this module or by FastAPI.
_HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_INVALID",
    403: "AUTH_FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
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
    quantity: Quantity
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


class FollowupRequest(BaseModel):
    """Body for ``POST /followup/run``.

    ``as_of`` is an ISO date; an empty value means "today", matching
    ``_followup._resolve_as_of``.
    """

    as_of: str = ""
    correlation_id: str = ""

# ---------------------------------------------------------------------------
# Phase 0 request models: RFQs, quotes, requisition conversion, stock
# ---------------------------------------------------------------------------
#
# These are the minimum field sets the Phase 1 endpoints need. ``due_date`` and
# ``valid_until`` stay ``str`` here and are validated by the handler with
# ``_validate_iso_date``, for the same reason ``Amount``/``Quantity`` are
# ``confloat``: a pydantic validator would have to be written twice to work on
# both v1 and v2, and this module is deliberately version-agnostic.


class CreateRFQRequest(BaseModel):
    description: str
    quantity: Quantity
    # ``create_rfq`` takes a list of vendors to invite, not a single vendor.
    vendor_ids: List[str]
    due_date: str = ""
    currency: str = "USD"
    correlation_id: str = ""


class SubmitQuoteRequest(BaseModel):
    vendor_id: str
    # ``submit_quote`` prices per unit and records a lead time, not a total
    # ``amount``; the PO amount is derived at award time.
    unit_price: Amount
    currency: str = "USD"
    lead_time_days: LeadTimeDays = 0
    valid_until: str
    correlation_id: str = ""


class AwardQuoteRequest(BaseModel):
    quote_id: str
    correlation_id: str = ""


class ConvertRequisitionRequest(BaseModel):
    vendor_id: str = ""
    amount: Amount
    currency: str = "USD"
    expected_delivery: str = ""
    correlation_id: str = ""


class StockItemRequest(BaseModel):
    item_id: str
    description: str = ""
    quantity: Quantity
    unit_cost: Amount
    vendor_id: str = ""
    currency: str = "USD"
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


def _validate_iso_date(value: str, field: str) -> str:
    """Return ``value`` if it is an ISO date, or raise a 400.

    ``expected_delivery`` was an unvalidated ``str``, so a malformed date was
    stored and would break any later delay computation. An empty value is
    allowed and means "not scheduled".
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be an ISO date (YYYY-MM-DD)",
        )
    return text

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


@app.exception_handler(RateLimitError)
def handle_rate_limit(request: Request, exc: RateLimitError) -> JSONResponse:
    """429 with ``Retry-After``, in the same envelope as every other failure.

    ``Retry-After`` is computed from the bucket rather than hard-coded, so a
    client that honours it retries when a token is actually available instead
    of guessing.
    """
    retry_after = max(1, int(math.ceil(exc.retry_after)))
    return JSONResponse(
        status_code=429,
        content=_error_body(exc.code, "request budget exhausted", _cid(request)),
        headers={"Retry-After": str(retry_after)},
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


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so an unhandled exception still returns the envelope.

    Without this, a ``KeyError`` in a handler produced Starlette's plain-text
    500 with no ``correlation_id`` and no ``ok`` field - a second response
    shape for the same class of failure, and one a client cannot parse.

    The log line carries the correlation ID so the server-side traceback can be
    tied to the response the client actually received.
    """
    _LOG.exception(
        "unhandled error on %s %s correlation_id=%s",
        request.method,
        request.url.path,
        _cid(request),
    )
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", _redact(500, str(exc)), _cid(request)),
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


def list_invoices() -> Dict[str, Any]:
    """Return every invoice, sorted by ID. App-layer only.

    Without this an approver had to know an invoice ID out of band to find
    anything to act on; ``GET /invoices/{id}`` alone cannot answer "what is
    waiting for me?".
    """
    with tools._LOCK:
        return {
            "ok": True,
            "invoices": [
                dict(inv) for _, inv in sorted(tools._INVOICES.items())
            ],
        }


def list_approvals() -> Dict[str, Any]:
    """Return every payout approval, sorted by ID. App-layer only.

    Same reasoning as ``list_invoices``: the human-approval gate is only real
    if the approver can discover the pending requests.
    """
    with tools._LOCK:
        return {
            "ok": True,
            "approvals": [
                dict(appr) for _, appr in sorted(tools._APPROVALS.items())
            ],
        }


def list_rfqs() -> Dict[str, Any]:
    """Return every RFQ, sorted by ID. App-layer only.

    ``tools`` has no ``list_rfqs`` for the same reason it has no ``list_pos``:
    a listing needs the whole store, which is exactly the widening
    ``tools._snapshot_for`` refuses to do. Keeping it here means it can never
    drift into ``decisions.PROPOSAL_KINDS`` and become model-reachable.
    """
    with tools._LOCK:
        return {
            "ok": True,
            "rfqs": [dict(rfq) for _, rfq in sorted(_RFQS.items())],
        }

# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _audit_space_problem() -> str:
    """Return a reason the audit log cannot be appended to, or ``""``.

    Mirrors the boot-time check in ``sandbox.preflight``. It is duplicated
    rather than imported because ``sandbox`` keeps it private, and because the
    two answer different questions: preflight asks "may we start?", the
    readiness probe asks "can we still serve?". Keeping them independent means
    a change to one cannot silently disable the other.
    """
    directory = os.path.dirname(os.path.abspath(config.AUDIT_LOG_PATH)) or "."
    if not os.path.isdir(directory):
        return f"audit log directory does not exist: {directory}"
    if not os.access(directory, os.W_OK | os.X_OK):
        return f"audit log directory is not writable: {directory}"

    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        # Non-POSIX host. bwrap is Linux-only, so this is unreachable in a
        # deployment that got this far; skip rather than fail.
        return ""
    try:
        stats = statvfs(directory)
    except OSError as exc:
        return f"cannot stat audit log filesystem {directory}: {exc}"

    free = stats.f_bavail * stats.f_frsize
    required = config.PL_AUDIT_MIN_FREE_MB * 1024 * 1024
    if free < required:
        return (
            f"audit log filesystem {directory} has {free} bytes free, "
            f"need at least {required}"
        )
    return ""

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, Any]:
    """Unauthenticated liveness probe. Deliberately reveals nothing else."""
    return {"ok": True}


@app.get("/ready")
def ready() -> Any:
    """Unauthenticated readiness probe.

    ``/health`` only proves the process is up. Readiness has to answer "can
    this process still do its job?", and the one thing that can change after
    boot is the audit log's filesystem: audit-before-mutation means a full
    disk refuses every mutation. Re-checking free space here turns that into a
    failed probe instead of a stream of 503s.

    The reason is logged, not returned: it names a host path, and this
    endpoint is unauthenticated.
    """
    problem = _audit_space_problem()
    if problem:
        _LOG.warning("readiness check failed: %s", problem)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "NOT_READY"},
        )
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
        # Defensive: agent.run reports failures in-band, but a client init
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
    result = dict(_tools_audit.verify_audit_chain(from_file=from_file))
    result["correlation_id"] = cid
    if not config.PL_DEBUG and result.get("detail"):
        result.pop("detail", None)
    return result


@app.get("/invoices")
def invoice_index(
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    """List every invoice. App-layer only; the model cannot reach this."""
    return _respond(list_invoices(), cid)


@app.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(_tools_ap.get_invoice(invoice_id), cid)


@app.post("/invoices")
def ingest_invoice(
    req: IngestRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_ap.ingest_invoice(
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
    return _respond(_tools_procurement.get_po(po_id), cid)


@app.post("/pos")
def create_po(
    req: CreatePORequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Create a PO. Operator-only; never reachable by the model."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    expected_delivery = _validate_iso_date(
        req.expected_delivery, "expected_delivery"
    )
    return _respond(
        _tools_procurement.create_po(
            req.vendor_id,
            req.amount,
            currency=req.currency,
            expected_delivery=expected_delivery,
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
        _tools_procurement.record_goods_receipt(
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
        _tools_procurement.close_po(po_id, correlation_id=correlation_id),
        correlation_id,
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
        _tools_procurement.cancel_po(po_id, correlation_id=correlation_id),
        correlation_id,
    )

# ---------------------------------------------------------------------------
# Purchase monitoring: read-only derived views over the PO store
# ---------------------------------------------------------------------------
#
# Operator-only and app-layer only. ``_purchases`` is not in
# ``decisions.PROPOSAL_KINDS`` or ``decisions.SNAPSHOT_SCOPES``, so the model
# cannot reach it, and it writes no audit events - a monitoring read must not
# grow the chain. ``as_of`` is a query parameter, not a body: these are reads,
# and an empty value means "today" (see ``_purchases._resolve_as_of``).


@app.get("/purchases")
def purchase_index(
    as_of: str = "",
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """List every PO with its derived purchase status. Operator-only."""
    return _respond(_purchases.list_purchases(as_of), cid)


@app.get("/purchases/{po_id}")
def purchase_detail(
    po_id: str,
    as_of: str = "",
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Return one PO with its derived purchase status. Operator-only."""
    return _respond(_purchases.purchase_status(po_id, as_of), cid)


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
    # Known gap: ``_tools_procurement`` has no public ``create_requisition``
    # wrapper yet - only the private ``_apply_create_requisition``. This
    # endpoint raises AttributeError at request time until the wrapper lands.
    return _respond(
        _tools_procurement.create_requisition(
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
    return _respond(_tools_procurement.get_requisition(requisition_id), cid)


@app.post("/requisitions/{requisition_id}/cancel")
def cancel_requisition(
    requisition_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Cancel an OPEN requisition. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    # Known gap: same as ``create_requisition`` - no public wrapper exists yet.
    return _respond(
        _tools_procurement.cancel_requisition(
            requisition_id, correlation_id=correlation_id
        ),
        correlation_id,
    )


@app.get("/approvals")
def approval_index(
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    """List every payout approval. App-layer only; the model cannot reach this."""
    return _respond(list_approvals(), cid)


@app.get("/approvals/{approval_id}")
def get_approval(
    approval_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_any),
) -> Any:
    return _respond(_tools_ap.get_approval(approval_id), cid)


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
        _tools_ap.approve_payout(
            approval_id, identity, correlation_id=correlation_id
        ),
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
        _tools_ap._execute_payout(
            approval_id, principal.sub, correlation_id=correlation_id
        ),
        correlation_id,
    )

# ---------------------------------------------------------------------------
# Phase 1 endpoints: RFQs and quotes
# ---------------------------------------------------------------------------
#
# Operator-only and app-layer only. None of these tools are in
# ``decisions.PROPOSAL_KINDS``, so the model cannot reach them; the operator
# credential is required for every one, matching the procurement mutations
# above. ``award_quote`` raises the PO through the existing ``create_po`` path,
# so an award inherits the ``PO_CREATED`` audit entry.


@app.post("/rfqs")
def create_rfq(
    req: CreateRFQRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Open a request for quotes. Operator-only; never reachable by the model."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_sourcing.create_rfq(
            req.description,
            req.quantity,
            req.currency,
            req.vendor_ids,
            due_date=req.due_date,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )


@app.get("/rfqs")
def rfq_index(
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """List every RFQ. App-layer only; the model cannot reach this."""
    return _respond(list_rfqs(), cid)


@app.get("/rfqs/{rfq_id}")
def get_rfq(
    rfq_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    return _respond(_tools_sourcing.get_rfq(rfq_id), cid)


@app.post("/rfqs/{rfq_id}/quotes")
def submit_quote(
    rfq_id: str,
    req: SubmitQuoteRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Submit a vendor quote against an OPEN RFQ. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_sourcing.submit_quote(
            rfq_id,
            req.vendor_id,
            req.unit_price,
            req.currency,
            req.lead_time_days,
            req.valid_until,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )


@app.get("/rfqs/{rfq_id}/quotes")
def rfq_quotes(
    rfq_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """List every quote on an RFQ. App-layer only."""
    return _respond(_tools_sourcing.list_quotes(rfq_id), cid)


@app.post("/rfqs/{rfq_id}/close")
def close_rfq(
    rfq_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Close an OPEN RFQ so no further quotes are accepted. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_sourcing.close_rfq(rfq_id, correlation_id=correlation_id),
        correlation_id,
    )


@app.post("/rfqs/{rfq_id}/cancel")
def cancel_rfq(
    rfq_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Cancel an OPEN RFQ. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_sourcing.cancel_rfq(rfq_id, correlation_id=correlation_id),
        correlation_id,
    )


@app.post("/rfqs/{rfq_id}/award")
def award_quote(
    rfq_id: str,
    req: AwardQuoteRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Award an RFQ to one quote and raise the resulting PO. Operator-only."""
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _tools_sourcing.award_quote(
            rfq_id, req.quote_id, correlation_id=correlation_id
        ),
        correlation_id,
    )


@app.get("/quotes/{quote_id}")
def get_quote(
    quote_id: str,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    return _respond(_tools_sourcing.get_quote(quote_id), cid)

# ---------------------------------------------------------------------------
# Follow-up endpoints: late POs and delay notification
# ---------------------------------------------------------------------------
#
# Operator-only and app-layer only. None of these tools are in
# ``decisions.PROPOSAL_KINDS``, so the model cannot reach them. The handler
# names are chosen so they do not shadow the module functions they call.


@app.get("/followup")
def followup_index(
    as_of: str = "",
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """List every late PO as of ``as_of``. App-layer only.

    ``as_of`` is a query parameter, not a body: this is a read, and an empty
    value means "today" (see ``_followup._resolve_as_of``).
    """
    return _respond(_followup.list_late_pos(as_of), cid)


@app.post("/followup/run")
def run_followup_pass(
    req: FollowupRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Flag every late, unflagged OPEN PO as of ``req.as_of``. Operator-only.

    The handler is named ``run_followup_pass`` so it does not shadow
    ``_followup.run_followup``.
    """
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(_followup.run_followup(req.as_of), correlation_id)


@app.post("/pos/{po_id}/notify-delay")
def notify_delay(
    po_id: str,
    req: ActionRequest,
    cid: str = Depends(correlation_id_header),
    principal: tools.Principal = Depends(require_operator),
) -> Any:
    """Record that a late PO's delay has been notified. Operator-only.

    The body is ``ActionRequest``, not ``FollowupRequest``: this endpoint takes
    no ``as_of``. The notifier is the authenticated operator's ``sub``, not a
    body field, so a caller cannot record a notification as someone else.
    """
    correlation_id = _resolve_correlation_id(cid, req.correlation_id)
    return _respond(
        _followup._apply_notify_delay(po_id, principal.sub, correlation_id),
        correlation_id,
    )
