import { useState } from 'react';
import { buildQuery, describeError } from '../api/client';
import { useApi, useResource } from '../api/hooks';
import type { AuditEntry, AuditPageResponse, VerifyResult } from '../api/types';
import { Drawer } from '../components/Drawer';
import { ErrorBanner, Empty, Loading } from '../components/Feedback';
import { shortHash, timestamp } from '../lib/format';

const PAGE_SIZE = 50;

/**
 * Paged chain viewer with hash linkage and a verify button.
 *
 * ``/audit/verify`` reports a broken chain as a 200 with ``valid: false`` -
 * a finding, not a transport failure - so the result is rendered as a banner
 * rather than thrown. ``detail`` is stripped unless ``PL_DEBUG`` is set, so an
 * empty detail is expected and is not rendered.
 */
export function Audit() {
  const { request } = useApi();
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<AuditEntry | null>(null);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [verifyError, setVerifyError] = useState<Error | null>(null);
  const [verifying, setVerifying] = useState(false);

  const { data, error, loading, refresh } = useResource(
    `audit:${offset}`,
    () =>
      request<AuditPageResponse>(
        `/audit${buildQuery({ limit: PAGE_SIZE, offset })}`,
      ),
  );

  async function runVerify() {
    setVerifying(true);
    setVerifyError(null);
    try {
      const result = await request<VerifyResult>('/audit/verify');
      setVerify(result);
    } catch (err) {
      setVerify(null);
      setVerifyError(err as Error);
    } finally {
      setVerifying(false);
    }
  }

  const total = data?.total ?? 0;
  const entries = data?.entries ?? [];
  const canPrev = offset > 0;
  const canNext = offset + PAGE_SIZE < total;

  return (
    <div className="stack">
      <div className="toolbar">
        <button
          type="button"
          className="btn btn-sm btn-primary"
          disabled={verifying}
          onClick={runVerify}
        >
          {verifying ? 'Verifying…' : 'Verify chain'}
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={refresh}>
          Refresh
        </button>
        <span className="muted small">
          {total} entries · showing {entries.length === 0 ? 0 : offset + 1}–
          {offset + entries.length}
        </span>
      </div>

      {verify ? (
        <div
          className={`banner ${verify.valid ? 'banner-info' : 'banner-error'}`}
        >
          <div className="banner-head">
            <strong>
              {verify.valid ? 'Chain intact' : 'Chain broken'}
            </strong>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => setVerify(null)}
            >
              Dismiss
            </button>
          </div>
          <div className="banner-detail">
            {verify.entries} entries
            {verify.valid
              ? ''
              : ` · broken at seq ${verify.broken_at ?? '?'} (${verify.reason ?? 'unknown'})`}
          </div>
        </div>
      ) : null}

      {verifyError ? <ErrorBanner error={verifyError} /> : null}
      {error ? <ErrorBanner error={error} onRetry={refresh} /> : null}

      {loading ? (
        <Loading />
      ) : entries.length === 0 ? (
        <Empty>No audit entries.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th className="num">Seq</th>
                <th>Event</th>
                <th>Source</th>
                <th>Timestamp</th>
                <th>Correlation</th>
                <th>Prev hash</th>
                <th>Hash</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.seq}
                  className="row-click"
                  onClick={() => setSelected(entry)}
                >
                  <td className="num mono">{entry.seq}</td>
                  <td className="mono">{entry.event_type}</td>
                  <td>{entry.source}</td>
                  <td className="mono">{timestamp(entry.timestamp)}</td>
                  <td className="mono" title={entry.correlation_id}>
                    {entry.correlation_id || '—'}
                  </td>
                  <td className="mono" title={entry.prev_hash}>
                    {shortHash(entry.prev_hash)}
                  </td>
                  <td className="mono" title={entry.hash}>
                    {shortHash(entry.hash)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="toolbar">
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          disabled={!canPrev}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          disabled={!canNext}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>

      {selected ? (
        <Drawer title={`seq ${selected.seq}`} onClose={() => setSelected(null)}>
          <div className="stack">
            <dl className="kv">
              <dt>Event</dt>
              <dd className="mono">{selected.event_type}</dd>
              <dt>Source</dt>
              <dd>{selected.source}</dd>
              <dt>Timestamp</dt>
              <dd className="mono">{timestamp(selected.timestamp)}</dd>
              <dt>Correlation</dt>
              <dd className="mono">{selected.correlation_id || '—'}</dd>
              <dt>Prev hash</dt>
              <dd className="mono">{selected.prev_hash}</dd>
              <dt>Hash</dt>
              <dd className="mono">{selected.hash}</dd>
            </dl>
            <section className="panel">
              <header className="panel-head">
                <h2>Payload</h2>
              </header>
              <div className="content">
                <pre className="mono">
                  {JSON.stringify(selected.payload, null, 2)}
                </pre>
              </div>
            </section>
          </div>
        </Drawer>
      ) : null}
    </div>
  );
}
