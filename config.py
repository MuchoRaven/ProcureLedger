import os

VULTR_API_KEY   = os.environ.get("VULTR_API_KEY", "")
VULTR_BASE_URL  = os.environ.get("VULTR_BASE_URL", "https://api.vultrinference.com/v1")
VULTR_MODEL     = os.environ.get("VULTR_MODEL", "llama-3.1-70b-instruct")
AGENT_MAX_STEPS = 8
