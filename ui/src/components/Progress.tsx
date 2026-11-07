/**
 * A thin progress bar with an optional label.
 *
 * ``max`` of zero renders an empty bar rather than dividing by zero, which is
 * the state a freshly created PO is in before any receipt exists.
 */
export function Progress({
  value,
  max,
  label,
}: {
  value: number;
  max: number;
  label?: string;
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div className="progress">
      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      {label ? <div className="progress-label muted">{label}</div> : null}
    </div>
  );
}
