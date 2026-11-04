"""
agent.py
========
ProcureLedger - LLM orchestrator.

Security posture
----------------
* Default-deny allowlist: ``ALLOWED_TOOLS`` is the single source of truth and
  ``_DISPATCH`` is *built by filtering it*, so a new public function in
  ``tools.py`` is never auto-exposed to the model.
* ``_execute_payout`` and ``ingest_invoice`` are app-layer only. The asserts
  below are the regression test that keeps them out of the agent's reach.
* The dispatcher strips model-supplied ``correlation_id`` and any kwargs that
  are not in the target function's signature, then injects the real
  ``correlation_id`` itself.
* ``temperature=0`` so the demo is reproducible.
* ``awaiting_human_approval`` short-circuits the loop the moment the payout
  gate opens, so the model cannot keep calling tools and "confirm" a payout
  in prose.

Operational note
----------------
``tools.py`` keeps ``_AUDIT_SEQ`` in module globals, so run the server with a
single worker (``uvicorn ... --workers 1``).
"""

from __future__ import annotations

import inspect
import json
import uuid
from typing import Any, Dict, List, Optional

import config
import tools

# ---------------------------------------------------------------------------
# Allowlist - default deny, single constant
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = frozenset(
    {
        "lookup_vendor",
        "run_three_way_match",
        "request_payout_approval",
        "log_audit_event",
        "get_invoice",
        "get_approval",
        "verify_audit_chain",
    }
)

# Built by filtering, so a new public function in tools.py is NOT auto-exposed.
_DISPATCH = {
    name: getattr(tools, name)
    for name in ALLOWED_TOOLS
    if callable(getattr(tools, name, None))
}

assert set(_DISPATCH) == ALLOWED_TOOLS, "allowlist references a missing tool"
assert "_execute_payout" not in _DISPATCH
assert "ingest_invoice" not in _DISPATCH

# ---------------------------------------------------------------------------
# Dispatcher - injects correlation_id, strips model-supplied args
# ---------------------------------------------------------------------------


def _deny(name: str, error: str, correlation_id: str) -> Dict[str, Any]:
    """Record and return a structured refusal for a non-allowlisted call."""
    try:
        tools.log_audit_event(
            "TOOL_CALL_REJECTED", {"tool": name, "error": error}, correlation_id
        )
    except Exception:
        pass
    return {
        "ok": False,
        "error": error,
        "tool": name,
        "correlation_id": correlation_id,
    }


