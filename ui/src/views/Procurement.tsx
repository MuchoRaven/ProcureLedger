import { useState, type FormEvent } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type {
  PO,
  PODetailResponse,
  POListResponse,
  RequisitionListResponse,
} from '../api/types';
import type { ActionResponse } from '../api/actions';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money } from '../lib/format';

/**
 * PO create, receipt, close and cancel, plus the read-only requisition list.
 *
 * The requisition tab is read-only on purpose: ``app.py`` calls
 * ``_tools_procurement.create_requisition`` and ``cancel_requisition``, which
 * the module docstring flags as a known gap - no public wrapper exists yet, so
 * those endpoints raise ``AttributeError`` at request time. Shipping a button
 * that 500s would be worse than saying so.
 */
export function Procurement() {
  const [tab, setTab] = useState<'pos' | 'requisitions'>('pos');

  return (
    <div className="stack">
      <div className="tabs">
        <button
          type="button"
          className={`tab${tab === 'pos' ? ' tab-active' : ''}`}
          onClick={() => setTab('pos')}
        >
          Purchase orders
        </button>
        <button
          type="button"
          className={`tab${tab === 'requisitions' ? ' tab-active' : ''}`}
          onClick={() => setTab('requisitions')}
        >
          Requisitions
        </button>
      </div>

      {tab === 'pos' ? <PurchaseOrders /> : <Requisitions />}
    </div>
  );
}

