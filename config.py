import os

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Vultr Serverless Inference. ``OPENAI_API_BASE`` is the locked name;
# ``OPENAI_BASE_URL`` is accepted as a fallback so existing deployments keep
# working without an env-var rename.
OPENAI_API_BASE = os.environ.get(
    "OPENAI_API_BASE",
    os.environ.get("OPENAI_BASE_URL", "https://api.vultrinference.com/v1"),
)

# Backwards-compatible alias: older code reads ``OPENAI_BASE_URL``.
OPENAI_BASE_URL = OPENAI_API_BASE

VULTR_MODEL = os.environ.get("VULTR_MODEL", "deepseek-v4.1-flash-1017")
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))

AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "audit_log.jsonl")

# Sandbox resource caps. Consumed by ``sandbox._bwrap_command`` (``memory_mb``)
# and ``sandbox._spawn`` (``wall_clock_s``). Parsed at import so a malformed
# value fails fast at boot instead of surfacing as an ``AttributeError`` on the
# first tool call.
SANDBOX_LIMITS = {
    "memory_mb": int(os.environ.get("SANDBOX_MEMORY_MB", "256")),
    "wall_clock_s": int(os.environ.get("SANDBOX_WALL_CLOCK_S", "10")),
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# Two credentials, deliberately not interchangeable. ``PL_AGENT_TOKEN`` reaches
# ``/agent/run``; ``PL_OPERATOR_TOKEN`` reaches invoice ingest, approval and
# payout settlement. The split is what makes the human-approval gate real: if
# one token reached both, the agent could approve its own payout request.
#
# ``app.preflight_auth`` refuses to boot if either is unset, or if the two are
# equal - an equal pair would silently promote the agent credential to
# operator. Empty defaults are therefore safe: they fail closed at startup
# rather than opening the service.
PL_AGENT_TOKEN = os.environ.get("PL_AGENT_TOKEN", "")
PL_OPERATOR_TOKEN = os.environ.get("PL_OPERATOR_TOKEN", "")

# Identity recorded as the approver on ``/approvals/{id}/approve``. The token
# authorises the call; this names the human behind it, so the audit trail does
# not read "operator" for every approver in the organisation.
PL_OPERATOR_NAME = os.environ.get("PL_OPERATOR_NAME", "operator")

# When true, 5xx responses carry the internal ``detail`` string. Off by
# default: ``tools`` puts raw ``OSError`` text in ``ToolError.detail``, and
# that text contains host paths. Same reasoning as ``sandbox.py`` keeping
# stderr server-side.
PL_DEBUG = os.environ.get("PL_DEBUG", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
