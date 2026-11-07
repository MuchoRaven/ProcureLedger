import type { Role } from './types';

/**
 * A business or transport failure reported by the backend.
 *
 * ``body`` carries the full envelope. It matters for ``POST /agent/run``:
 * ``MAX_STEPS_EXCEEDED`` is a 200 with ``ok: false``, and the transcript the
 * console needs to render lives in that body, not in the error fields.
 */
export class ApiError extends Error {
  constructor(
    public code: string,
    public detail: string,
    public status: number,
    public correlationId: string,
    public body: any = null,
  ) {
    super(code);
    this.name = 'ApiError';
  }
}

/**
 * A 429. Carries the server's ``Retry-After`` so the caller can count down
 * instead of blind-retrying into the same budget.
 */
export class RateLimited extends Error {
  constructor(
    public retryAfter: number,
    public correlationId: string,
  ) {
    super('RATE_LIMITED');
    this.name = 'RateLimited';
  }
}

/** The request never reached the server, or the response was not HTTP. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

/**
 * Request options.
 *
 * ``body`` is ``unknown`` rather than ``BodyInit`` so callers can pass a plain
 * object and have it serialised. Strings, ``FormData``, ``Blob``,
 * ``URLSearchParams`` and ``ArrayBuffer`` pass through untouched.
 */
export type RequestOptions = Omit<RequestInit, 'body'> & { body?: unknown };

function isRawBody(value: unknown): value is BodyInit {
  return (
    typeof value === 'string' ||
    (typeof FormData !== 'undefined' && value instanceof FormData) ||
    (typeof Blob !== 'undefined' && value instanceof Blob) ||
    (typeof URLSearchParams !== 'undefined' && value instanceof URLSearchParams) ||
    (typeof ArrayBuffer !== 'undefined' && value instanceof ArrayBuffer)
  );
}

/**
 * Render a ``detail`` field for display.
 *
 * ``detail`` is not always a string. ``_purchases._resolve_as_of`` and
 * ``_followup._resolve_as_of`` raise ``ToolError`` with a dict detail, and
 * ``_followup._apply_notify_delay`` returns one in-band; ``app._respond`` only
 * strips ``detail`` on 5xx, so a 400/404 can carry a JSON object. Rendering
 * that as ``[object Object]`` would hide the field name the caller needs.
 */
export function formatDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return '';
  if (typeof detail === 'string') return detail;
  if (typeof detail === 'number' || typeof detail === 'boolean') {
    return String(detail);
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

/**
 * A correlation id for the request.
 *
 * The server echoes its own when it has one, but a request that dies before
 * reaching the handler still needs a handle for support, so one is generated
 * up front and sent as ``X-Correlation-ID``.
 */
export function newCorrelationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '');
  }
  return Math.random().toString(16).slice(2) + Date.now().toString(16);
}

/**
 * One request, one envelope.
 *
 * The rules this enforces, all of them derived from ``app.py``:
 *
 * * a 200 does not imply success - ``MAX_STEPS_EXCEEDED`` is a 200 with
 *   ``ok: false``, so the body is always inspected;
 * * a 429 carries ``Retry-After`` and is surfaced as ``RateLimited``;
 * * ``detail`` is blanked on 5xx by the server unless ``PL_DEBUG`` is set, so
 *   an empty detail is normal and must not be rendered as a bug;
 * * ``correlation_id`` is echoed from the body when present, otherwise the one
 *   this client generated, so a toast always has a support handle;
 * * nothing is retried automatically. ``_commit`` is audit-before-mutation, so
 *   a retry after an ambiguous failure could double-apply a write.
 */
