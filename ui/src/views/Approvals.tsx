import { useState } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type { Approval, ApprovalListResponse } from '../api/types';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money } from '../lib/format';

/**
 * The PENDING queue.
 *
 * Approving records the decision; it does not move money. The approver is the
 * authenticated operator, so the request body is empty - a body ``approver``
 * that disagreed with the principal would be a 403, and sending one would be
 * a way to get that 403 by accident.
 *
 * ``showAll`` exists because an approved row leaves the pending queue: without
 * it there is no way to confirm the decision landed.
 */
export function Approvals() {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const [showAll, setShowAll] = useState(false);

  const { data, error, loading, refresh } = useResource(
    'approvals',
    () => request<ApprovalListResponse>('/approvals'),
    { interval: 30000 },
  );

  const all = data?.approvals ?? [];
  const pending = all.filter((a) => a.status === 'PENDING');
  const rows = showAll ? all : pending;

  async function approve(approval: Approval) {
    const outcome = await action.run(() =>
      request(`/approvals/${encodeURIComponent(approval.approval_id)}/approve`, {
        method: 'POST',
        body: {},
      }),
    );
    if (outcome.ok) {
      toast.push({ kind: 'ok', title: `Approved ${approval.approval_id}` });
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
          Approving records the decision; it does not move money.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => setShowAll((value) => !value)}
        >
          {showAll ? 'Show pending only' : 'Show all'}
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : rows.length === 0 ? (
        <Empty>
          {showAll ? 'No approvals.' : 'No approvals are waiting.'}
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
                <th>Created</th>
                <th>Approver</th>
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
                  <td className="mono">{isoDate(a.created_at)}</td>
                  <td>{a.approver || '—'}</td>
                  <td className="actions">
                    {a.status === 'PENDING' ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        disabled={action.running}
                        onClick={() => setConfirming(a)}
                      >
                        Approve
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
          title={`Approve ${confirming.approval_id}?`}
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
                onClick={() => approve(confirming)}
              >
                {action.running ? 'Approving…' : 'Approve'}
              </button>
            </>
          }
        >
          <p>
            This records the decision for{' '}
            <span className="mono">{confirming.approval_id}</span>. It does not
            move money; settlement is a separate executor action.
          </p>
        </Modal>
      ) : null}
    </div>
  );
}
