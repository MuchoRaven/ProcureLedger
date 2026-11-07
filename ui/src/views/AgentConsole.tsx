import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { ApiError } from '../api/client';
import { useApi } from '../api/hooks';
import type { AgentResult, AgentTranscriptEntry } from '../api/types';
import { Drawer } from '../components/Drawer';
import { Empty, ErrorBanner } from '../components/Feedback';

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

  async function send() {
    const text = draft.trim();
    if (!text || running) return;

    const id = nextId.current++;
    setDraft('');
    setRunning(true);
    setTurns((prev) => [
      ...prev,
      { id, message: text, pending: true, result: null, error: null },
    ]);

    try {
      const result = await request<AgentResult>('/agent/run', {
        method: 'POST',
        body: { message: text },
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
    void send();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      void send();
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
        <Empty>
          No runs yet. Ask the agent to inspect a vendor, run a three-way match,
          or open a payout approval.
        </Empty>
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
          placeholder="e.g. Run a three-way match on INV-2001 against PO-1001."
          rows={3}
          spellCheck={false}
        />
        <div className="composer-actions">
          <span className="muted small">Ctrl/⌘ + Enter to send</span>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={running || !draft.trim()}
          >
            {running ? 'Running…' : 'Send'}
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
        <div className="bubble-role">You</div>
        <div className="bubble-body">{turn.message}</div>
      </div>

      <div className="bubble bubble-agent">
        <div className="bubble-role">Agent</div>
        {turn.pending ? (
          <div className="bubble-body muted">Running…</div>
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

      <div className="muted small">
        {result.steps} step{result.steps === 1 ? '' : 's'} · {calls} tool call
        {calls === 1 ? '' : 's'}
      </div>

      {calls > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>Tool</th>
                <th>Result</th>
                <th>Audit</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {result.transcript.map((entry, index) => {
                const gap = auditGap(entry.result);
                return (
                  <tr key={index}>
                    <td className="num mono">{index + 1}</td>
                    <td className="mono">{entry.tool}</td>
                    <td>
                      {toolOk(entry.result) ? (
                        <span className="chip chip-ok">ok</span>
                      ) : (
                        <span className="chip chip-danger">
                          {toolError(entry.result) || 'error'}
                        </span>
                      )}
                    </td>
                    <td>
                      {gap ? (
                        <span className="chip chip-warn" title={gap}>
                          audit gap
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="actions">
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => onSelect(entry, index + 1)}
                      >
                        View
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
