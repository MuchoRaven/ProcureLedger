import { useState } from 'react';
import { buildQuery } from '../api/client';
import { useApi, useResource } from '../api/hooks';
import type {
  PurchaseDetailResponse,
  PurchaseListResponse,
  PurchaseRow,
} from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Progress } from '../components/Progress';
import { StatusChip } from '../components/StatusChip';
import { isoDate, money } from '../lib/format';

/**
 * Derived purchase rows with an ``as_of`` picker and a drill-in drawer.
 *
 * ``as_of`` is a query parameter, not a body: an empty value means "today
 * (UTC)" on the server, so it is omitted rather than sent as ``as_of=``.
 * ``buildQuery`` is what enforces that.
 */
export function Purchases() {
  const { request } = useApi();
  const [asOf, setAsOf] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const { data, error, loading, refreshing, refresh } = useResource(
    `purchases:${asOf}`,
    () =>
      request<PurchaseListResponse>(`/purchases${buildQuery({ as_of: asOf })}`),
    { interval: 30000 },
  );

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
      ) : !data ? null : data.purchases.length === 0 ? (
        <Empty>No purchase orders.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>PO</th>
                <th>Vendor</th>
                <th>Status</th>
                <th className="num">Amount</th>
                <th>Received</th>
                <th>Invoiced</th>
                <th>Expected</th>
                <th>Late</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.purchases.map((p) => (
                <tr
                  key={p.po_id}
                  className="row-click"
                  onClick={() => setSelected(p.po_id)}
                >
                  <td className="mono">{p.po_id}</td>
                  <td>{p.vendor_name || p.vendor_id}</td>
                  <td>
                    <StatusChip status={p.status} />
                  </td>
                  <td className="num">{money(p.amount, p.currency)}</td>
                  <td>
                    <Progress
                      value={p.received_amount}
                      max={p.amount}
                      label={`${money(p.received_amount, p.currency)} / ${money(
                        p.amount,
                        p.currency,
                      )}`}
                    />
                  </td>
                  <td>
                    <Progress
                      value={p.invoiced_amount}
                      max={p.received_amount || p.amount}
                      label={`${money(p.invoiced_amount, p.currency)} / ${money(
                        p.received_amount,
                        p.currency,
                      )}`}
                    />
                  </td>
                  <td className="mono">{isoDate(p.expected_delivery)}</td>
                  <td>
                    {p.is_late ? (
                      <span className="chip chip-danger">{p.days_late}d</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="actions">
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelected(p.po_id);
                      }}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected ? (
        <PurchaseDrawer
          poId={selected}
          asOf={asOf}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </div>
  );
}

function PurchaseDrawer({
  poId,
  asOf,
  onClose,
}: {
  poId: string;
  asOf: string;
  onClose: () => void;
}) {
  const { request } = useApi();
  const { data, error, loading } = useResource(
    `purchase:${poId}:${asOf}`,
    () =>
      request<PurchaseDetailResponse>(
        `/purchases/${encodeURIComponent(poId)}${buildQuery({ as_of: asOf })}`,
      ),
  );

  return (
    <Drawer title={poId} onClose={onClose}>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorBanner error={error} />
      ) : data ? (
        <PurchaseDetail purchase={data.purchase} />
      ) : null}
    </Drawer>
  );
}

function PurchaseDetail({ purchase }: { purchase: PurchaseRow }) {
  return (
    <dl className="kv">
      <dt>Vendor</dt>
      <dd>{purchase.vendor_name || purchase.vendor_id}</dd>
      <dt>Status</dt>
      <dd>
        <StatusChip status={purchase.status} />
      </dd>
      <dt>Amount</dt>
      <dd className="mono">{money(purchase.amount, purchase.currency)}</dd>
      <dt>Received</dt>
      <dd className="mono">
        {money(purchase.received_amount, purchase.currency)}
      </dd>
      <dt>Invoiced</dt>
      <dd className="mono">
        {money(purchase.invoiced_amount, purchase.currency)}
      </dd>
      <dt>Outstanding receipt</dt>
      <dd className="mono">
        {money(purchase.outstanding_receipt, purchase.currency)}
      </dd>
      <dt>Outstanding invoice</dt>
      <dd className="mono">
        {money(purchase.outstanding_invoice, purchase.currency)}
      </dd>
      <dt>Receipts</dt>
      <dd>{purchase.receipt_count}</dd>
      <dt>Invoices</dt>
      <dd>{purchase.invoice_count}</dd>
      <dt>Last receipt</dt>
      <dd className="mono">{isoDate(purchase.last_receipt_at)}</dd>
      <dt>Expected delivery</dt>
      <dd className="mono">{isoDate(purchase.expected_delivery)}</dd>
      <dt>Requisition</dt>
      <dd className="mono">{purchase.requisition_id || '—'}</dd>
      <dt>Late</dt>
      <dd>{purchase.is_late ? `${purchase.days_late} days` : 'No'}</dd>
      <dt>As of</dt>
      <dd className="mono">{purchase.as_of}</dd>
    </dl>
  );
}