function PurchaseOrders() {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [creating, setCreating] = useState(false);
  const [receiptFor, setReceiptFor] = useState<PO | null>(null);
  const [confirm, setConfirm] = useState<{
    kind: 'close' | 'cancel';
    po: PO;
  } | null>(null);

  const { data, error, loading, refresh } = useResource(
    'pos',
    () => request<POListResponse>('/pos'),
    { interval: 30000 },
  );

  async function act(kind: 'close' | 'cancel', po: PO) {
    const outcome = await action.run(() =>
      request<ActionResponse>(`/pos/${encodeURIComponent(po.po_id)}/${kind}`, {
        method: 'POST',
        body: {},
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `${kind === 'close' ? 'Closed' : 'Cancelled'} ${po.po_id}`,
      });
      setConfirm(null);
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
          A receipt is capped at the PO amount; close and cancel only apply to
          an OPEN PO.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={() => setCreating(true)}
        >
          Create PO
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : !data ? null : data.pos.length === 0 ? (
        <Empty>No purchase orders.</Empty>
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
                <th>Requisition</th>
                <th>RFQ</th>
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
                  <td className="mono">{po.requisition_id || '—'}</td>
                  <td className="mono">{po.rfq_id || '—'}</td>
                  <td className="actions">
                    {po.status === 'OPEN' ? (
                      <>
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          onClick={() => setReceiptFor(po)}
                        >
                          Receipt
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          onClick={() => setConfirm({ kind: 'close', po })}
                        >
                          Close
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          onClick={() => setConfirm({ kind: 'cancel', po })}
                        >
                          Cancel
                        </button>
                      </>
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

      {creating ? (
        <Modal title="Create purchase order" onClose={() => setCreating(false)}>
          <CreatePOForm
            onDone={() => {
              setCreating(false);
              refresh();
            }}
            onCancel={() => setCreating(false)}
          />
        </Modal>
      ) : null}

      {receiptFor ? (
        <Modal
          title={`Record receipt for ${receiptFor.po_id}`}
          onClose={() => setReceiptFor(null)}
        >
          <ReceiptForm
            po={receiptFor}
            onDone={() => {
              setReceiptFor(null);
              refresh();
            }}
            onCancel={() => setReceiptFor(null)}
          />
        </Modal>
      ) : null}

      {confirm ? (
        <Modal
          title={`${confirm.kind === 'close' ? 'Close' : 'Cancel'} ${
            confirm.po.po_id
          }?`}
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setConfirm(null)}
              >
                Back
              </button>
              <button
                type="button"
                className={
                  confirm.kind === 'close'
                    ? 'btn btn-primary'
                    : 'btn btn-danger'
                }
                disabled={action.running}
                onClick={() => act(confirm.kind, confirm.po)}
              >
                {action.running
                  ? 'Working…'
                  : confirm.kind === 'close'
                    ? 'Close PO'
                    : 'Cancel PO'}
              </button>
            </>
          }
        >
          <p>
            {confirm.kind === 'close'
              ? 'Closing an OPEN PO stops further receipts. It does not reverse anything already recorded.'
              : 'Cancelling an OPEN PO is terminal. It does not reverse anything already recorded.'}
          </p>
        </Modal>
      ) : null}
    </div>
  );
}

function CreatePOForm({
  onDone,
  onCancel,
}: {
  onDone: () => void;
  onCancel: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [vendorId, setVendorId] = useState('');
  const [amount, setAmount] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [expectedDelivery, setExpectedDelivery] = useState('');
  const [requisitionId, setRequisitionId] = useState('');
  const [formError, setFormError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(amount);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setFormError('Amount must be a positive number.');
      return;
    }
    setFormError('');
    const outcome = await action.run(() =>
      request<PODetailResponse>('/pos', {
        method: 'POST',
        body: {
          vendor_id: vendorId.trim(),
          amount: parsed,
          currency: currency.trim() || 'USD',
          expected_delivery: expectedDelivery,
          requisition_id: requisitionId.trim(),
        },
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Created ${outcome.result.po.po_id}`,
      });
      onDone();
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
    <form className="stack" onSubmit={submit}>
      <label className="field">
        <span>Vendor ID</span>
        <input
          value={vendorId}
          onChange={(event) => setVendorId(event.target.value)}
          placeholder="VEND-001"
          required
        />
      </label>
      <label className="field">
        <span>Amount</span>
        <input
          type="number"
          min="0"
          step="0.01"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          required
        />
      </label>
      <label className="field">
        <span>Currency</span>
        <input
          value={currency}
          onChange={(event) => setCurrency(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Expected delivery</span>
        <input
          type="date"
          value={expectedDelivery}
          onChange={(event) => setExpectedDelivery(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Requisition ID (optional)</span>
        <input
          value={requisitionId}
          onChange={(event) => setRequisitionId(event.target.value)}
          placeholder="REQ-4002"
        />
      </label>

      {formError ? (
        <div className="banner banner-error" role="alert">
          <div className="banner-detail">{formError}</div>
        </div>
      ) : null}

      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={action.running}>
          {action.running ? 'Creating…' : 'Create'}
        </button>
      </div>
    </form>
  );
}

function ReceiptForm({
  po,
  onDone,
  onCancel,
}: {
  po: PO;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [amount, setAmount] = useState('');
  const [formError, setFormError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = Number(amount);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setFormError('Amount must be a positive number.');
      return;
    }
    setFormError('');
    const outcome = await action.run(() =>
      request<ActionResponse>(
        `/pos/${encodeURIComponent(po.po_id)}/receipts`,
        { method: 'POST', body: { amount: parsed } },
      ),
    );
    if (outcome.ok) {
      toast.push({ kind: 'ok', title: `Receipt recorded for ${po.po_id}` });
      onDone();
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
    <form className="stack" onSubmit={submit}>
      <p className="muted small">
        PO amount is {money(po.amount, po.currency)}. A receipt that would take
        the total past that is rejected with{' '}
        <span className="mono">RECEIPT_EXCEEDS_PO</span>.
      </p>
      <label className="field">
        <span>Receipt amount</span>
        <input
          type="number"
          min="0"
          step="0.01"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          required
        />
      </label>

      {formError ? (
        <div className="banner banner-error" role="alert">
          <div className="banner-detail">{formError}</div>
        </div>
      ) : null}

      <div className="modal-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={action.running}>
          {action.running ? 'Recording…' : 'Record receipt'}
        </button>
      </div>
    </form>
  );
}

function Requisitions() {
  const { request } = useApi();
  const { data, error, loading, refresh } = useResource(
    'requisitions',
    () => request<RequisitionListResponse>('/requisitions'),
    { interval: 30000 },
  );

  return (
    <div className="stack">
      <div className="banner banner-warn">
        <div className="banner-head">
          <strong>Read-only</strong>
        </div>
        <div className="banner-detail">
          Creating and cancelling a requisition are not wired yet:{' '}
          <span className="mono">app.py</span> calls{' '}
          <span className="mono">_tools_procurement.create_requisition</span>{' '}
          and <span className="mono">cancel_requisition</span>, which have no
          public wrapper, so those endpoints raise at request time. The list
          below is live.
        </div>
      </div>

      <div className="toolbar">
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : !data ? null : data.requisitions.length === 0 ? (
        <Empty>No requisitions.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Requisition</th>
                <th>Description</th>
                <th>Vendor</th>
                <th>PO</th>
                <th className="num">Quantity</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th>Requested by</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.requisitions.map((req) => (
                <tr key={req.requisition_id}>
                  <td className="mono">{req.requisition_id}</td>
                  <td>{req.description}</td>
                  <td className="mono">{req.vendor_id || '—'}</td>
                  <td className="mono">{req.po_id || '—'}</td>
                  <td className="num">{req.quantity}</td>
                  <td className="num">{money(req.amount, req.currency)}</td>
                  <td>
                    <StatusChip status={req.status} />
                  </td>
                  <td>{req.requested_by}</td>
                  <td className="mono">{isoDate(req.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
