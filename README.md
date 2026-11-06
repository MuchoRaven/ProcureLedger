# ProcureLedger

ProcureLedger is a zero-trust, sandboxed AI financial agent for accounts
payable. It automates procurement reconciliation — vendor checks, three-way
matching and payout approval requests — while keeping every state change on a
cryptographically linked audit chain and keeping money movement behind a human
gate the model cannot reach.

Four rules hold the design together:

1. **The model cannot move money.** It can open an approval gate; only a
   separate executor credential can settle it, and never the person who
   approved it.
2. **Every mutation is audited before it is applied.** A failed audit write
   leaves business state untouched, so the caller can retry.
3. **The audit chain is the source of truth.** It is replayed on boot to rebuild
   in-memory state, so a restart cannot resurrect a settled invoice.
4. **The sandbox is default-deny.** The model reaches exactly five tools, and
   each call is re-decided against live state before it is applied.

## Lifecycle

    ingest ──▶ match ──▶ request approval ──▶ approve ──▶ settle
    operator   agent      agent                operator    executor

An invoice is created by the operator, matched by the agent, escalated to a
human approval gate by the agent, approved by the operator, and settled by the
executor. The agent's credential cannot reach any step after "request approval".

## Running

    export PL_AGENT_TOKEN=...        # model-facing credential
    export PL_OPERATOR_TOKEN=...     # ingest + approve
    export PL_EXECUTOR_TOKEN=...     # settle
    export PL_OPERATOR_NAME=alice    # recorded as the approver
    export PL_EXECUTOR_NAME=bob      # recorded as the executor
    export OPENAI_API_KEY=...

    uvicorn app:app --workers 1

`--workers 1` is mandatory. The audit chain is process-local, and the lifespan
hook takes an exclusive `flock` on `<AUDIT_LOG_PATH>.lock`; a second worker
fails at boot rather than forking the chain.

## Authentication

Three bearer tokens, and they are not interchangeable:

| Credential | Role | Reaches |
|---|---|---|
| `PL_AGENT_TOKEN` | `agent` | `POST /agent/run` |
| `PL_OPERATOR_TOKEN` | `operator` | `POST /invoices`, `POST /approvals/{id}/approve` |
| `PL_EXECUTOR_TOKEN` | `executor` | `POST /approvals/{id}/execute` |

Read endpoints (`GET /invoices/{id}`, `GET /approvals/{id}`, `GET /audit`,
`GET /audit/verify`) accept any of the three. `GET /health` is unauthenticated.

`preflight_auth` runs at boot and refuses to start if any token is unset or if
any two are equal — an identical pair would collapse two roles into one. Tokens
are compared with `hmac.compare_digest` against every candidate, so a wrong
token does not leak which prefix matched.

The approver recorded on `/approvals/{id}/approve` is the authenticated
operator's `sub` (`PL_OPERATOR_NAME`), not the request body. A body `approver`
that disagrees is rejected with 403 rather than silently ignored.

## Endpoints

| Method | Path | Credential |
|---|---|---|
| GET | `/health` | none |
| POST | `/agent/run` | agent |
| GET | `/invoices/{invoice_id}` | any |
| POST | `/invoices` | operator |
| GET | `/approvals/{approval_id}` | any |
| POST | `/approvals/{approval_id}/approve` | operator |
| POST | `/approvals/{approval_id}/execute` | executor |
| GET | `/audit` | any |
| GET | `/audit/verify` | any |

## The agent

The model reaches tools only through a default-deny allowlist derived from
`decisions.PROPOSAL_KINDS`:

- `lookup_vendor`
- `get_invoice`
- `run_three_way_match`
- `request_payout_approval`
- `log_audit_event`

Everything else in `tools.py` is app-layer only and unreachable by the model,
even though it is a public function: `ingest_invoice`, `approve_payout`,
`_execute_payout`, `get_approval`, `verify_audit_chain`.

Every allowlisted call is executed through `sandbox.dispatch`, which re-decides
the call against a read-only snapshot and then against live state before
applying the verified proposal. The authenticated `Principal` is threaded from
the HTTP layer into the sandbox envelope; it is never taken from the model's
tool arguments.

Each call is bracketed by `TOOL_CALL_STARTED` / `TOOL_CALL_COMPLETED` links. If
the opening link cannot be written the call is aborted — an unrecorded action
defeats the point of the chain. If the closing link fails the tool has already
run, so the result is returned with an `audit_error` field.

## Audit chain

