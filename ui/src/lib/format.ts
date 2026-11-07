/**
 * Display helpers shared by the Phase 3 views.
 *
 * Kept in one place so money, dates and status tones render the same way in
 * every table. ``statusTone`` maps a backend status string to one of the chip
 * tones defined in ``styles.css``; an unknown status falls back to ``muted``
 * rather than guessing, because the backend may add a state before the UI
 * knows about it.
 */

export type Tone = 'ok' | 'warn' | 'danger' | 'accent' | 'muted';

export function money(amount: number, currency: string): string {
  const code = (currency || 'USD').toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: code,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${code} ${amount.toFixed(2)}`;
  }
}

/** Show the date part of an ISO timestamp, or an em dash when absent. */
export function isoDate(value: string | null | undefined): string {
  if (!value) return '—';
  return value.slice(0, 10);
}

/** Render an ISO timestamp as ``YYYY-MM-DD HH:MM:SS UTC``. */
export function timestamp(value: string | null | undefined): string {
  if (!value) return '—';
  return value.replace('T', ' ').replace(/(\.\d+)?(\+00:00|Z)$/, ' UTC');
}

/** Shorten a hash for a table cell; the full value goes in ``title``. */
export function shortHash(value: string | null | undefined): string {
  if (!value) return '—';
  return `${value.slice(0, 10)}…`;
}

export function statusTone(status: string | null | undefined): Tone {
  switch ((status || '').toUpperCase()) {
    case 'OPEN':
    case 'RECEIVED':
    case 'SUBMITTED':
    case 'PENDING':
      return 'accent';
    case 'MATCHED':
    case 'APPROVED':
    case 'EXECUTED':
    case 'CLOSED':
    case 'AWARDED':
    case 'CONVERTED':
    case 'RESOLVED':
    case 'LOW':
      return 'ok';
    case 'EXCEPTION':
    case 'CANCELLED':
    case 'REJECTED':
    case 'INACTIVE':
    case 'HIGH':
      return 'danger';
    case 'MEDIUM':
      return 'warn';
    default:
      return 'muted';
  }
}
