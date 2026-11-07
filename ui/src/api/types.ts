/**
 * Wire types.
 *
 * These mirror the records the backend actually returns, not the internal
 * dataclasses. Where the two differ the wire shape wins: ``PurchaseRow`` is
 * the *derived* view from ``/purchases`` (receipts and invoices already rolled
 * up), while ``PO`` is the raw record from ``/pos``. Keeping them separate is
 * what stops a view from reading ``outstanding_receipt`` off a PO that never
 * had it.
 *
 * Every response type below is checked against ``app.py`` and the
 * ``tool_modules`` it calls. Where a shape is inferred rather than read - the
 * apply helpers in ``_apply_procurement`` are not in this repo - the comment
 * says so.
 */

/** The one envelope every endpoint returns, success or failure. */
export type Envelope = {
  ok: boolean;
  error?: string;
  /**
   * Not always a string. ``_purchases._resolve_as_of`` and
   * ``_followup._resolve_as_of`` raise ``ToolError`` with a dict detail, and
   * ``_followup._apply_notify_delay`` returns one in-band. ``_respond`` only
   * strips ``detail`` on 5xx, so a 400/404 can carry an object.
   */
  detail?: unknown;
  correlation_id?: string;
};

/**
 * The three credentials are not interchangeable. There is no "who am I"
 * endpoint, so the role is inferred by ``probeRole`` in ``client.ts``.
 */
export type Role = 'agent' | 'operator' | 'executor';

/** Raw purchase order, as returned by ``/pos``. */
export type PO = {
  po_id: string;
  vendor_id: string;
  amount: number;
  currency: string;
  status: string;
  expected_delivery: string;
  requisition_id: string | null;
  created_at: string;
  updated_at?: string;
  /** Stamped by ``award_quote`` so the award is visible from the PO side. */
  rfq_id?: string;
  late_flagged_at?: string;
  delay_notified_at?: string;
  delay_notified_by?: string;
};

/**
 * Derived purchase view from ``/purchases``.
 *
 * ``is_late`` and ``days_late`` are computed against the ``as_of`` date the
 * caller supplied, which is why the row echoes ``as_of`` back: the browser's
 * idea of "today" and the server's can differ across timezones.
 */
export type PurchaseRow = {
  po_id: string;
  vendor_id: string;
  vendor_name: string;
  status: string;
  currency: string;
  amount: number;
  expected_delivery: string;
  requisition_id: string | null;
  created_at: string;
  received_amount: number;
  invoiced_amount: number;
  outstanding_receipt: number;
  outstanding_invoice: number;
  receipt_count: number;
  invoice_count: number;
  last_receipt_at: string;
  is_fully_received: boolean;
  is_fully_invoiced: boolean;
  is_late: boolean;
  days_late: number;
  is_outstanding: boolean;
  as_of: string;
};

export type Invoice = {
  invoice_id: string;
  vendor_id: string;
  po_id: string;
  amount: number;
  currency: string;
  status: string;
  receipt_ids?: string[];
  due_date?: string;
  created_at: string;
  updated_at: string;
};

export type Approval = {
  approval_id: string;
  vendor_id: string;
  po_id: string;
  invoice_id: string;
  amount: number;
  status: string;
  created_at: string;
  updated_at: string;
  approver?: string;
  approved_at?: string;
  executor?: string;
  executed_at?: string;
};

export type Requisition = {
  requisition_id: string;
  description: string;
  vendor_id: string | null;
  po_id: string | null;
  quantity: number;
  amount: number;
  currency: string;
  status: string;
  requested_by: string;
  created_at: string;
  updated_at: string;
};

export type RFQ = {
  rfq_id: string;
  description: string;
  quantity: number;
  currency: string;
  vendor_ids?: string[];
  due_date?: string;
  status: string;
  awarded_quote_id?: string | null;
  po_id?: string | null;
  created_at: string;
  updated_at: string;
  created_by?: string;
  correlation_id?: string;
};

export type Quote = {
  quote_id: string;
  rfq_id: string;
  vendor_id: string;
  unit_price: number;
  currency: string;
  lead_time_days: number;
  valid_until: string;
  status: string;
  submitted_at?: string;
  updated_at?: string;
  awarded_at?: string;
  correlation_id?: string;
};