def dispatch(name: str, args: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    """Execute one allowlisted tool. Never raises.

    Guarantees:
      * the model cannot reach a function outside ``ALLOWED_TOOLS``;
      * the model cannot pass ``correlation_id`` (it is injected here);
      * the model cannot pass kwargs the target signature does not accept.
    """
    if name not in ALLOWED_TOOLS:
        return _deny(name, "TOOL_NOT_ALLOWED", correlation_id)

    fn = _DISPATCH[name]
    params = set(inspect.signature(fn).parameters)

    # Drop unknown keys AND any model-supplied correlation_id.
    clean = {
        key: value
        for key, value in (args or {}).items()
        if key in params and key != "correlation_id"
    }
    if "correlation_id" in params:
        clean["correlation_id"] = correlation_id

    try:
        tools.log_audit_event(
            "TOOL_CALL", {"tool": name, "args": clean}, correlation_id
        )
    except Exception:
        pass  # audit failure must not block the call

    try:
        return fn(**clean)
    except Exception as exc:  # _fail can raise OSError on disk failure
        return {
            "ok": False,
            "error": "TOOL_EXCEPTION",
            "tool": name,
            "detail": str(exc),
            "correlation_id": correlation_id,
        }


# ---------------------------------------------------------------------------
# Tool schemas - must match tools.py exactly
# ---------------------------------------------------------------------------
# correlation_id is omitted from every schema; the dispatcher injects it.

TOOL_SCHEMAS: List[Dict[str, Any]] = [
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
                "Compare PO, goods receipt and invoice. Moves invoice to "
                "MATCHED or EXCEPTION."
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
    {
        "type": "function",
        "function": {
            "name": "get_approval",
            "description": "Read an approval record by ID.",
            "parameters": {
                "type": "object",
                "properties": {"approval_id": {"type": "string"}},
                "required": ["approval_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_audit_chain",
            "description": "Recompute the audit hash chain and report divergence.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Second line of defence: the private settlement path must never be offered.
_SCHEMA_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
assert _SCHEMA_NAMES <= ALLOWED_TOOLS, "schema exposes a non-allowlisted tool"
assert "_execute_payout" not in _SCHEMA_NAMES
assert "ingest_invoice" not in _SCHEMA_NAMES

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are ProcureLedger, an accounts-payable orchestrator.

Workflow, in order:
1. lookup_vendor(vendor_id) - confirm the vendor is ACTIVE and not HIGH risk.
2. run_three_way_match(invoice_id, po_id) - note the argument order.
3. If status is EXCEPTION, stop and report the mismatch_codes. Do not request approval.
4. If status is MATCHED, call request_payout_approval with the invoice total.
5. Then STOP and report that a human must approve.

Hard rules:
- You cannot move money. Settlement is a human action outside your tools.
- Never claim a payout happened. Never invent an approval_id.
- Never pass correlation_id; it is injected for you.
- If a tool returns ok=false, read the error code and decide whether to retry
  with corrected arguments or stop and report.
"""

# ---------------------------------------------------------------------------
# LLM client - lazy so tools.py tests run without the SDK installed
# ---------------------------------------------------------------------------

_CLIENT: Optional[Any] = None


def _client() -> Any:
    """Return a cached OpenAI-compatible client pointed at Vultr.

    Imports ``openai`` lazily and raises a clear error when ``VULTR_API_KEY``
    is unset, so importing this module never requires the SDK or a key.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    api_key = str(getattr(config, "VULTR_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError(
            "VULTR_API_KEY is not set; export it before running the agent."
        )

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "The 'openai' package is required to run the agent "
            "(pip install openai)."
        ) from exc

    _CLIENT = OpenAI(
        api_key=api_key,
        base_url=getattr(
            config, "VULTR_BASE_URL", "https://api.vultrinference.com/v1"
        ),
    )
    return _CLIENT


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def run_agent(user_message: str, correlation_id: str = "") -> Dict[str, Any]:
    """Drive the tool-calling loop until the model stops or the gate opens.

    Returns a structured result; never raises for expected failures.
    """
    correlation_id = correlation_id or ("REQ-" + uuid.uuid4().hex[:12].upper())
    trace: List[Dict[str, Any]] = []
    awaiting_human = False

    try:
        client = _client()
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": "LLM_CLIENT_UNAVAILABLE",
            "detail": str(exc),
            "correlation_id": correlation_id,
            "trace": trace,
            "steps": 0,
        }

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for step in range(config.AGENT_MAX_STEPS):
        try:
            resp = client.chat.completions.create(
                model=config.VULTR_MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": "LLM_CALL_FAILED",
                "detail": str(exc),
                "correlation_id": correlation_id,
                "trace": trace,
                "steps": step,
            }

        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return {
                "ok": True,
                "correlation_id": correlation_id,
                "final": msg.content,
                "trace": trace,
                "awaiting_human_approval": awaiting_human,
                "steps": step + 1,
            }

        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}

            result = dispatch(call.function.name, args, correlation_id)
            trace.append(
                {"tool": call.function.name, "args": args, "result": result}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                }
            )

            if result.get("requires_human_approval"):
                awaiting_human = True

        if awaiting_human:
            return {
                "ok": True,
                "correlation_id": correlation_id,
                "final": "Payout gated. Awaiting human approval.",
                "trace": trace,
                "awaiting_human_approval": True,
                "steps": step + 1,
            }

    return {
        "ok": False,
        "error": "AGENT_STEP_BUDGET_EXHAUSTED",
        "correlation_id": correlation_id,
        "trace": trace,
        "steps": config.AGENT_MAX_STEPS,
    }


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    prompt = " ".join(sys.argv[1:]) or (
        "Vendor VEND-001 sent invoice INV-2001 against PO-1001. Process it."
    )
    outcome = run_agent(prompt)
    print(json.dumps(outcome, indent=2, default=str))
