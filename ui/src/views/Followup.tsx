import { useState } from 'react';
import { buildQuery, describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type { FollowupRun, LatePO, LatePOListResponse } from '../api/types';
import type { ActionResponse } from '../api/actions';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money, timestamp } from '../lib/format';

/**
 * Late-PO worklist, the idempotent pass, and delay notification.
 *
 * A re-run reports skips, not failures: ``_apply_flag_late`` turns
 * ``PO_ALREADY_FLAGGED`` into ``{ok: true, skipped: true}``, so the pass result
 * is rendered as three buckets rather than as a success/failure pair.
 */
export function Followup() {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [asOf, setAsOf] = useState('');
  const [runResult, setRunResult] = useState<FollowupRun | null>(null);

  const { data, error, loading, refreshing, refresh } = useResource(
    `followup:${asOf}`,
    () =>
      request<LatePOListResponse>(`/followup${buildQuery({ as_of: asOf })}`),
    { interval: 30000 },
  );

  async function runPass() {
    const outcome = await action.run(() =>
      request<FollowupRun>('/followup/run', {
        method: 'POST',
        body: { as_of: asOf },
      }),
    );
    if (outcome.ok) {
      setRunResult(outcome.result);
      toast.push({
        kind: 'ok',
        title: 'Follow-up pass complete',
        detail: `${outcome.result.flagged.length} flagged, ${outcome.result.skipped.length} skipped, ${outcome.result.errors.length} errors`,
      });
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

  async function notify(po: LatePO) {
    const outcome = await action.run(() =>
      request<ActionResponse>(
        `/pos/${encodeURIComponent(po.po_id)}/notify-delay`,
        { method: 'POST', body: {} },
      ),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: outcome.result.skipped
          ? `${po.po_id} was already notified`
          : `Notified delay on ${po.po_id}`,
      });
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
      <div className="asof">
        <label className="field-inline">
          <span>As of</span>
          <input
            type="date"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => setAsOf('')}
        >
          Today
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          disabled={action.running}
          onClick={runPass}
        >
          {action.running ? 'Running…' : 'Run pass'}
        </button>
        <span className="muted small">
          {refreshing ? 'Refreshing…' : 'Polled every 30s'}
        </span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {runResult ? (
        <div className="banner banner-info">
          <div className="banner-head">
            <strong>Pass result as of {runResult.as_of}</strong>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => setRunResult(null)}
            >
              Dismiss
            </button>
          </div>
          <div className="banner-detail">
            Flagged: {runResult.flagged.length ? runResult.flagged.join(', ') : 'none'}
            <br />
            Skipped: {runResult.skipped.length ? runResult.skipped.join(', ') : 'none'}
            {runResult.errors.length > 0 ? (
              <>
                <br />
                Errors:{' '}
                {runResult.errors
                  .map((entry) => `${entry.po_id} (${entry.error})`)
                  .join(', ')}
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : !data ? null : data.pos.length === 0 ? (
        <Empty>No late purchase orders as of {data.as_of}.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>PO</th>
                <th>Vendor</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th>Expected</th>
                <th className="num">Days late</th>
                <th>Flagged</th>
                <th>Notified</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.pos.map((po) => (
                <tr key={po.po_id}>
                  <td className="mono">{po.po_id}</td>
                  <td className="mono">{po.vendor_id}</td>
                  <td className="num">{money(po.amount, po.currency)}</td>
                  <td>
                    <StatusChip status={po.status} />
                  </td>
                  <td className="mono">{isoDate(po.expected_delivery)}</td>
                  <td className="num">
                    <span className="chip chip-danger">{po.days_late}d</span>
                  </td>
                  <td className="mono">{timestamp(po.late_flagged_at)}</td>
                  <td className="mono">{timestamp(po.delay_notified_at)}</td>
                  <td className="actions">
                    {po.delay_notified_at ? (
                      <span className="muted">Notified</span>
                    ) : (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={action.running}
                        onClick={() => notify(po)}
                      >
                        Notify delay
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