export type AuditEntry = {
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  correlation_id: string;
  source: string;
  timestamp: string;
  prev_hash: string;
  hash: string;
};

/** A late PO as returned by ``/followup``: a PO plus its computed lateness. */
export type LatePO = PO & { days_late: number };

/**
 * ``/audit/verify`` result.
 *
 * ``valid: false`` is a 200 with ``ok: true`` - a broken chain is a finding,
 * not a transport failure. ``broken_at`` names the first bad ``seq``.
 * ``detail`` is stripped unless ``PL_DEBUG`` is set.
 */
export type VerifyResult = {
  ok: boolean;
  valid: boolean;
  entries: number;
  broken_at?: number;
  reason?: string;
  detail?: unknown;
  correlation_id?: string;
};

export type AgentTranscriptEntry = {
  tool: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
};

/**
 * ``POST /agent/run`` result.
 *
 * ``ok: false`` here is a *completed* run that hit ``MAX_STEPS_EXCEEDED`` or a
 * tool error - it arrives as HTTP 200 and the transcript is still worth
 * rendering. ``call()`` throws on it, so the agent console reads the transcript
 * off ``ApiError.body``.
 */
export type AgentResult = {
  ok: boolean;
  reply?: string;
  error?: string;
  detail?: string;
  steps: number;
  transcript: AgentTranscriptEntry[];
  correlation_id?: string;
};

/**
 * ``POST /followup/run`` result.
 *
 * ``skipped`` is not an error: a second pass over an already-notified PO is
 * idempotent and reports the PO as skipped rather than failing. ``ok`` is
 * always true - per-PO failures land in ``errors``.
 */
export type FollowupRun = {
  ok: boolean;
  as_of: string;
  flagged: string[];
  skipped: string[];
  errors: { po_id: string; error: string; detail: unknown }[];
  correlation_id?: string;
};

// ---------------------------------------------------------------------------
// Response wrappers
// ---------------------------------------------------------------------------
//
// One type per endpoint, so a view cannot read a field the endpoint does not
// return. The ``ok: true`` literal is what lets a caller narrow after
// ``call()`` has already thrown on ``ok: false``.

export type PurchaseListResponse = {
  ok: true;
  as_of: string;
  purchases: PurchaseRow[];
  correlation_id?: string;
};

export type PurchaseDetailResponse = {
  ok: true;
  purchase: PurchaseRow;
  correlation_id?: string;
};

export type InvoiceListResponse = {
  ok: true;
  invoices: Invoice[];
  correlation_id?: string;
};

export type InvoiceDetailResponse = {
  ok: true;
  invoice: Invoice;
  correlation_id?: string;
};

export type ApprovalListResponse = {
  ok: true;
  approvals: Approval[];
  correlation_id?: string;
};

export type ApprovalDetailResponse = {
  ok: true;
  approval: Approval;
  correlation_id?: string;
};

export type RequisitionListResponse = {
  ok: true;
  requisitions: Requisition[];
  correlation_id?: string;
};

export type RequisitionDetailResponse = {
  ok: true;
  requisition: Requisition;
  correlation_id?: string;
};

export type POListResponse = {
  ok: true;
  pos: PO[];
  correlation_id?: string;
};

export type PODetailResponse = {
  ok: true;
  po: PO;
  correlation_id?: string;
};

export type RFQListResponse = {
  ok: true;
  rfqs: RFQ[];
  correlation_id?: string;
};

export type RFQDetailResponse = {
  ok: true;
  rfq: RFQ;
  correlation_id?: string;
};

export type QuoteListResponse = {
  ok: true;
  rfq_id: string;
  quotes: Quote[];
  correlation_id?: string;
};

export type QuoteDetailResponse = {
  ok: true;
  quote: Quote;
  correlation_id?: string;
};

/**
 * ``POST /rfqs/{id}/award`` result.
 *
 * Returns all three records because the award touches all three: the RFQ moves
 * to AWARDED, the winning quote to AWARDED, and a PO is created.
 */
export type AwardResponse = {
  ok: true;
  rfq: RFQ;
  quote: Quote;
  po: PO;
  correlation_id?: string;
};

export type AuditPageResponse = {
  ok: true;
  entries: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
  correlation_id?: string;
};

export type LatePOListResponse = {
  ok: true;
  as_of: string;
  pos: LatePO[];
  correlation_id?: string;
};
