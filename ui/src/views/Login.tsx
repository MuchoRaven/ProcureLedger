import { useState, type FormEvent } from 'react';
import { describeError } from '../api/client';
import { useSession } from '../auth/session';

/**
 * The only screen that is not role-scoped.
 *
 * There is no "who am I" endpoint, so the console is chosen by probing the
 * token (``probeRole``). That costs three requests against the caller's
 * rate-limit budget, which is why it happens once here rather than on every
 * render.
 */
export function Login() {
  const { signIn } = useSession();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      await signIn(trimmed);
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
    }
  }

  const described = error ? describeError(error) : null;

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="brand brand-lg">
          <span className="brand-mark">PL</span>
          <span className="brand-name">ProcureLedger</span>
        </div>

        <p className="muted">
          Paste one of the three bearer tokens. The console is chosen from the
          token: agent, operator or executor.
        </p>

        <label className="field">
          <span>Bearer token</span>
          <input
            type="password"
            autoComplete="off"
            autoFocus
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="PL_AGENT_TOKEN / PL_OPERATOR_TOKEN / PL_EXECUTOR_TOKEN"
          />
        </label>

        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || !token.trim()}
        >
          {busy ? 'Connecting…' : 'Connect'}
        </button>

        {described ? (
          <div className="banner banner-error" role="alert">
            <div className="banner-head">
              <strong>{described.title}</strong>
            </div>
            {described.detail ? (
              <div className="banner-detail">{described.detail}</div>
            ) : null}
            {described.cid ? (
              <div className="banner-cid">
                correlation_id: {described.cid}
              </div>
            ) : null}
          </div>
        ) : null}

        <p className="muted small">
          The three credentials are not interchangeable. The agent token can
          only reach <span className="mono">/agent/run</span>; the executor
          token can only settle an approved payout. The operator token cannot
          settle, and the executor token cannot approve.
        </p>
      </form>
    </div>
  );
}
