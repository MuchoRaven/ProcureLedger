/**
 * Response shapes for mutation endpoints whose payload the UI does not read.
 *
 * ``_apply_record_receipt``, ``_apply_po_status`` and ``_apply_rfq_status``
 * live in ``tool_modules`` and are not in this repo, so their exact success
 * payload is inferred rather than read. Rather than invent a shape and have a
 * view read a field that may not exist, those endpoints are typed as
 * ``ActionResponse``: the ``ok: true`` literal is what ``call()`` narrows on,
 * and any extra fields are reachable but untyped.
 *
 * Endpoints whose shape *is* known - ``create_po`` returns ``{ok, po}``,
 * ``create_rfq`` returns ``{ok, rfq}``, ``award_quote`` returns
 * ``{ok, rfq, quote, po}`` - use their specific type from ``types.ts``.
 */
export type ActionResponse = {
  ok: true;
  correlation_id?: string;
} & Record<string, unknown>;
