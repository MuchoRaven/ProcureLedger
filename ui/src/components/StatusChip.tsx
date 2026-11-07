import { statusTone } from '../lib/format';

/**
 * A status chip.
 *
 * The tone is derived from the status string rather than passed in, so every
 * table renders ``OPEN`` the same way. An unknown status is ``muted``, not an
 * error: the backend may add a state before the UI knows about it.
 */
export function StatusChip({ status }: { status: string | null | undefined }) {
  const label = status || 'UNKNOWN';
  return <span className={`chip chip-${statusTone(status)}`}>{label}</span>;
}
