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

# Minimum free space (MiB) required on the filesystem holding the audit log.
# ``sandbox.preflight`` refuses to boot below this, and ``app./ready`` re-checks
# it on every probe. Audit-before-mutation means a failed append refuses the
# mutation, so a full disk stops the service; it is better to stop at boot with
# a clear message than mid-request with an OSError.
PL_AUDIT_MIN_FREE_MB = int(os.environ.get("PL_AUDIT_MIN_FREE_MB", "64"))

# Audit-log recovery policy when the durable chain is unreadable or fails
# verification at boot.
#
# ``fail`` (default) refuses to start. A tampered or truncated chain is an
# integrity failure, and continuing would fork it: the next append would write
# ``seq=1`` with ``prev_hash=GENESIS_HASH`` on top of a file that already holds
# entries, producing duplicate sequence numbers and a broken link.
#
# ``quarantine`` moves the bad file aside, starts a fresh chain, and records a
# ``CHAIN_RESET`` entry naming the quarantine path. Use it to recover a host
# without hand-editing the log; the reset is itself auditable.
PL_AUDIT_RECOVERY = os.environ.get("PL_AUDIT_RECOVERY", "fail").strip().lower()

# Sandbox resource caps. Consumed by ``sandbox._bwrap_command`` (``memory_mb``)
# and ``sandbox._spawn`` (``wall_clock_s``). Parsed at import so a malformed
# value fails fast at boot instead of surfacing as an ``AttributeError`` on the
# first tool call.
SANDBOX_LIMITS = {
    "memory_mb": int(os.environ.get("SANDBOX_MEMORY_MB", "256")),
    "wall_clock_s": int(os.environ.get("SANDBOX_WALL_CLOCK_S", "10")),
}

# Per-principal request budget. ``PL_RATE_LIMIT_PER_MIN`` is the sustained
# rate; ``PL_RATE_LIMIT_BURST`` is the bucket depth, i.e. how many requests may
# arrive back-to-back before the sustained rate applies. Set
# ``PL_RATE_LIMIT_PER_MIN`` to 0 to disable limiting.
#
# The limiter is in-process, which is correct only because the app is pinned to
# one worker (see ``app._acquire_single_worker_lock``). With N workers each
# would enforce its own budget and the effective limit would be N times this.
PL_RATE_LIMIT_PER_MIN = int(os.environ.get("PL_RATE_LIMIT_PER_MIN", "120"))
PL_RATE_LIMIT_BURST = int(os.environ.get("PL_RATE_LIMIT_BURST", "20"))

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# Three credentials, deliberately not interchangeable. ``PL_AGENT_TOKEN``
# reaches ``/agent/run``; ``PL_OPERATOR_TOKEN`` reaches invoice ingest and
# approval; ``PL_EXECUTOR_TOKEN`` reaches payout settlement. The split is what
# makes the human-approval gate real: if one token reached both, the agent
# could approve its own payout request, and the same operator could approve and
# settle.
#
# ``app.preflight_auth`` refuses to boot if any is unset, or if any two are
# equal - an equal pair would silently collapse two roles into one. Empty
# defaults are therefore safe: they fail closed at startup rather than opening
# the service.
PL_AGENT_TOKEN = os.environ.get("PL_AGENT_TOKEN", "")
PL_OPERATOR_TOKEN = os.environ.get("PL_OPERATOR_TOKEN", "")
PL_EXECUTOR_TOKEN = os.environ.get("PL_EXECUTOR_TOKEN", "")

# Identity recorded as the approver / executor. The token authorises the call;
# these name the human behind it, so the audit trail does not read "operator"
# for every approver in the organisation.
PL_OPERATOR_NAME = os.environ.get("PL_OPERATOR_NAME", "operator")
PL_EXECUTOR_NAME = os.environ.get("PL_EXECUTOR_NAME", "executor")

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
