import type { ReactNode } from 'react';
import { useApi, useResource } from '../api/hooks';
import type {
  ApprovalListResponse,
  AuditPageResponse,
  LatePOListResponse,
  PurchaseListResponse,
  RFQListResponse,
  VerifyResult,
} from '../api/types';
import { ErrorBanner, Loading } from '../components/Feedback';
import { isoDate, money, timestamp } from '../lib/format';

/**
 * The operator landing page, laid out as a three-column control plane.
 *
 * Column 1 is the shell's fixed sidebar (rendered by ``Layout``). Column 2 is
 * the primary work surface: the pending-approvals queue, with the outstanding
 * purchases table beneath it. Column 3 is a stack of cards, each wired to a
 * real backend module rather than a placeholder:
 *
 * * match & approval metrics come from ``/purchases`` and ``/approvals``;
 * * the sourcing card summarises ``/rfqs`` (``_tools_sourcing``);
 * * the follow-up card summarises ``/followup`` (``_followup``);
 * * the recently-completed card reads the tail of ``/audit``, i.e. the
 *   immutable hash-chain ledger.
 *
 * Nothing here is invented: every number is derived from a response the
 * backend actually returns, and a card with no data says so rather than
 * showing a zero that looks like a real measurement.
 *
 * The dark theme lives in ``styles-dark.css`` and is scoped to ``.ov-root`` /
 * ``.ov-*`` / ``.shell-dark``, so no other view changes appearance. The fixed
 * footer is rendered here rather than by ``Layout`` for the same reason: it
 * must appear on Overview and nowhere else.
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

/** How many audit entries the "Recently completed" card shows. */
const RECENT_LIMIT = 6;

