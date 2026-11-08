import { useMemo, useState } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type {
  Approval,
  ApprovalDetailResponse,
  ApprovalListResponse,
} from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money, timestamp } from '../lib/format';

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
 * The filter bar is client-side over the single ``/approvals`` read: the
 * endpoint returns the whole store, so search, status, date range and vendor
 * are all applied in the browser. The default status filter is ``APPROVED``
 * because that is the settleable queue; ``All statuses`` replaces the old
 * "Show all" toggle.
 *
 * The executor cannot approve or reject - those are operator-only endpoints -
 * so the inline actions are ``Settle`` (APPROVED rows only) and
 * ``View Details``, and the bulk action is ``Bulk Settle``. A row that is not
 * APPROVED has its checkbox disabled rather than hidden, so the column stays
 * aligned and the reason is a tooltip.
 */
const DATE_RANGES: { value: string; label: string }[] = [
  { value: 'ALL', label: 'All time' },
  { value: 'TODAY', label: 'Today' },
  { value: '7D', label: 'Last 7 days' },
  { value: '30D', label: 'Last 30 days' },
];

/** Lower bound (ms) for a date-range preset, or ``null`` for "all time". */
function cutoffFor(range: string): number | null {
  if (range === 'ALL') return null;
  const now = new Date();
  if (range === 'TODAY') {
    return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  }
  const days = range === '7D' ? 7 : range === '30D' ? 30 : 0;
  return Date.now() - days * 24 * 60 * 60 * 1000;
}

