import { useState } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type {
  Approval,
  ApprovalDetailResponse,
  ApprovalListResponse,
} from '../api/types';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { money, timestamp } from '../lib/format';

/**
 * The executor settlement queue.
 *
 * Settling is the only action in the app that moves money, and
 * ``POST /approvals/{id}/execute`` is the only endpoint the executor
 * credential can reach. ``GET /approvals`` is ``require_any``, so the queue is
 * readable with the same token - which is what makes this a queue rather than
 * a form that needs an ID typed in from out of band.
 *
 * Separation of duties is enforced in ``tools._execute_payout`` under the
 * state lock, not here: the server refuses when the executor's ``sub`` equals
 * the approver's. The UI cannot pre-empt that check because there is no "who
 * am I" endpoint - ``probeRole`` learns the role, never the identity - so a
 * self-settlement surfaces as a 409 ``SEPARATION_OF_DUTIES`` toast rather than
 * a disabled button. The approver column is shown so the conflict is visible
 * before the click.
 *
 * ``showAll`` exists for the same reason as in the operator Approvals view: a
 * settled row leaves the APPROVED queue, so without it there is no way to
 * confirm the settlement landed.
 */
export function Settlement() {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const [showAll, setShowAll] = useState(false);

  const { data, error, loading, refreshing, refresh } = useResource(
    'settlement',
    () => request<ApprovalListResponse>('/approvals'),
    { interval: 30000 },
  );

  const all = data?.approvals ?? [];
  const approved = all.filter((a) => a.status === 'APPROVED');
  const rows = showAll ? all : approved;

  async function settle(approval: Approval) {
    const outcome = await action.run(() =>
      request<ApprovalDetailResponse>(
        `/approvals/${encodeURIComponent(approval.approval_id)}/execute`,
        { method: 'POST', body: {} },
      ),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Settled ${approval.approval_id}`,
        detail: `Executed by ${outcome.result.approval.executor || 'executor'}`,
      });
      setConfirming(null);
      refresh();
    } else {
      const described = describeError(outcome.error);
      toast.push({
        kind: 'error',
        title: described.title,
        detail: described.detail,
        cid: described.cid,
      });
    }
  }

  return (
    <div className="stack">
      <div className="toolbar">
        <span className="muted small">
          Settling moves money. Only an APPROVED payout can be settled, and the
          server refuses a settlement by the identity that approved it.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => setShowAll((value) => !value)}
        >
          {showAll ? 'Show approved only' : 'Show all'}
        </button>
        <span className="muted small">
          {refreshing ? 'Refreshing…' : 'Polled every 30s'}
        </span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : rows.length === 0 ? (
        <Empty>
          {showAll
            ? 'No approvals.'
            : 'No approved payouts are waiting to settle.'}
        </Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Approval</th>
                <th>Vendor</th>
                <th>PO</th>
                <th>Invoice</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th>Approver</th>
                <th>Approved</th>
                <th>Executed</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.approval_id}>
                  <td className="mono">{a.approval_id}</td>
                  <td className="mono">{a.vendor_id}</td>
                  <td className="mono">{a.po_id}</td>
                  <td className="mono">{a.invoice_id}</td>
                  <td className="num">{money(a.amount, 'USD')}</td>
                  <td>
                    <StatusChip status={a.status} />
                  </td>
                  <td>{a.approver || '—'}</td>
                  <td className="mono">{timestamp(a.approved_at)}</td>
                  <td className="mono">{timestamp(a.executed_at)}</td>
                  <td className="actions">
                    {a.status === 'APPROVED' ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        disabled={action.running}
                        onClick={() => setConfirming(a)}
                      >
                        Settle
                      </button>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {confirming ? (
        <Modal
          title={`Settle ${confirming.approval_id}?`}
          onClose={() => setConfirming(null)}
          actions={
            <>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setConfirming(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={action.running}
                onClick={() => settle(confirming)}
              >
                {action.running ? 'Settling…' : 'Settle'}
              </button>
            </>
          }
        >
          <p>
            This settles{' '}
            <span className="mono">{money(confirming.amount, 'USD')}</span> to{' '}
            <span className="mono">{confirming.vendor_id}</span> against{' '}
            <span className="mono">{confirming.invoice_id}</span>. It is
            terminal: an EXECUTED payout cannot be reversed from this console.
          </p>
          <p className="muted small">
            Approved by <span className="mono">{confirming.approver || '—'}</span>
            . If that is the same identity as this credential, the server
            rejects the settlement with{' '}
            <span className="mono">SEPARATION_OF_DUTIES</span>.
          </p>
        </Modal>
      ) : null}
    </div>
  );
}
