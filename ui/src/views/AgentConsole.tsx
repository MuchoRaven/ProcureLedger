import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { ApiError } from '../api/client';
import { useApi } from '../api/hooks';
import type { AgentResult, AgentTranscriptEntry } from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner } from '../components/Feedback';

/**
 * The agent console.
 *
 * The agent is a restricted service account, not a chat assistant, and the UI
 * is built to say so. The constraints that make it safe - independent runs,
 * no memory, no authority to move money, every call sandboxed and audited -
 * are on screen rather than in a tooltip, because a friendly chat UI invites
 * casual use of a system that should be used deliberately.
 *
 * One run per message, not a conversation. ``agent.run_agent_cycle`` builds a
 * fresh ``[system, user]`` message list on every call, so the agent has no
 * memory of an earlier turn - the log below is a record of independent runs.
 *
 * The agent cannot move money. ``/agent/run`` is the only endpoint the agent
 * credential can reach; approval and settlement are separate credentials, and
 * ``ALLOWED_TOOLS`` is derived from ``decisions.PROPOSAL_KINDS``, so the model
 * cannot even see ``approve_payout`` or ``_execute_payout``.
 *
 * Two failure shapes, and they are not the same thing:
 *
 * * ``MAX_STEPS_EXCEEDED`` is a *completed* run - HTTP 200 with ``ok: false``
 *   and a transcript worth reading. ``call()`` throws on ``ok: false``, so the
 *   body is recovered from ``ApiError.body`` and rendered as a run.
 * * ``CLIENT_INIT_FAILED`` / ``MODEL_CALL_FAILED`` are 503s: the deployment is
 *   broken and there is nothing to show. They stay an error banner.
 *
 * The run is synchronous and can take several model round-trips, so the turn
 * is appended immediately in a pending state rather than leaving the composer
 * looking inert.
 */

type Turn = {
  id: number;
  message: string;
  pending: boolean;
  result: AgentResult | null;
  error: Error | null;
};

/**
 * Example prompts. Each one is a real, valid call against the seed data:
 * INV-2002 is RECEIVED and matches PO-1002, SKU-HYD-001 has three live
 * quotes, INV-2004 is in EXCEPTION with a past due date, and VEND-004 is
 * HIGH risk.
 */
const SUGGESTIONS = [
  'Run a three-way match on INV-2002 against PO-1002.',
  'Compare vendors for SKU-HYD-001.',
  'What is the aging bucket for INV-2004?',
  'Look up vendor VEND-004 and its risk tier.',
];

/**
 * The hard ceiling on loop iterations, from ``agent._HARD_MAX_STEPS``.
 *
 * The deployment may configure a lower cap via ``AGENT_MAX_STEPS``; this is
 * the absolute maximum, so "N of 8" is always true even when the run stopped
 * earlier at a lower configured cap.
 */
const STEP_CAP = 8;

/**
 * Recover a completed run from the error ``call()`` threw.
 *
 * Only ``MAX_STEPS_EXCEEDED`` qualifies. A 503 also carries a ``transcript``
 * field (an empty one), so testing for the field alone would render a broken
 * deployment as a run that did nothing.
 */
function stepCapResult(err: unknown): AgentResult | null {
  if (!(err instanceof ApiError)) return null;
  if (err.code !== 'MAX_STEPS_EXCEEDED') return null;
  const body = err.body as AgentResult | null;
  if (!body || !Array.isArray(body.transcript)) return null;
  return body;
}

function toolOk(result: Record<string, unknown> | null | undefined): boolean {
  return result?.ok === true;
}

function toolError(result: Record<string, unknown> | null | undefined): string {
  const code = result?.error;
  return typeof code === 'string' ? code : '';
}

/**
 * A read whose live data diverged from the sandbox snapshot.
 *
 * ``sandbox._redeci`` sets ``stale: true`` when a read's proposal differs from
 * the live re-decision. The answer is still applied, but it may be out of
 * date, and that is worth showing rather than dropping.
 */
function isStale(result: Record<string, unknown> | null | undefined): boolean {
  return result?.stale === true;
}

