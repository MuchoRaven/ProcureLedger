import { useState, type FormEvent } from 'react';
import { describeError } from '../api/client';
import { useAction, useApi, useResource } from '../api/hooks';
import type {
  AwardResponse,
  Quote,
  QuoteDetailResponse,
  QuoteListResponse,
  RFQ,
  RFQDetailResponse,
  RFQListResponse,
} from '../api/types';
import type { ActionResponse } from '../api/actions';
import { Drawer } from '../components/Drawer';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { Modal } from '../components/Modal';
import { StatusChip } from '../components/StatusChip';
import { useToast } from '../components/Toast';
import { isoDate, money } from '../lib/format';

/**
 * RFQs, quotes and award.
 *
 * An award raises a PO and links the two records, so the toast names the new
 * PO - that is the only place the caller learns the ID without a second read.
 * The RFQ may be OPEN or CLOSED when it is awarded: closing stops new quotes,
 * it does not invalidate the ones already on it.
 */
export function Sourcing() {
  const { request } = useApi();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const { data, error, loading, refresh } = useResource(
    'rfqs',
    () => request<RFQListResponse>('/rfqs'),
    { interval: 30000 },
  );

  return (
    <div className="stack">
      <div className="toolbar">
        <span className="muted small">
          An award raises a PO and links it to the RFQ.
        </span>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={() => setCreating(true)}
        >
          Create RFQ
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
      </div>

      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : !data ? null : data.rfqs.length === 0 ? (
        <Empty>No requests for quotes.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>RFQ</th>
                <th>Description</th>
                <th className="num">Quantity</th>
                <th>Status</th>
                <th>Due</th>
                <th>Awarded quote</th>
                <th>PO</th>
                <th className="actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.rfqs.map((rfq) => (
                <tr
                  key={rfq.rfq_id}
                  className="row-click"
                  onClick={() => setSelected(rfq.rfq_id)}
                >
                  <td className="mono">{rfq.rfq_id}</td>
                  <td>{rfq.description}</td>
                  <td className="num">{rfq.quantity}</td>
                  <td>
                    <StatusChip status={rfq.status} />
                  </td>
                  <td className="mono">{isoDate(rfq.due_date)}</td>
                  <td className="mono">{rfq.awarded_quote_id || '—'}</td>
                  <td className="mono">{rfq.po_id || '—'}</td>
                  <td className="actions">
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={(event) => {
                        event.stopPropagation();
                        setSelected(rfq.rfq_id);
                      }}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating ? (
        <Modal title="Create request for quotes" onClose={() => setCreating(false)}>
          <CreateRFQForm
            onDone={() => {
              setCreating(false);
              refresh();
            }}
            onCancel={() => setCreating(false)}
          />
        </Modal>
      ) : null}

      {selected ? (
        <RFQDrawer
          rfqId={selected}
          onClose={() => setSelected(null)}
          onChanged={refresh}
        />
      ) : null}
    </div>
  );
}

