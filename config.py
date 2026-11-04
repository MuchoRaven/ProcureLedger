import os

OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.vultrinference.com/v1")
VULTR_MODEL     = os.environ.get("VULTR_MODEL", "llama-3.1-70b-instruct")
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))

AUDIT_LOG_PATH  = os.environ.get("AUDIT_LOG_PATH", "audit_log.jsonl")
