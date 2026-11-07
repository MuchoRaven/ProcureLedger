import { useState, type FormEvent } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type {
  Invoice,
  InvoiceDetailResponse,
  InvoiceListResponse,
} from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money } from '../lib/format';

/**
 * Invoice list, detail and ingest.
 *
 * Three-way match is agent-only, so there is no button for it here - the
 * toolbar says so rather than leaving the absence unexplained.
 */
export function Invoices() {
  const { request } = useApi();
  const [selected, setSelected] = useState<string | null>(null);
  const [ingesting, setIngesting] = useState(false);

  const { data, error, loading, refresh } = useResource(
    'invoices',
    () => request<InvoiceListResponse>('/invoices'),
    { interval: 30000 },
  );

  return (
    <div className="stack">
      <div className="toolbar">
        <span className="muted small">
          Three-way match is agent-only, so there is no button for it here.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={() => setIngesting(true)}
        >
          Ingest invoice
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : !data ? null : data.invoices.length === 0 ? (
        <Empty>No invoices.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Vendor</th>
                <th>PO</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th>Due</th>
                <th>Created</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.invoices.map((invoice) => (
                <tr
                  key={invoice.invoice_id}
                  className="row-click"
                  onClick={() => setSelected(invoice.invoice_id)}
                >
                  <td className="mono">{invoice.invoice_id}</td>
                  <td className="mono">{invoice.vendor_id}</td>
                  <td className="mono">{invoice.po_id}</td>
                  <td className="num">
                    {money(invoice.amount, invoice.currency)}
                  </td>
                  <td>
                    <StatusChip status={invoice.status} />
                  </td>
                  <td className="mono">{isoDate(invoice.due_date)}</td>
                  <td className="mono">{isoDate(invoice.created_at)}</td>
                  <td className="actions">
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelected(invoice.invoice_id);
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
        <InvoiceDrawer
          invoiceId={selected}
          onClose={() => setSelected(null)}
        />
      ) : null}

      {ingesting ? (
        <Modal title="Ingest invoice" onClose={() => setIngesting(false)}>
          <IngestForm
            onDone={() => {
              setIngesting(false);
              refresh();
            }}
            onCancel={() => setIngesting(false)}
          />
        </Modal>
      ) : null}
    </div>
  );
}

function InvoiceDrawer({
  invoiceId,
  onClose,
}: {
  invoiceId: string;
  onClose: () => void;
}) {
  const { request } = useApi();
  const { data, error, loading } = useResource(`invoice:${invoiceId}`, () =>
    request<InvoiceDetailResponse>(
      `/invoices/${encodeURIComponent(invoiceId)}`,
    ),
  );

  return (
    <Drawer title={invoiceId} onClose={onClose}>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorBanner error={error} />
      ) : data ? (
        <InvoiceDetail invoice={data.invoice} />
      ) : null}
    </Drawer>
  );
}

function InvoiceDetail({ invoice }: { invoice: Invoice }) {
  return (
    <dl className="kv">
      <dt>Vendor</dt>
      <dd className="mono">{invoice.vendor_id}</dd>
      <dt>PO</dt>
      <dd className="mono">{invoice.po_id}</dd>
      <dt>Amount</dt>
      <dd className="mono">{money(invoice.amount, invoice.currency)}</dd>
      <dt>Status</dt>
      <dd>
        <StatusChip status={invoice.status} />
      </dd>
      <dt>Due date</dt>
      <dd className="mono">{isoDate(invoice.due_date)}</dd>
      <dt>Receipts</dt>
      <dd className="mono">
        {invoice.receipt_ids && invoice.receipt_ids.length > 0
          ? invoice.receipt_ids.join(', ')
          : '—'}
      </dd>
      <dt>Created</dt>
      <dd className="mono">{isoDate(invoice.created_at)}</dd>
      <dt>Updated</dt>
      <dd className="mono">{isoDate(invoice.updated_at)}</dd>
    </dl>
  );
}

/**
 * The ingest form.
 *
 * ``amount`` is parsed to a number before it is sent: ``app.Amount`` is
 * ``confloat(strict=True, gt=0)``, so a string would be a 422 rather than a
 * business error. The client-side check is a courtesy, not a control - the
 * server validates the same thing.
 */
function IngestForm({
  onDone,
  onCancel,
}: {
  onDone: () => void;
  onCancel: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [invoiceId, setInvoiceId] = useState('');
  const [vendorId, setVendorId] = useState('');
  const [poId, setPoId] = useState('');
  const [amount, setAmount] = useState('');
  const [currency, setCurrency] = useState('USD');
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
      request<InvoiceDetailResponse>('/invoices', {
        method: 'POST',
        body: {
          invoice_id: invoiceId.trim(),
          vendor_id: vendorId.trim(),
          po_id: poId.trim(),
          amount: parsed,
          currency: currency.trim() || 'USD',
        },
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Ingested ${outcome.result.invoice.invoice_id}`,
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
        <span>Invoice ID</span>
        <input
          value={invoiceId}
          onChange={(event) => setInvoiceId(event.target.value)}
          placeholder="INV-2003"
          required
        />
      </label>
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
        <span>PO ID</span>
        <input
          value={poId}
          onChange={(event) => setPoId(event.target.value)}
          placeholder="PO-1001"
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
          {action.running ? 'Ingesting…' : 'Ingest'}
        </button>
      </div>
    </form>
  );
}
