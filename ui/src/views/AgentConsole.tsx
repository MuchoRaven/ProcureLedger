import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { ApiError } from '../api/client';
import { useApi } from '../api/hooks';
import type { AgentResult, AgentTranscriptEntry } from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner } from '../components/Feedback';

/**
 * The agent console.
 *
 * One run per message, not a conversation. ``agent.run_agent_cycle`` builds a
 * fresh ``[system, user]`` message list on every call, so the agent has no
 * memory of an earlier turn - the log below is a record of independent runs,
 * and the toolbar says so rather than letting the transcript imply otherwise.
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
 * The closing audit link failed after the tool already ran.
 *
 * ``agent.dispatch_tool`` keeps the result and attaches ``audit_error`` rather
 * than discarding it, so the call happened but the chain has a gap. That is
 * worth a chip of its own - it is neither a success nor a tool failure.
 */
function auditGap(result: Record<string, unknown> | null | undefined): string {
  const gap = result?.audit_error;
  return typeof gap === 'string' ? gap : '';
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
      <div className="toolbar">
        <span className="muted small">
          Each run is independent: the agent starts from its system prompt and
          this message, with no memory of an earlier turn. It cannot move money
          - approval and settlement are separate credentials.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          disabled={running || turns.length === 0}
          onClick={clear}
        >
          Clear
        </button>
      </div>

      {turns.length === 0 ? (
        <div className="chat-empty">
          <div className="chat-empty-avatar">PL</div>
          <div className="chat-empty-title">Ask ProcureLedger</div>
          <div className="chat-empty-sub">
            Inspect vendors, run three-way matches, or open a payout approval.
            The agent cannot move money - settlement is a separate executor
            action.
          </div>
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
          placeholder="Ask the agent to inspect a vendor, run a three-way match, or open a payout approval…"
          rows={2}
          spellCheck={false}
        />
        <div className="composer-actions">
          <span className="composer-hint">Ctrl/⌘ + Enter to send</span>
          <button
            type="submit"
            className="composer-send"
            disabled={running || !draft.trim()}
            aria-label="Send"
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M2.5 8h11" />
              <path d="m9 3.5 4.5 4.5L9 12.5" />
            </svg>
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
      <div className="bubble bubble-user">
        <div className="bubble-avatar bubble-avatar-user" aria-hidden="true">
          <svg
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="8" cy="5.5" r="2.5" />
            <path d="M3 13.5c0-2.5 2.2-4 5-4s5 1.5 5 4" />
          </svg>
        </div>
        <div className="bubble-content">
          <div className="bubble-role">You</div>
          <div className="bubble-body">{turn.message}</div>
        </div>
      </div>

      <div className="bubble bubble-agent">
        <div className="bubble-avatar bubble-avatar-agent" aria-hidden="true">
          PL
        </div>
        <div className="bubble-content">
          <div className="bubble-role">Agent</div>
          {turn.pending ? (
            <div className="bubble-body muted">
              <span className="typing">
                <span />
                <span />
                <span />
              </span>
            </div>
          ) : turn.error ? (
            <ErrorBanner error={turn.error} />
          ) : turn.result ? (
            <RunResult result={turn.result} onSelect={onSelect} />
          ) : null}
        </div>
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

  return (
    <div className="stack">
      {result.ok ? (
        <div className="bubble-body">
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
          {result.correlation_id ? (
            <div className="banner-cid">
              correlation_id: {result.correlation_id}
            </div>
          ) : null}
        </div>
      )}

      <div className="run-meta">
        <span>
          {result.steps} step{result.steps === 1 ? '' : 's'}
        </span>
        <span className="run-meta-dot" />
        <span>
          {calls} tool call{calls === 1 ? '' : 's'}
        </span>
      </div>

      {calls > 0 ? (
        <div className="tool-chips">
          {result.transcript.map((entry, index) => {
            const gap = auditGap(entry.result);
            const ok = toolOk(entry.result);
            const err = toolError(entry.result);
            const tone = gap ? 'warn' : ok ? 'ok' : 'error';
            const label = gap ? 'audit gap' : ok ? 'ok' : err || 'error';
            return (
              <button
                key={index}
                type="button"
                className={`tool-chip tool-chip-${tone}`}
                onClick={() => onSelect(entry, index + 1)}
                title={gap || err || undefined}
              >
                <span className="tool-chip-dot" />
                <span>{entry.tool}</span>
                <span className="muted">· {label}</span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
