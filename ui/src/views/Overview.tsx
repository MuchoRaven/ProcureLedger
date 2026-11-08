import type { ReactNode } from 'react';
import { useApi, useResource } from '../api/hooks';
import type {
  ApprovalListResponse,
  LatePOListResponse,
  PurchaseListResponse,
  VerifyResult,
} from '../api/types';
import { ErrorBanner, Loading } from '../components/Feedback';
import { money } from '../lib/format';

/**
 * The operator landing page.
 *
 * Four reads are issued in parallel and polled every 30s. ``/audit/verify``
 * reports a broken chain as a 200 with ``valid: false``, so it never rejects
 * the whole ``Promise.all``; the other three are operator-accessible reads.
 *
 * The cards are buttons rather than links: the shell has no router, so
 * navigation is a callback into ``App``'s active-view state.
 *
 * This is the one view rendered in the dark control-plane theme. The styling
 * lives in ``styles-dark.css`` and is scoped to ``.ov-root`` / ``.ov-*``, so
 * nothing here leaks into the other views. The fixed footer is rendered by
 * this view rather than by ``Layout`` for the same reason: it must appear on
 * Overview and nowhere else.
 */

type Tone = 'ok' | 'warn' | 'danger' | 'accent' | 'muted';

/**
 * Map a PO status to a pill tone.
 *
 * Deliberately not ``lib/format.statusTone``: that helper is tuned for the
 * light theme's chip palette, and this view uses the high-contrast dark pills
 * (emerald for settled states, amber for anything needing attention).
 */
function purchaseTone(status: string | null | undefined): Tone {
  switch ((status || '').toUpperCase()) {
    case 'CLOSED':
    case 'RECEIVED':
    case 'MATCHED':
      return 'ok';
    case 'CANCELLED':
      return 'danger';
    case 'OPEN':
      return 'accent';
    default:
      return 'muted';
  }
}

function Chip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`ov-chip ov-chip-${tone}`}>{children}</span>;
}

export function Overview({ onNavigate }: { onNavigate: (id: string) => void }) {
  const { request } = useApi();
  const { data, error, loading, refreshing, refresh } = useResource(
    'overview',
    async () => {
      const [purchases, late, approvals, verify] = await Promise.all([
        request<PurchaseListResponse>('/purchases'),
        request<LatePOListResponse>('/followup'),
        request<ApprovalListResponse>('/approvals'),
        request<VerifyResult>('/audit/verify'),
      ]);
      return { purchases, late, approvals, verify };
    },
    { interval: 30000 },
  );

  const audit = data?.verify ?? null;
  // Every response echoes the correlation id the server used. Preferring the
  // verify call means the footer names the request that produced the chain
  // status shown next to it.
  const cid =
    data?.verify.correlation_id ??
    data?.purchases.correlation_id ??
    data?.late.correlation_id ??
    data?.approvals.correlation_id ??
    '';

  const footer = (
    <footer className="ov-footer">
      <span className="ov-footer-item">
        <span
          className={`ov-dot ${
            audit
              ? audit.valid
                ? 'ov-dot-ok'
                : 'ov-dot-danger'
              : 'ov-dot-muted'
          }`}
        />
        hash chain:{' '}
        {audit
          ? audit.valid
            ? `verified · ${audit.entries} entries`
            : `broken at seq ${audit.broken_at ?? '?'}`
          : 'checking…'}
      </span>
      <span className="ov-footer-item ov-footer-cid">
        correlation_id: {cid || '—'}
      </span>
    </footer>
  );

  if (loading) {
    return (
      <div className="ov-root stack">
        <Loading />
        {footer}
      </div>
    );
  }

  if (error) {
    return (
      <div className="ov-root stack">
        <ErrorBanner error={error} onRetry={refresh} />
        {footer}
      </div>
    );
  }

  if (!data) return null;

  const outstanding = data.purchases.purchases.filter((p) => p.is_outstanding);
  const lateAmongOutstanding = outstanding.filter((p) => p.is_late).length;
  const pending = data.approvals.approvals.filter((a) => a.status === 'PENDING');
  const late = data.late.pos;

  return (
    <div className="ov-root stack">
      <div className="toolbar">
        <span className="muted small">
          {refreshing ? 'Refreshing…' : 'Polled every 30s'}
        </span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      <div className="cards">
        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('purchases')}
        >
          <span className="card-label">Outstanding POs</span>
          <span className="card-value">{outstanding.length}</span>
          <span className="card-foot">
            {lateAmongOutstanding > 0 ? (
              <Chip tone="warn">{lateAmongOutstanding} late</Chip>
            ) : (
              <span className="muted">on schedule</span>
            )}
          </span>
        </button>

        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('followup')}
        >
          <span className="card-label">Late POs</span>
          <span className="card-value">{late.length}</span>
          <span className="card-foot muted mono">as of {data.late.as_of}</span>
        </button>

        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('approvals')}
        >
          <span className="card-label">Pending approvals</span>
          <span className="card-value">{pending.length}</span>
          <span className="card-foot">
            {pending.length > 0 ? (
              <Chip tone="warn">awaiting decision</Chip>
            ) : (
              <span className="muted">queue clear</span>
            )}
          </span>
        </button>

        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('audit')}
        >
          <span className="card-label">Audit chain</span>
          <span
            className={`card-value ${
              audit?.valid ? 'ov-value-ok' : 'ov-value-danger'
            }`}
          >
            {audit?.valid ? 'OK' : 'BROKEN'}
          </span>
          <span className="card-foot muted mono">
            {audit ? `${audit.entries} entries` : '—'}
          </span>
        </button>
      </div>

      <section className="panel">
        <header className="panel-head">
          <h2>Outstanding purchases</h2>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={() => onNavigate('purchases')}
          >
            View all
          </button>
        </header>
        <div className="content">
          {outstanding.length === 0 ? (
            <div className="empty">
              Every PO is fully received and invoiced.
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>PO</th>
                  <th>Vendor</th>
                  <th>Status</th>
                  <th className="num">Amount</th>
                  <th className="num">Outstanding</th>
                  <th>Expected</th>
                  <th>Late</th>
                </tr>
              </thead>
              <tbody>
                {outstanding.slice(0, 8).map((p) => (
                  <tr key={p.po_id}>
                    <td className="mono">{p.po_id}</td>
                    <td>{p.vendor_name || p.vendor_id}</td>
                    <td>
                      <Chip tone={purchaseTone(p.status)}>{p.status}</Chip>
                    </td>
                    <td className="num mono">
                      {money(p.amount, p.currency)}
                    </td>
                    <td className="num mono">
                      {money(p.outstanding_receipt, p.currency)}
                    </td>
                    <td className="mono">{p.expected_delivery || '—'}</td>
                    <td>
                      {p.is_late ? (
                        <Chip tone="warn">{p.days_late}d</Chip>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {footer}
    </div>
  );
}