/**
 * The audit state of one tool call.
 *
 * ``agent.dispatch_tool`` brackets every call with ``TOOL_CALL_STARTED`` and
 * ``TOOL_CALL_COMPLETED``. Three outcomes are possible:
 *
 * * ``logged``  - both brackets were written.
 * * ``gap``     - the call ran but the closing bracket failed; the result
 *                 carries ``audit_error``.
 * * ``aborted`` - the opening bracket failed, so the call was refused before
 *                 it ran; the result is ``AUDIT_WRITE_FAILED``.
 */
type AuditState = 'logged' | 'gap' | 'aborted';

function auditState(
  result: Record<string, unknown> | null | undefined,
): AuditState {
  if (result?.error === 'AUDIT_WRITE_FAILED') return 'aborted';
  if (typeof result?.audit_error === 'string') return 'gap';
  return 'logged';
}

type ChipTone = 'ok' | 'error' | 'warn';

/**
 * The chip for one tool call: its tone, its label, and an optional tooltip.
 *
 * The label carries both the result and the audit state, so the chain status
 * is visible rather than assumed. Priority: aborted > gap > stale > result.
 */
function chipFor(entry: AgentTranscriptEntry): {
  tone: ChipTone;
  label: string;
  title?: string;
} {
  const state = auditState(entry.result);
  const ok = toolOk(entry.result);
  const err = toolError(entry.result);
  const stale = isStale(entry.result);
  const resultLabel = ok ? 'ok' : err || 'error';

  if (state === 'aborted') {
    return {
      tone: 'error',
      label: 'not logged',
      title:
        'The opening audit link failed, so the call was refused before it ran.',
    };
  }
  if (state === 'gap') {
    return {
      tone: 'warn',
      label: `${resultLabel} · audit gap`,
      title: 'The call ran but the closing audit link failed.',
    };
  }
  if (stale) {
    return {
      tone: 'warn',
      label: `${resultLabel} · stale`,
      title:
        'Live data diverged from the sandbox snapshot; the answer may be out of date.',
    };
  }
  return {
    tone: ok ? 'ok' : 'error',
    label: `${resultLabel} · logged`,
  };
}