function CreateRFQForm({
  onDone,
  onCancel,
}: {
  onDone: () => void;
  onCancel: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [description, setDescription] = useState('');
  const [quantity, setQuantity] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [vendorIds, setVendorIds] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [formError, setFormError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsedQuantity = Number(quantity);
    if (!Number.isFinite(parsedQuantity) || parsedQuantity <= 0) {
      setFormError('Quantity must be a positive number.');
      return;
    }
    const vendors = vendorIds
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean);
    if (vendors.length === 0) {
      setFormError('At least one vendor ID is required.');
      return;
    }
    setFormError('');
    const outcome = await action.run(() =>
      request<RFQDetailResponse>('/rfqs', {
        method: 'POST',
        body: {
          description: description.trim(),
          quantity: parsedQuantity,
          currency: currency.trim() || 'USD',
          vendor_ids: vendors,
          due_date: dueDate,
        },
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Created ${outcome.result.rfq.rfq_id}`,
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
        <span>Description</span>
        <input
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Hydraulic fittings, Q2 restock"
          required
        />
      </label>
      <label className="field">
        <span>Quantity</span>
        <input
          type="number"
          min="0"
          step="1"
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
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
        <span>Vendor IDs (comma separated)</span>
        <input
          value={vendorIds}
          onChange={(event) => setVendorIds(event.target.value)}
          placeholder="VEND-001, VEND-002"
          required
        />
      </label>
      <label className="field">
        <span>Due date</span>
        <input
          type="date"
          value={dueDate}
          onChange={(event) => setDueDate(event.target.value)}
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

function RFQDrawer({
  rfqId,
  onClose,
  onChanged,
}: {
  rfqId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const [quoting, setQuoting] = useState(false);
  const [awarding, setAwarding] = useState<Quote | null>(null);

  const { data, error, loading, refresh } = useResource(
    `rfq:${rfqId}`,
    async () => {
      const [rfq, quotes] = await Promise.all([
        request<RFQDetailResponse>(`/rfqs/${encodeURIComponent(rfqId)}`),
        request<QuoteListResponse>(
          `/rfqs/${encodeURIComponent(rfqId)}/quotes`,
        ),
      ]);
      return { rfq: rfq.rfq, quotes: quotes.quotes };
    },
  );

  async function act(kind: 'close' | 'cancel') {
    const outcome = await action.run(() =>
      request<ActionResponse>(`/rfqs/${encodeURIComponent(rfqId)}/${kind}`, {
        method: 'POST',
        body: {},
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `${kind === 'close' ? 'Closed' : 'Cancelled'} ${rfqId}`,
      });
      refresh();
      onChanged();
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

  async function award(quote: Quote) {
    const outcome = await action.run(() =>
      request<AwardResponse>(`/rfqs/${encodeURIComponent(rfqId)}/award`, {
        method: 'POST',
        body: { quote_id: quote.quote_id },
      }),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Awarded ${quote.quote_id}`,
        detail: `Raised ${outcome.result.po.po_id}`,
      });
      setAwarding(null);
      refresh();
      onChanged();
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

  const rfq = data?.rfq;
  const quotes = data?.quotes ?? [];
  const canQuote = rfq?.status === 'OPEN';
  const canAward = rfq?.status === 'OPEN' || rfq?.status === 'CLOSED';

  return (
    <Drawer title={rfqId} onClose={onClose}>
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorBanner error={error} onRetry={refresh} />
      ) : !rfq ? null : (
        <div className="stack">
          <dl className="kv">
            <dt>Description</dt>
            <dd>{rfq.description}</dd>
            <dt>Quantity</dt>
            <dd className="mono">{rfq.quantity}</dd>
            <dt>Currency</dt>
            <dd className="mono">{rfq.currency}</dd>
            <dt>Status</dt>
            <dd>
              <StatusChip status={rfq.status} />
            </dd>
            <dt>Due date</dt>
            <dd className="mono">{isoDate(rfq.due_date)}</dd>
            <dt>Invited vendors</dt>
            <dd className="mono">
              {rfq.vendor_ids && rfq.vendor_ids.length > 0
                ? rfq.vendor_ids.join(', ')
                : '—'}
            </dd>
            <dt>Awarded quote</dt>
            <dd className="mono">{rfq.awarded_quote_id || '—'}</dd>
            <dt>PO</dt>
            <dd className="mono">{rfq.po_id || '—'}</dd>
          </dl>

          <div className="toolbar">
            {canQuote ? (
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => setQuoting(true)}
              >
                Submit quote
              </button>
            ) : null}
            {rfq.status === 'OPEN' ? (
              <>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  disabled={action.running}
                  onClick={() => act('close')}
                >
                  Close RFQ
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  disabled={action.running}
                  onClick={() => act('cancel')}
                >
                  Cancel RFQ
                </button>
              </>
            ) : null}
          </div>

          <section className="panel">
            <header className="panel-head">
              <h2>Quotes</h2>
            </header>
            <div className="content">
              {quotes.length === 0 ? (
                <div className="empty">No quotes yet.</div>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Quote</th>
                      <th>Vendor</th>
                      <th className="num">Unit price</th>
                      <th className="num">Lead time</th>
                      <th>Valid until</th>
                      <th>Status</th>
                      <th className="actions">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {quotes.map((quote) => (
                      <tr key={quote.quote_id}>
                        <td className="mono">{quote.quote_id}</td>
                        <td className="mono">{quote.vendor_id}</td>
                        <td className="num">
                          {money(quote.unit_price, quote.currency)}
                        </td>
                        <td className="num">{quote.lead_time_days}d</td>
                        <td className="mono">{isoDate(quote.valid_until)}</td>
                        <td>
                          <StatusChip status={quote.status} />
                        </td>
                        <td className="actions">
                          {canAward && quote.status === 'SUBMITTED' ? (
                            <button
                              type="button"
                              className="btn btn-sm btn-primary"
                              onClick={() => setAwarding(quote)}
                            >
                              Award
                            </button>
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
      )}

      {quoting && rfq ? (
        <Modal title={`Submit quote on ${rfqId}`} onClose={() => setQuoting(false)}>
          <SubmitQuoteForm
            rfq={rfq}
            onDone={() => {
              setQuoting(false);
              refresh();
              onChanged();
            }}
            onCancel={() => setQuoting(false)}
          />
        </Modal>
      ) : null}

      {awarding ? (
        <Modal
          title={`Award ${rfqId} to ${awarding.quote_id}?`}
          onClose={() => setAwarding(null)}
          actions={
            <>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setAwarding(null)}
              >
                Back
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={action.running}
                onClick={() => award(awarding)}
              >
                {action.running ? 'Awarding…' : 'Award'}
              </button>
            </>
          }
        >
          <p>
            This raises a PO for{' '}
            <span className="mono">
              {money(awarding.unit_price, awarding.currency)}
            </span>{' '}
            per unit and rejects every other SUBMITTED quote on the RFQ.
          </p>
        </Modal>
      ) : null}
    </Drawer>
  );
}

function SubmitQuoteForm({
  rfq,
  onDone,
  onCancel,
}: {
  rfq: RFQ;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { request } = useApi();
  const toast = useToast();
  const action = useAction();
  const invited = rfq.vendor_ids ?? [];
  const [vendorId, setVendorId] = useState(invited[0] ?? '');
  const [unitPrice, setUnitPrice] = useState('');
  const [currency, setCurrency] = useState(rfq.currency || 'USD');
  const [leadTimeDays, setLeadTimeDays] = useState('0');
  const [validUntil, setValidUntil] = useState('');
  const [formError, setFormError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    const parsedPrice = Number(unitPrice);
    if (!Number.isFinite(parsedPrice) || parsedPrice <= 0) {
      setFormError('Unit price must be a positive number.');
      return;
    }
    const parsedLead = Number(leadTimeDays);
    if (!Number.isInteger(parsedLead) || parsedLead < 0) {
      setFormError('Lead time must be a whole number of days.');
      return;
    }
    if (!validUntil) {
      setFormError('Valid-until date is required.');
      return;
    }
    setFormError('');
    const outcome = await action.run(() =>
      request<QuoteDetailResponse>(
        `/rfqs/${encodeURIComponent(rfq.rfq_id)}/quotes`,
        {
          method: 'POST',
          body: {
            vendor_id: vendorId.trim(),
            unit_price: parsedPrice,
            currency: currency.trim() || 'USD',
            lead_time_days: parsedLead,
            valid_until: validUntil,
          },
        },
      ),
    );
    if (outcome.ok) {
      toast.push({
        kind: 'ok',
        title: `Submitted ${outcome.result.quote.quote_id}`,
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
        <span>Vendor</span>
        {invited.length > 0 ? (
          <select
            value={vendorId}
            onChange={(event) => setVendorId(event.target.value)}
          >
            {invited.map((vendor) => (
              <option key={vendor} value={vendor}>
                {vendor}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={vendorId}
            onChange={(event) => setVendorId(event.target.value)}
            placeholder="VEND-001"
            required
          />
        )}
      </label>
      <label className="field">
        <span>Unit price</span>
        <input
          type="number"
          min="0"
          step="0.01"
          value={unitPrice}
          onChange={(event) => setUnitPrice(event.target.value)}
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
        <span>Lead time (days)</span>
        <input
          type="number"
          min="0"
          step="1"
          value={leadTimeDays}
          onChange={(event) => setLeadTimeDays(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Valid until</span>
        <input
          type="date"
          value={validUntil}
          onChange={(event) => setValidUntil(event.target.value)}
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
          {action.running ? 'Submitting…' : 'Submit quote'}
        </button>
      </div>
    </form>
  );
}