export async function call<T>(
  path: string,
  token: string,
  options: RequestOptions = {},
): Promise<T> {
  const cid = newCorrelationId();
  const headers = new Headers(options.headers);
  headers.set('Accept', 'application/json');
  headers.set('Authorization', `Bearer ${token}`);
  headers.set('X-Correlation-ID', cid);

  const { body: rawBody, ...rest } = options;
  let body: BodyInit | undefined;
  if (rawBody !== undefined) {
    if (isRawBody(rawBody)) {
      body = rawBody;
    } else {
      body = JSON.stringify(rawBody);
      if (!headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
      }
    }
  }

  let res: Response;
  try {
    res = await fetch(path, { ...rest, headers, body });
  } catch (err) {
    throw new NetworkError(err instanceof Error ? err.message : String(err));
  }

  if (res.status === 429) {
    const raw = res.headers.get('Retry-After');
    const parsed = raw === null ? NaN : Number(raw);
    throw new RateLimited(
      Number.isFinite(parsed) && parsed > 0 ? parsed : 60,
      cid,
    );
  }

  let parsedBody: any = null;
  try {
    parsedBody = await res.json();
  } catch {
    parsedBody = null;
  }

  if (parsedBody === null || typeof parsedBody !== 'object') {
    throw new ApiError(
      'INVALID_RESPONSE',
      `HTTP ${res.status} with no JSON body`,
      res.status,
      cid,
    );
  }

  if (!parsedBody.ok) {
    throw new ApiError(
      typeof parsedBody.error === 'string' ? parsedBody.error : 'TOOL_ERROR',
      formatDetail(parsedBody.detail),
      res.status,
      typeof parsedBody.correlation_id === 'string'
        ? parsedBody.correlation_id
        : cid,
      parsedBody,
    );
  }

  return parsedBody as T;
}

/**
 * Work out which console a token belongs to.
 *
 * There is no "who am I" endpoint, so the role is inferred from three probes:
 *
 * 1. ``GET /invoices`` is ``require_any``, so a 200 proves the token is real.
 *    A 401 here means the token is wrong and propagates.
 * 2. ``GET /purchases`` is ``require_operator`` and read-only - a 200 means
 *    operator, a 403 means not.
 * 3. ``POST /approvals/__probe__/execute`` is ``require_executor``. The
 *    approval does not exist, so ``_execute_payout`` raises
 *    ``APPROVAL_NOT_FOUND`` (404) for an executor and ``require_executor``
 *    raises ``AUTH_FORBIDDEN`` (403) otherwise. Nothing is mutated:
 *    ``_execute_payout`` calls ``_commit(..., check=check)``, and ``_commit``
 *    runs ``check()`` before ``_append_audit``, so the 404 is raised before
 *    any audit link is written.
 *
 * Anything left is the agent credential, which can only reach ``/agent/run``.
 *
 * The three probes cost three units of the caller's rate-limit budget
 * (``PL_RATE_LIMIT_PER_MIN``, default 120), which is why this runs once at
 * sign-in rather than on every render.
 */
export async function probeRole(token: string): Promise<Role> {
  await call('/invoices', token);

  try {
    await call('/purchases', token);
    return 'operator';
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 403) throw err;
  }

  try {
    await call('/approvals/__probe__/execute', token, {
      method: 'POST',
      body: {},
    });
    return 'executor';
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return 'executor';
    if (!(err instanceof ApiError) || err.status !== 403) throw err;
  }

  return 'agent';
}

/**
 * Flatten any thrown value into the three things a UI can show: a code, a
 * human-readable detail, and a correlation id.
 *
 * An empty ``detail`` is expected on 5xx (the server redacts it unless
 * ``PL_DEBUG`` is set) and must not be rendered as a bug.
 */
export function describeError(err: unknown): {
  title: string;
  detail: string;
  cid: string;
} {
  if (err instanceof ApiError) {
    return { title: err.code, detail: err.detail, cid: err.correlationId };
  }
  if (err instanceof RateLimited) {
    return {
      title: 'RATE_LIMITED',
      detail: `Request budget exhausted. Retry in ${Math.ceil(err.retryAfter)}s.`,
      cid: err.correlationId,
    };
  }
  if (err instanceof NetworkError) {
    return { title: 'NETWORK_ERROR', detail: err.message, cid: '' };
  }
  return {
    title: 'UNEXPECTED_ERROR',
    detail: err instanceof Error ? err.message : String(err),
    cid: '',
  };
}

/**
 * Build a query string, dropping empty values.
 *
 * ``as_of`` is the reason this exists: an empty value means "today (UTC)" on
 * the server (``_purchases._resolve_as_of``), so it must be omitted rather
 * than sent as ``as_of=``.
 */
export function buildQuery(
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}