export function AgentConsole() {
  const { request } = useApi();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [running, setRunning] = useState(false);
  const [selected, setSelected] = useState<{
    entry: AgentTranscriptEntry;
    step: number;
  } | null>(null);
  const nextId = useRef(1);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || running) return;

    const id = nextId.current++;
    setDraft('');
    setRunning(true);
    setTurns((prev) => [
      ...prev,
      { id, message: trimmed, pending: true, result: null, error: null },
    ]);

    try {
      const result = await request<AgentResult>('/agent/run', {
        method: 'POST',
        body: { message: trimmed },
      });
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === id ? { ...turn, pending: false, result } : turn,
        ),
      );
    } catch (err) {
      const capped = stepCapResult(err);
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === id
            ? {
                ...turn,
                pending: false,
                result: capped,
                error: capped ? null : (err as Error),
              }
            : turn,
        ),
      );
    } finally {
      setRunning(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(draft);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      void send(draft);
    }
  }

  function clear() {
    setTurns([]);
    setSelected(null);
  }

  return (
    <div className="chat">
      <div className="banner banner-info">
        <strong>Restricted service account.</strong> Independent runs, no
        memory between messages. Cannot approve, settle, or ingest invoices.
        Every tool call runs in a sandbox and is bracketed in the audit chain.
      </div>

      <div className="toolbar">
        <span className="muted small">
          {running ? 'A run is in progress.' : 'Ready.'}
        </span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          disabled={running || turns.length === 0}
          onClick={clear}
        >
          Clear log
        </button>
      </div>

      {turns.length === 0 ? (
        <div className="chat-empty">
          <div className="chat-empty-title">ProcureLedger agent</div>
          <div className="chat-empty-sub">
            A restricted service account for accounts-payable work. It can
            read vendors, invoices, POs and requisitions; run three-way
            matches; compare vendors; create POs and record receipts; and
            request a payout approval. It cannot approve, settle, or ingest
            invoices — those are separate credentials.
          </div>
          <div className="chat-empty-sub">
            Each message is an independent run: the agent starts from its
            system prompt and this message, with no memory of an earlier turn.
            Every tool call runs in a sandbox and is bracketed in the audit
            chain.
          </div>
          <div className="chat-empty-label">Example calls</div>
          <div className="chat-suggestions">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                className="chat-suggestion"
                onClick={() => void send(s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="chat-log">
          {turns.map((turn) => (
            <TurnCard
              key={turn.id}
              turn={turn}
              onSelect={(entry, step) => setSelected({ entry, step })}
            />
          ))}
        </div>
      )}

      <form className="composer" onSubmit={submit}>
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Describe the call to make. Each message is a fresh run."
          rows={2}
          spellCheck={false}
        />
        <div className="composer-actions">
          <span className="composer-hint">Ctrl/⌘ + Enter to run</span>
          <button
            type="submit"
            className="btn btn-sm btn-primary"
            disabled={running || !draft.trim()}
          >
            {running ? 'Running…' : 'Run'}
          </button>
        </div>
      </form>

      {selected ? (
        <Drawer
          title={`${selected.entry.tool} · call ${selected.step}`}
          onClose={() => setSelected(null)}
        >
          <div className="stack">
            <section className="panel">
              <header className="panel-head">
                <h2>Arguments</h2>
              </header>
              <div className="content">
                <pre className="mono">
                  {JSON.stringify(selected.entry.arguments, null, 2)}
                </pre>
              </div>
            </section>
            <section className="panel">
              <header className="panel-head">
                <h2>Result</h2>
              </header>
              <div className="content">
                <pre className="mono">
                  {JSON.stringify(selected.entry.result, null, 2)}
                </pre>
              </div>
            </section>
          </div>
        </Drawer>
      ) : null}
    </div>
  );
}

function TurnCard({
  turn,
  onSelect,
}: {
  turn: Turn;
  onSelect: (entry: AgentTranscriptEntry, step: number) => void;
}) {
  return (
    <div className="turn">
      <div className="turn-block turn-user">
        <div className="turn-role">You</div>
        <div className="turn-reply">{turn.message}</div>
      </div>

      <div className="turn-block turn-agent">
        <div className="turn-role">Agent</div>
        {turn.pending ? (
          <div className="turn-pending">Running…</div>
        ) : turn.error ? (
          <ErrorBanner error={turn.error} />
        ) : turn.result ? (
          <RunResult result={turn.result} onSelect={onSelect} />
        ) : null}
      </div>
    </div>
  );
}

function RunResult({
  result,
  onSelect,
}: {
  result: AgentResult;
  onSelect: (entry: AgentTranscriptEntry, step: number) => void;
}) {
  const calls = result.transcript.length;
  const staleCount = result.transcript.filter((entry) =>
    isStale(entry.result),
  ).length;

  return (
    <div className="stack">
      {result.ok ? (
        <div className="turn-reply">
          {result.reply ? (
            result.reply
          ) : (
            <span className="muted">(empty reply)</span>
          )}
        </div>
      ) : (
        <div className="banner banner-error" role="alert">
          <div className="banner-head">
            <strong>{result.error || 'AGENT_ERROR'}</strong>
          </div>
          <div className="banner-detail">
            {result.error === 'MAX_STEPS_EXCEEDED'
              ? `The run hit the ${result.steps}-step cap without a final answer. The transcript below is what it did.`
              : result.detail || 'The run did not complete.'}
          </div>
        </div>
      )}

      <div className="run-meta">
        <span
          title={`The loop is capped at ${STEP_CAP} steps (the hard ceiling; the deployment may configure a lower cap).`}
        >
          {result.steps} of {STEP_CAP} steps
        </span>
        <span className="run-meta-dot" />
        <span>
          {calls} tool call{calls === 1 ? '' : 's'}
        </span>
        {staleCount > 0 ? (
          <>
            <span className="run-meta-dot" />
            <span className="run-meta-warn">{staleCount} stale</span>
          </>
        ) : null}
      </div>

      {calls > 0 ? (
        <div className="tool-chips">
          {result.transcript.map((entry, index) => {
            const chip = chipFor(entry);
            return (
              <button
                key={index}
                type="button"
                className={`tool-chip tool-chip-${chip.tone}`}
                onClick={() => onSelect(entry, index + 1)}
                title={chip.title}
              >
                <span className="tool-chip-dot" />
                <span>{entry.tool}</span>
                <span className="muted">· {chip.label}</span>
              </button>
            );
          })}
        </div>
      ) : null}

      {result.correlation_id ? (
        <div className="run-cid">
          correlation_id: {result.correlation_id}
        </div>
      ) : null}
    </div>
  );
}
