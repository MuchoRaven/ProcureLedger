"""
agent.py
========
ProcureLedger - agent loop.

The model reaches tools exclusively through the default-deny ``_DISPATCH``
allowlist below. Anything not in ``ALLOWED_TOOLS`` is unreachable, even if it
is a public function in ``tools.py`` (e.g. ``ingest_invoice``,
``approve_payout``, ``_execute_payout``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import config
import tools

# ---------------------------------------------------------------------------
# Default-deny allowlist
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = {
    "lookup_vendor",
    "run_three_way_match",
    "request_payout_approval",
    "log_audit_event",
    "get_invoice",
    "get_approval",
    "verify_audit_chain",
}

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
            "description": (
                "Recompute the audit hash chain and report the first divergence."
            ),
            "parameters": {"type": "object", "properties": {}},
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
# Client
# ---------------------------------------------------------------------------


def _client():
    if not config.OPENAI_API_KEY:
        raise RuntimeError("API key not set: set OPENAI_API_KEY")
    from openai import OpenAI

    return OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _execute_tool(
    name: str, arguments: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
    if name not in _DISPATCH:
        return {"ok": False, "error": "TOOL_NOT_ALLOWED", "tool": name}
    fn = _DISPATCH[name]
    try:
        return fn(correlation_id=correlation_id, **arguments)
    except tools.ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}
    except TypeError as exc:
        return {"ok": False, "error": "INVALID_ARGUMENTS", "detail": str(exc)}

# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def run(user_message: str, correlation_id: str = "") -> Dict[str, Any]:
    client = _client()
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    transcript: List[Dict[str, Any]] = []

    for step in range(config.AGENT_MAX_STEPS):
        response = client.chat.completions.create(
            model=config.VULTR_MODEL,
            messages=messages,
            tools=_TOOL_SCHEMAS,
            tool_choice="auto",
        )
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
            result = _execute_tool(name, arguments, correlation_id)
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
        "steps": config.AGENT_MAX_STEPS,
        "transcript": transcript,
    }
