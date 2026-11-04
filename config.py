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
