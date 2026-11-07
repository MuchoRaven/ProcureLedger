/**
 * Placeholder for a view that has not been built yet.
 *
 * Every nav entry routes to a real view as of Phase 5, so this is only
 * reachable through ``App``'s default case - a stale or unknown view id. It
 * states which phase the view would land in and what it would do, so a blank
 * shell is never the answer.
 */
export function ComingSoon({
  label,
  phase,
  note,
}: {
  label: string;
  phase: string;
  note: string;
}) {
  return (
    <div className="stack">
      <div className="banner banner-info">
        <div className="banner-head">
          <strong>
            {label} lands in {phase}
          </strong>
        </div>
        <div className="banner-detail">{note}</div>
      </div>

      <section className="panel">
        <header className="panel-head">
          <h2>What is already wired</h2>
        </header>
        <div className="content">
          <p className="muted">
            The session, the role probe and the API client are live. Every
            request from this console carries the bearer token and a
            client-generated <span className="mono">X-Correlation-ID</span>, and
            a 200 with <span className="mono">ok: false</span> is treated as a
            failure rather than a success.
          </p>
        </div>
      </section>
    </div>
  );
}