`audit_log.jsonl` is an append-only hash chain. Each link carries `seq`, `ts`,
`event_type`, `payload`, `correlation_id`, `prev_hash`, `source` and `hash`,
where `hash` is SHA-256 over the canonical JSON of the other fields (including
`source` when present, so links written before the field existed still verify).
Links are written with `flush` + `fsync` before the tool call returns.

`source` is `"tool"` for control-plane links and `"agent"` for model reasoning
notes. `log_audit_event` refuses the tool-authored event types when
`source="agent"`, so a model note cannot masquerade as a real approval gate or
settlement.

**Ordering.** Every mutating tool routes its state change through `_commit`,
which writes the audit link *first* and only then applies the mutation, under a
lock. A failed write leaves business state untouched. If the mutation itself
raises, the in-memory chain and the on-disk link are rolled back together.

**Replay.** On import, `_recover_audit_state` loads the log and replays
`INVOICE_INGESTED`, `THREE_WAY_MATCH`, `PAYOUT_APPROVAL_REQUESTED`,
`PAYOUT_APPROVED` and `PAYOUT_EXECUTED` in `seq` order to rebuild `_INVOICES`
and `_APPROVALS`. Replay is at-least-once and safe because every transition is
status-guarded: a link that would move an invoice backwards is skipped with a
warning rather than applied, so a restart cannot resurrect a paid invoice.

If the log cannot be parsed, read or replayed, the app **fails closed**: the
in-memory chain is left empty, business state resets to the seed literals, and
every subsequent write is refused with `AUDIT_LOG_CORRUPT` until an operator
repairs the log.

**Verification.** `GET /audit/verify` recomputes the chain and reports the first
divergence (`SEQ_GAP`, `PREV_HASH_MISMATCH`, `HASH_MISMATCH`). It verifies the
on-disk chain by default; pass `?from_file=false` to check the in-memory copy.

## Separation of duties

`tools._execute_payout` refuses when the executor equals the approver
(`SEPARATION_OF_DUTIES`, HTTP 409). The check runs under the state lock, so a
direct caller cannot bypass it. The executor is recorded in the
`PAYOUT_EXECUTED` payload.

## Configuration

| Setting | Purpose |
|---|---|
| `AUDIT_LOG_PATH` | Audit chain location. Empty disables persistence. |
| `AGENT_MAX_STEPS` | Loop cap. Clamped to a hard ceiling of 8. |
| `PL_DEBUG` | When set, 5xx `detail` is not redacted. Never enable in production. |
| `PL_AGENT_TOKEN` / `PL_OPERATOR_TOKEN` / `PL_EXECUTOR_TOKEN` | Bearer tokens. |
| `PL_OPERATOR_NAME` / `PL_EXECUTOR_NAME` | `sub` recorded for each role. |
| `OPENAI_API_KEY` | Provider key. |
| `OPENAI_API_BASE` / `OPENAI_BASE_URL` | Provider base URL. |
| `VULTR_MODEL` | Model name. |

`OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_BASE_URL` and `VULTR_MODEL` are
read from the environment first, then from `config.py`. The base URL and model
fall back to hardcoded defaults; a missing API key surfaces as
`CLIENT_INIT_FAILED` (HTTP 503).

## Error contract

Every failure returns one envelope:

    {"ok": false, "error": "CODE", "detail": "...", "correlation_id": "..."}

`detail` is blanked on 5xx unless `PL_DEBUG` is set, because the tool layer puts
raw `OSError` text there and that text contains host paths.

Status mapping: auth failures are 401/403, business and integrity errors map
through `_TOOL_ERROR_STATUS` (404 for missing records, 409 for illegal
transitions and separation-of-duties, 503 for audit-chain failures), and agent
client/model init failures are 503. Agent step-cap exhaustion is a 200 with
`ok: false` — the run completed, it just did not converge.

`X-Correlation-ID` is echoed on every response, including errors. The inbound
header is used when present, otherwise a UUID is generated once per request.

## Operational notes

- **`audit_log.jsonl` is runtime state and is gitignored.** A fresh clone must
  not start with a chain that is not its own. If a foreign or corrupt log is
  present at boot, the app fails closed rather than appending to it.
- **Run one worker.** See above.
- **The chain is process-local.** Multi-process or multi-host deployment needs
  an external append-only store; the current design assumes a single writer.

## Tests

    pytest

`test_sandbox_wiring.py` covers the sandbox wiring and skips when bubblewrap is
unavailable. `test_replay_and_auth.py` covers boot replay, separation of duties,
source labelling, principal threading and auth preflight, and needs no sandbox.
