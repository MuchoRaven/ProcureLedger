/**
 * Placeholder for a view that has not been built yet.
 *
 * It states which phase the view lands in and what it will do, so the shell
 * can be clicked through end to end before any data view exists. Phase 3
 * replaces the operator entries, Phase 4 the executor queue, Phase 5 the agent
 * console.
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