export function Overview({ onNavigate }: { onNavigate: (id: string) => void }) {
  const { request } = useApi();
  const { data, error, loading, refreshing, refresh } = useResource(
    'overview',
    async () => {
      const [purchases, late, approvals, verify, rfqs, auditHead] =
        await Promise.all([
          request<PurchaseListResponse>('/purchases'),
          request<LatePOListResponse>('/followup'),
          request<ApprovalListResponse>('/approvals'),
          request<VerifyResult>('/audit/verify'),
          request<RFQListResponse>('/rfqs'),
          // ``/audit`` pages from the start of the chain, so the tail has to
          // be located from ``total`` first. One extra request buys the
          // *latest* entries rather than the oldest.
          request<AuditPageResponse>('/audit?limit=1&offset=0'),
        ]);

      const offset = Math.max(0, auditHead.total - RECENT_LIMIT);
      const recent = await request<AuditPageResponse>(
        `/audit?limit=${RECENT_LIMIT}&offset=${offset}`,
      );

      return { purchases, late, approvals, verify, rfqs, recent };
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

  const purchases = data.purchases.purchases;
  const outstanding = purchases.filter((p) => p.is_outstanding);
  const fullyReceived = purchases.filter((p) => p.is_fully_received).length;
  const fullyInvoiced = purchases.filter((p) => p.is_fully_invoiced).length;
  const lateAmongOutstanding = outstanding.filter((p) => p.is_late).length;

  const approvals = data.approvals.approvals;
  const pending = approvals.filter((a) => a.status === 'PENDING');
  const approved = approvals.filter((a) => a.status === 'APPROVED');
  const executed = approvals.filter((a) => a.status === 'EXECUTED');

  const rfqs = data.rfqs.rfqs;
  const openRfqs = rfqs.filter((r) => r.status === 'OPEN');
  const awardedRfqs = rfqs.filter((r) => r.status === 'AWARDED');

  const latePos = data.late.pos;
  const flagged = latePos.filter((p) => p.late_flagged_at).length;
  const notified = latePos.filter((p) => p.delay_notified_at).length;

  // ``/audit`` returns entries in ascending ``seq``; the card reads newest
  // first, so the page is reversed rather than re-sorted.
  const recentEntries = [...data.recent.entries].reverse();

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

      <div className="ov-grid">
        <div className="ov-main stack">
          {/* Primary work surface: the pending-approvals queue. */}
          <section className="panel">
            <header className="panel-head">
              <h2>Pending approvals queue</h2>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => onNavigate('approvals')}
              >
                View all
              </button>
            </header>
            <div className="content">
              {pending.length === 0 ? (
                <div className="empty">No approvals are waiting.</div>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Approval</th>
                      <th>Vendor</th>
                      <th>PO</th>
                      <th>Invoice</th>
                      <th className="num">Amount</th>
                      <th>Created</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((a) => (
                      <tr key={a.approval_id}>
                        <td className="mono">{a.approval_id}</td>
                        <td className="mono">{a.vendor_id}</td>
                        <td className="mono">{a.po_id}</td>
                        <td className="mono">{a.invoice_id}</td>
                        <td className="num mono">{money(a.amount, 'USD')}</td>
                        <td className="mono">{timestamp(a.created_at)}</td>
                        <td>
                          <Chip tone="warn">{a.status}</Chip>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          {/* Secondary: outstanding purchases, unchanged in substance. */}
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
        </div>

        <aside className="ov-rail">
          {/* Match & approvals: derived from /purchases and /approvals. */}
          <button
            type="button"
            className="ov-card"
            onClick={() => onNavigate('approvals')}
          >
            <span className="ov-card-head">
              <span className="ov-card-title">Match &amp; approvals</span>
              <Chip tone={pending.length > 0 ? 'warn' : 'ok'}>
                {pending.length > 0 ? `${pending.length} pending` : 'clear'}
              </Chip>
            </span>
            <span className="ov-metrics">
              <span className="ov-metric">
                <span className="ov-metric-label">Outstanding POs</span>
                <span className="ov-metric-value">{outstanding.length}</span>
              </span>
              <span className="ov-metric">
                <span className="ov-metric-label">Fully received</span>
                <span className="ov-metric-value">{fullyReceived}</span>
              </span>
              <span className="ov-metric">
                <span className="ov-metric-label">Fully invoiced</span>
                <span className="ov-metric-value">{fullyInvoiced}</span>
              </span>
              <span className="ov-metric">
                <span className="ov-metric-label">Late</span>
                <span className="ov-metric-value">{lateAmongOutstanding}</span>
              </span>
            </span>
            <span className="ov-list">
              <span className="ov-list-item">
                <span className="muted">Pending</span>
                <span className="mono">{pending.length}</span>
              </span>
              <span className="ov-list-item">
                <span className="muted">Approved</span>
                <span className="mono">{approved.length}</span>
              </span>
              <span className="ov-list-item">
                <span className="muted">Executed</span>
                <span className="mono">{executed.length}</span>
              </span>
            </span>
          </button>

          {/* Sourcing: summarises /rfqs from _tools_sourcing. */}
          <button
            type="button"
            className="ov-card"
            onClick={() => onNavigate('sourcing')}
          >
            <span className="ov-card-head">
              <span className="ov-card-title">Sourcing overview</span>
              <Chip tone={openRfqs.length > 0 ? 'accent' : 'muted'}>
                {openRfqs.length} open
              </Chip>
            </span>
            <span className="ov-list">
              {openRfqs.length === 0 ? (
                <span className="ov-list-item">
                  <span className="muted">No open RFQs.</span>
                </span>
              ) : (
                openRfqs.slice(0, 4).map((rfq) => (
                  <span key={rfq.rfq_id} className="ov-list-item">
                    <span className="mono">{rfq.rfq_id}</span>
                    <span className="mono muted">{isoDate(rfq.due_date)}</span>
                  </span>
                ))
              )}
            </span>
            <span className="ov-list-item">
              <span className="muted">Awarded</span>
              <span className="mono">{awardedRfqs.length}</span>
            </span>
          </button>

          {/* Follow-up: summarises /followup from _followup. */}
          <button
            type="button"
            className="ov-card"
            onClick={() => onNavigate('followup')}
          >
            <span className="ov-card-head">
              <span className="ov-card-title">Follow-up</span>
              <Chip tone={latePos.length > 0 ? 'warn' : 'ok'}>
                {latePos.length} late
              </Chip>
            </span>
            <span className="ov-list">
              <span className="ov-list-item">
                <span className="muted">Flagged</span>
                <span className="mono">{flagged}</span>
              </span>
              <span className="ov-list-item">
                <span className="muted">Notified</span>
                <span className="mono">{notified}</span>
              </span>
              <span className="ov-list-item">
                <span className="muted">As of</span>
                <span className="mono">{data.late.as_of}</span>
              </span>
            </span>
          </button>

          {/* Recently completed: the tail of the hash-chain ledger. */}
          <button
            type="button"
            className="ov-card"
            onClick={() => onNavigate('audit')}
          >
            <span className="ov-card-head">
              <span className="ov-card-title">Recently completed</span>
              <Chip tone={audit?.valid ? 'ok' : 'danger'}>
                {audit?.valid ? 'chain ok' : 'chain broken'}
              </Chip>
            </span>
            {recentEntries.length === 0 ? (
              <span className="muted small">No audit entries yet.</span>
            ) : (
              <span className="ov-audit-list">
                {recentEntries.map((entry) => (
                  <span key={entry.seq} className="ov-audit-row">
                    <span className="ov-audit-top">
                      <span className="ov-audit-event">{entry.event_type}</span>
                      <span className="mono muted">#{entry.seq}</span>
                    </span>
                    <span
                      className="ov-audit-cid"
                      title={entry.correlation_id}
                    >
                      {entry.correlation_id || '—'}
                    </span>
                  </span>
                ))}
              </span>
            )}
          </button>
        </aside>
      </div>

      {footer}
    </div>
  );
}
