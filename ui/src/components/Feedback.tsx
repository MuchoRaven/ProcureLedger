import type { ReactNode } from 'react';
import { describeError } from '../api/client';

/**
 * The one place an error is rendered.
 *
 * ``detail`` is omitted when empty rather than shown as a blank line: the
 * server blanks it on 5xx unless ``PL_DEBUG`` is set, so an empty detail is
 * expected and must not read as a bug. The ``correlation_id`` is always shown
 * when present - it is the only handle support has.
 */
export function ErrorBanner({
  error,
  onRetry,
}: {
  error: Error | null;
  onRetry?: () => void;
}) {
  if (!error) return null;
  const { title, detail, cid } = describeError(error);
  return (
    <div className="banner banner-error" role="alert">
      <div className="banner-head">
        <strong>{title}</strong>
        {onRetry ? (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={onRetry}
          >
            Retry
          </button>
        ) : null}
      </div>
      {detail ? <div className="banner-detail">{detail}</div> : null}
      {cid ? <div className="banner-cid">correlation_id: {cid}</div> : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}