export function Settlement() {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('APPROVED');
  const [vendorFilter, setVendorFilter] = useState('ALL');
  const [dateRange, setDateRange] = useState('ALL');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkRunning, setBulkRunning] = useState(false);
  const [detail, setDetail] = useState<Approval | null>(null);

  const { data, error, loading, refreshing, refresh } = useResource(
    'settlement',
    () => request<ApprovalListResponse>('/approvals'),
    { interval: 30000 },
  );

  const all = data?.approvals ?? [];

  const vendors = useMemo(() => {
    const set = new Set<string>();
    for (const a of all) set.add(a.vendor_id);
    return Array.from(set).sort();
  }, [all]);

  const filtered = useMemo(() => {
    const cutoff = cutoffFor(dateRange);
    const q = search.trim().toLowerCase();
    return all.filter((a) => {
      if (statusFilter !== 'ALL' && a.status !== statusFilter) return false;
      if (vendorFilter !== 'ALL' && a.vendor_id !== vendorFilter) return false;
      if (cutoff !== null) {
        const created = Date.parse(a.created_at);
        if (!Number.isFinite(created) || created < cutoff) return false;
      }
      if (q) {
        const hay =
          `${a.approval_id} ${a.vendor_id} ${a.po_id} ${a.invoice_id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [all, statusFilter, vendorFilter, dateRange, search]);

  const settleableIds = useMemo(
    () =>
      filtered
        .filter((a) => a.status === 'APPROVED')
        .map((a) => a.approval_id),
    [filtered],
  );
  const allSelected =
    settleableIds.length > 0 && settleableIds.every((id) => selected.has(id));
  const selectedSettleable = settleableIds.filter((id) => selected.has(id));

  function clearSelection() {
    setSelected(new Set());
  }

  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        for (const id of settleableIds) next.delete(id);
      } else {
        for (const id of settleableIds) next.add(id);
      }
      return next;
    });
  }

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

  async function bulkSettle() {
    const ids = selectedSettleable;
    if (ids.length === 0) return;
    setBulkRunning(true);
    let ok = 0;
    const failures: { id: string; error: string }[] = [];
    for (const id of ids) {
      try {
        await request<ApprovalDetailResponse>(
          `/approvals/${encodeURIComponent(id)}/execute`,
          { method: 'POST', body: {} },
        );
        ok++;
      } catch (err) {
        failures.push({ id, error: describeError(err).title });
      }
    }
    setBulkRunning(false);
    setBulkConfirm(false);
    clearSelection();
    if (failures.length === 0) {
      toast.push({
        kind: 'ok',
        title: `Settled ${ok} payout${ok === 1 ? '' : 's'}`,
      });
    } else {
      toast.push({
        kind: 'error',
        title: `Settled ${ok}, ${failures.length} failed`,
        detail: failures.map((f) => `${f.id}: ${f.error}`).join('; '),
      });
    }
    refresh();
  }

  const bulkTotal = selectedSettleable.reduce((sum, id) => {
    const a = all.find((x) => x.approval_id === id);
    return sum + (a?.amount ?? 0);
  }, 0);

  return (
    <div className="stack">
      <div className="filter-bar">
        <input
          type="search"
          placeholder="Search approvals..."
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            clearSelection();
          }}
          aria-label="Search approvals"
        />
        <select
          value={statusFilter}
          onChange={(event) => {
            setStatusFilter(event.target.value);
            clearSelection();
          }}
          aria-label="Filter by status"
        >
          <option value="ALL">All statuses</option>
          <option value="APPROVED">Approved</option>
          <option value="PENDING">Pending</option>
          <option value="EXECUTED">Executed</option>
        </select>
        <select
          value={dateRange}
          onChange={(event) => {
            setDateRange(event.target.value);
            clearSelection();
          }}
          aria-label="Filter by date range"
        >
          {DATE_RANGES.map((range) => (
            <option key={range.value} value={range.value}>
              {range.label}
            </option>
          ))}
        </select>
        <select
          value={vendorFilter}
          onChange={(event) => {
            setVendorFilter(event.target.value);
            clearSelection();
          }}
          aria-label="Filter by vendor"
        >
          <option value="ALL">All vendors</option>
          {vendors.map((vendor) => (
            <option key={vendor} value={vendor}>
              {vendor}
            </option>
          ))}
        </select>
        <div className="filter-actions">
          {selected.size > 0 ? (
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={clearSelection}
            >
              Clear
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={selectedSettleable.length === 0 || bulkRunning}
            onClick={() => setBulkConfirm(true)}
          >
            {bulkRunning
              ? 'Settling…'
              : `Bulk Settle${
                  selectedSettleable.length
                    ? ` (${selectedSettleable.length})`
                    : ''
                }`}
          </button>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={refresh}
          >
            Refresh
          </button>
        </div>
      </div>

      <div className="toolbar">
        <span className="muted small">
          Settling moves money. Only an APPROVED payout can be settled, and the
          server refuses a settlement by the identity that approved it.
        </span>
        <span className="muted small">
          {refreshing ? 'Refreshing…' : 'Polled every 30s'}
        </span>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : filtered.length === 0 ? (
        <Empty>
          {all.length === 0
            ? 'No approvals.'
            : 'No approvals match the current filters.'}
        </Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="check">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    disabled={settleableIds.length === 0}
                    aria-label="Select all settleable approvals"
                  />
                </th>
                <th>Approval ID</th>
                <th>Vendor</th>
                <th className="num">Amount</th>
                <th>Submitted</th>
                <th>Status</th>
                <th>Agent Match</th>
                <th>Approver</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((a) => {
                const settleable = a.status === 'APPROVED';
                return (
                  <tr key={a.approval_id}>
                    <td className="check">
                      <input
                        type="checkbox"
                        checked={selected.has(a.approval_id)}
                        disabled={!settleable}
                        onChange={() => toggleOne(a.approval_id)}
                        title={
                          settleable
                            ? undefined
                            : 'Only APPROVED payouts can be settled'
                        }
                        aria-label={`Select ${a.approval_id}`}
                      />
                    </td>
                    <td className="mono strong">{a.approval_id}</td>
                    <td className="mono">{a.vendor_id}</td>
                    <td className="num mono strong">
                      {money(a.amount, 'USD')}
                    </td>
                    <td className="mono">{isoDate(a.created_at)}</td>
                    <td>
                      <StatusChip status={a.status} />
                    </td>
                    <td>
                      <span
                        className="chip chip-ok"
                        title="Approvals are only created from MATCHED invoices. Live re-check is not exposed by /approvals."
                      >
                        Agent Match OK
                      </span>
                    </td>
                    <td>{a.approver || '—'}</td>
                    <td className="actions">
                      {settleable ? (
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          disabled={action.running}
                          onClick={() => setConfirming(a)}
                        >
                          Settle
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => setDetail(a)}
                      >
                        View Details
                      </button>
                    </td>
                  </tr>
                );
              })}
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
            Approved by{' '}
            <span className="mono">{confirming.approver || '—'}</span>. If that
            is the same identity as this credential, the server rejects the
            settlement with{' '}
            <span className="mono">SEPARATION_OF_DUTIES</span>.
          </p>
        </Modal>
      ) : null}

      {bulkConfirm ? (
        <Modal
          title={`Settle ${selectedSettleable.length} payout${
            selectedSettleable.length === 1 ? '' : 's'
          }?`}
          onClose={() => setBulkConfirm(false)}
          actions={
            <>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setBulkConfirm(false)}
                disabled={bulkRunning}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={bulkRunning}
                onClick={bulkSettle}
              >
                {bulkRunning ? 'Settling…' : 'Settle all'}
              </button>
            </>
          }
        >
          <p>
            This settles{' '}
            <span className="mono">{money(bulkTotal, 'USD')}</span> across{' '}
            <span className="mono">{selectedSettleable.length}</span> approved
            payout{selectedSettleable.length === 1 ? '' : 's'}. Each settlement
            is terminal and cannot be reversed from this console.
          </p>
          <p className="muted small">
            Settlements are attempted one at a time. A row that fails (for
            example, a separation-of-duties conflict) is reported in the toast
            and the rest still run.
          </p>
        </Modal>
      ) : null}

      {detail ? (
        <Drawer title={detail.approval_id} onClose={() => setDetail(null)}>
          <ApprovalDetail approval={detail} />
        </Drawer>
      ) : null}
    </div>
  );
}

function ApprovalDetail({ approval }: { approval: Approval }) {
  return (
    <dl className="kv">
      <dt>Approval</dt>
      <dd className="mono">{approval.approval_id}</dd>
      <dt>Vendor</dt>
      <dd className="mono">{approval.vendor_id}</dd>
      <dt>PO</dt>
      <dd className="mono">{approval.po_id}</dd>
      <dt>Invoice</dt>
      <dd className="mono">{approval.invoice_id}</dd>
      <dt>Amount</dt>
      <dd className="mono">{money(approval.amount, 'USD')}</dd>
      <dt>Status</dt>
      <dd>
        <StatusChip status={approval.status} />
      </dd>
      <dt>Approver</dt>
      <dd>{approval.approver || '—'}</dd>
      <dt>Approved</dt>
      <dd className="mono">{timestamp(approval.approved_at)}</dd>
      <dt>Executor</dt>
      <dd>{approval.executor || '—'}</dd>
      <dt>Executed</dt>
      <dd className="mono">{timestamp(approval.executed_at)}</dd>
      <dt>Created</dt>
      <dd className="mono">{timestamp(approval.created_at)}</dd>
      <dt>Updated</dt>
      <dd className="mono">{timestamp(approval.updated_at)}</dd>
    </dl>
  );
}
