"""
agent.py
========
ProcureLedger - agent loop.

The model reaches tools exclusively through the default-deny ``_DISPATCH``
allowlist below, and every allowlisted call is executed through
``sandbox.dispatch`` - never by calling ``tools`` directly. Anything not in
``ALLOWED_TOOLS`` is unreachable, even if it is a public function in
``tools.py``. ``ingest_invoice``, ``approve_payout``, ``_execute_payout``,
``get_approval`` and ``verify_audit_chain`` are therefore app-layer only:
reachable over HTTP, never by the model.

``ALLOWED_TOOLS`` is derived from ``decisions.PROPOSAL_KINDS`` so the agent
allowlist and the sandbox's accepted-tool set cannot drift apart.

Client configuration
--------------------
The client targets Vultr Serverless Inference. Resolution order for each
setting is environment variable, then ``config``, then a hardcoded default:

* API key  - ``OPENAI_API_KEY``
* Base URL - ``OPENAI_API_BASE``, then ``OPENAI_BASE_URL``
* Model    - ``VULTR_MODEL``, defaulting to ``deepseek-v4.1-flash-1017``

Reading the environment first means a deployment can be repointed without
editing ``config.py``, and the ``config`` fallback keeps the module importable
if ``config.py`` predates ``OPENAI_API_BASE``.

Audit bracketing
----------------
Every allowlisted tool call is wrapped in two audit links,
``TOOL_CALL_STARTED`` and ``TOOL_CALL_COMPLETED``, so the durable chain records
what the model *asked for* and not merely what the tool layer changed. The
bracket is skipped for ``log_audit_event`` itself, which would otherwise
recurse. If the opening link cannot be written the call is aborted, because
performing an unrecorded action defeats the point of the chain. If the closing
link fails the tool has already run, so the result is returned with an
``audit_error`` field rather than being discarded.

The bracket writes go straight to ``tools.log_audit_event`` with
``source="tool"``, deliberately bypassing the sandbox: the control plane's own
bookkeeping must not depend on the sandbox being healthy, or a broken sandbox
would make the chain unwritable and the fail-closed check would abort every
call before it was even attempted.

Principal threading
-------------------
``run`` and ``dispatch_tool`` take the authenticated ``Principal`` from the
HTTP layer and forward it to ``sandbox.dispatch``, so the sandbox envelope
records who asked. The principal is never taken from the model's tool
arguments: ``dispatch_tool`` strips ``principal`` and ``correlation_id`` from
the model's arguments before calling the sandbox.

Failure contract
----------------
``run`` does not raise for expected failures. It returns ``ok: False`` with a
machine-readable ``error``:

* ``CLIENT_INIT_FAILED`` - no API key, or the SDK could not be constructed.
* ``MODEL_CALL_FAILED``  - the provider call raised or returned no choices.
* ``MAX_STEPS_EXCEEDED`` - the loop hit the step cap without a final answer.

``app.py`` maps the first two to HTTP 503 and leaves the third as a 200.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import config
import decisions
import sandbox
import tools

# ---------------------------------------------------------------------------
# Default-deny allowlist
# ---------------------------------------------------------------------------

# Absolute ceiling on loop iterations. ``config.AGENT_MAX_STEPS`` may lower it
# but never raise it, so a misconfigured environment variable cannot turn the
# loop into an unbounded spend.
_HARD_MAX_STEPS = 8

# Derived from the sandbox's own table so the two allowlists cannot drift.
ALLOWED_TOOLS = set(decisions.PROPOSAL_KINDS)

# Built at import so a typo in ``ALLOWED_TOOLS`` fails fast rather than at the
# first tool call. The values are not called directly - execution goes through
# ``sandbox.dispatch`` - but the lookup proves every allowlisted name exists in
# ``tools``.
_DISPATCH = {name: getattr(tools, name) for name in ALLOWED_TOOLS}

# ---------------------------------------------------------------------------
# Tool schemas exposed to the model
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_vendor",
            "description": "Check vendor status and risk tier. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {"vendor_id": {"type": "string"}},
                "required": ["vendor_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_three_way_match",
            "description": (
                "Compare PO, goods receipt and invoice; set MATCHED or EXCEPTION."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string"},
                    "po_id": {"type": "string"},
                },
                "required": ["invoice_id", "po_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_payout_approval",
            "description": "Open a human approval gate. Does NOT move money.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "string"},
                    "amount": {"type": "number"},
                    "po_id": {"type": "string"},
                    "invoice_id": {"type": "string"},
                },
                "required": ["vendor_id", "amount", "po_id", "invoice_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_audit_event",
            "description": "Record an agent reasoning note in the audit trail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["event_type", "payload"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_invoice",
            "description": "Read an invoice record by ID.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are ProcureLedger, an accounts-payable assistant. "
    "Use the provided tools to inspect vendors, run three-way matches, and "
    "request human approval for payouts. You cannot move money: payouts are "
    "settled only after a human approves. Always log your reasoning with "
    "log_audit_event. Never invent IDs or amounts."
)

# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
_DEFAULT_MODEL = "deepseek-v4.1-flash-1017"


def _api_key() -> str:
    """Resolve the API key: environment first, then ``config``."""
    return (
        os.environ.get("OPENAI_API_KEY")
        or getattr(config, "OPENAI_API_KEY", "")
        or ""
    ).strip()


def _base_url() -> str:
    """Resolve the base URL: ``OPENAI_API_BASE``, then ``OPENAI_BASE_URL``."""
    for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for attr in ("OPENAI_API_BASE", "OPENAI_BASE_URL"):
        value = (getattr(config, attr, "") or "").strip()
        if value:
            return value
    return _DEFAULT_BASE_URL


def _model() -> str:
    """Resolve the model name, defaulting to the locked Vultr model."""
    return (
        (os.environ.get("VULTR_MODEL") or "").strip()
        or (getattr(config, "VULTR_MODEL", "") or "").strip()
        or _DEFAULT_MODEL
    )


def _client():
    """Build the OpenAI-compatible client, or raise ``RuntimeError``."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("API key not set: set OPENAI_API_KEY")
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=_base_url())

