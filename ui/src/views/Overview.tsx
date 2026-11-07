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
 */
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

  if (loading) return <Loading />;
  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (!data) return null;

  const outstanding = data.purchases.purchases.filter((p) => p.is_outstanding);
  const lateAmongOutstanding = outstanding.filter((p) => p.is_late).length;
  const pending = data.approvals.approvals.filter((a) => a.status === 'PENDING');
  const late = data.late.pos;
  const audit = data.verify;

  return (
    <div className="stack">
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
          <span className="card-foot muted">{lateAmongOutstanding} late</span>
        </button>
        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('followup')}
        >
          <span className="card-label">Late POs</span>
          <span className="card-value">{late.length}</span>
          <span className="card-foot muted">as of {data.late.as_of}</span>
        </button>
        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('approvals')}
        >
          <span className="card-label">Pending approvals</span>
          <span className="card-value">{pending.length}</span>
          <span className="card-foot muted">awaiting decision</span>
        </button>
        <button
          type="button"
          className="card card-link"
          onClick={() => onNavigate('audit')}
        >
          <span className="card-label">Audit chain</span>
          <span className="card-value">{audit.valid ? 'OK' : 'BROKEN'}</span>
          <span className="card-foot muted">{audit.entries} entries</span>
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
                </tr>
              </thead>
              <tbody>
                {outstanding.slice(0, 8).map((p) => (
                  <tr key={p.po_id}>
                    <td className="mono">{p.po_id}</td>
                    <td>{p.vendor_name || p.vendor_id}</td>
                    <td>{p.status}</td>
                    <td className="num">{money(p.amount, p.currency)}</td>
                    <td className="num">
                      {money(p.outstanding_receipt, p.currency)}
                    </td>
                    <td className="mono">{p.expected_delivery || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  );
}