# ---------------------------------------------------------------------------
# Audit bracketing
# ---------------------------------------------------------------------------


def _audit(
    event_type: str, payload: Dict[str, Any], correlation_id: str
) -> str:
    """Write one audit link. Return an error string, or ``""`` on success.

    Calls ``tools.log_audit_event`` directly rather than going through the
    sandbox: the control plane's own bookkeeping must not depend on the sandbox
    being healthy. ``source="tool"`` marks the link as control-plane authored,
    which also lets it use the reserved event types. ``tools.log_audit_event``
    reports bad input in-band and raises ``ToolError`` only when the chain
    itself is unwritable, so both paths are folded into a single error string
    here.
    """
    try:
        result = tools.log_audit_event(
            event_type, payload, correlation_id=correlation_id, source="tool"
        )
    except tools.ToolError as exc:
        return f"{exc.code}: {exc.detail}" if exc.detail else exc.code
    if not isinstance(result, dict) or not result.get("ok", False):
        error = result.get("error") if isinstance(result, dict) else None
        return str(error or "AUDIT_WRITE_FAILED")
    return ""

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def dispatch_tool(
    name: str,
    arguments: Dict[str, Any],
    correlation_id: str,
    principal: Any = None,
) -> Dict[str, Any]:
    """Run one allowlisted tool through the sandbox, bracketed by audit links."""
    if name not in _DISPATCH:
        return {"ok": False, "error": "TOOL_NOT_ALLOWED", "tool": name}

    if not isinstance(arguments, dict):
        return {"ok": False, "error": "INVALID_ARGUMENTS", "tool": name}

    # ``correlation_id`` and ``principal`` are supplied by the loop, never by
    # the model; drop them from the model's arguments so they cannot collide
    # with the keyword arguments below.
    arguments = {
        k: v
        for k, v in arguments.items()
        if k not in ("correlation_id", "principal")
    }

    # ``log_audit_event`` is the audit primitive itself; bracketing it would
    # recurse.
    bracketed = name != "log_audit_event"

    if bracketed:
        error = _audit(
            "TOOL_CALL_STARTED",
            {"tool": name, "arguments": arguments},
            correlation_id,
        )
        if error:
            # Fail closed: an unrecorded action is worse than a refused one.
            return {
                "ok": False,
                "error": "AUDIT_WRITE_FAILED",
                "detail": error,
                "tool": name,
            }

    try:
        result = sandbox.dispatch(
            name, arguments, principal=principal, correlation_id=correlation_id
        )
    except tools.ToolError as exc:
        result = {"ok": False, "error": exc.code, "detail": exc.detail}
    except TypeError as exc:
        result = {"ok": False, "error": "INVALID_ARGUMENTS", "detail": str(exc)}

    if not isinstance(result, dict):
        result = {"ok": False, "error": "INVALID_TOOL_RESULT", "tool": name}

    if bracketed:
        error = _audit(
            "TOOL_CALL_COMPLETED",
            {
                "tool": name,
                "ok": result.get("ok", False),
                "error": result.get("error"),
            },
            correlation_id,
        )
        if error:
            # The tool already ran, so the result stands; surface the gap.
            result = dict(result)
            result["audit_error"] = error

    return result

# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def run_agent_cycle(
    client: Any,
    user_message: str,
    correlation_id: str = "",
    principal: Any = None,
) -> Dict[str, Any]:
    """Drive the model/tool loop until it answers or the step cap is hit."""
    max_steps = min(config.AGENT_MAX_STEPS, _HARD_MAX_STEPS)
    model = _model()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    transcript: List[Dict[str, Any]] = []

    for step in range(max_steps):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOL_SCHEMAS,
                tool_choice="auto",
            )
        except Exception as exc:  # provider/transport failure
            return {
                "ok": False,
                "error": "MODEL_CALL_FAILED",
                "detail": str(exc),
                "steps": step,
                "transcript": transcript,
            }

        if not getattr(response, "choices", None):
            return {
                "ok": False,
                "error": "MODEL_CALL_FAILED",
                "detail": "provider returned no choices",
                "steps": step,
                "transcript": transcript,
            }

        choice = response.choices[0].message

        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": choice.content,
        }
        if choice.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in choice.tool_calls
            ]
        messages.append(assistant_msg)

        if not choice.tool_calls:
            return {
                "ok": True,
                "reply": choice.content or "",
                "steps": step + 1,
                "transcript": transcript,
            }

        for call in choice.tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = dispatch_tool(name, arguments, correlation_id, principal)
            transcript.append(
                {"tool": name, "arguments": arguments, "result": result}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                }
            )

    return {
        "ok": False,
        "error": "MAX_STEPS_EXCEEDED",
        "steps": max_steps,
        "transcript": transcript,
    }


def run(
    user_message: str,
    correlation_id: str = "",
    principal: Any = None,
) -> Dict[str, Any]:
    """Public entry point used by ``app.py``.

    Client construction is folded into the in-band error contract so a broken
    deployment surfaces as ``CLIENT_INIT_FAILED`` (HTTP 503) instead of an
    unhandled exception.
    """
    try:
        client = _client()
    except Exception as exc:
        return {
            "ok": False,
            "error": "CLIENT_INIT_FAILED",
            "detail": str(exc),
            "steps": 0,
            "transcript": [],
        }
    return run_agent_cycle(client, user_message, correlation_id, principal)
