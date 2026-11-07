import { useState } from 'react';
import { SessionProvider, useSession } from './auth/session';
import { ToastProvider } from './components/Toast';
import { Layout, type NavItem } from './components/Layout';
import { Login } from './views/Login';
import { ComingSoon } from './views/ComingSoon';
import type { Role } from './api/types';

/**
 * One app, three consoles.
 *
 * The nav sets are not cosmetic: they are the set of endpoints each credential
 * can actually reach. ``/rfqs`` is operator-only while ``/pos`` and
 * ``/invoices`` accept any credential, so the operator nav is the only one
 * that lists Sourcing. A role never sees a view it would get a 403 on.
 */
const NAV: Record<Role, NavItem[]> = {
  operator: [
    { id: 'overview', label: 'Overview' },
    { id: 'purchases', label: 'Purchases' },
    { id: 'approvals', label: 'Approvals' },
    { id: 'procurement', label: 'Procurement' },
    { id: 'invoices', label: 'Invoices' },
    { id: 'sourcing', label: 'Sourcing' },
    { id: 'followup', label: 'Follow-up' },
    { id: 'audit', label: 'Audit' },
  ],
  executor: [{ id: 'settlement', label: 'Settlement queue' }],
  agent: [{ id: 'console', label: 'Agent console' }],
};

const VIEWS: Record<string, { phase: string; note: string }> = {
  overview: {
    phase: 'Phase 3',
    note: 'Outstanding POs, late POs, pending approvals and audit health, polled every 30s.',
  },
  purchases: {
    phase: 'Phase 3',
    note: 'Derived purchase rows with an as_of picker, receipt and invoice progress, and a drill-in drawer.',
  },
  approvals: {
    phase: 'Phase 3',
    note: 'The PENDING queue. Approving records the decision; it does not move money.',
  },
  procurement: {
    phase: 'Phase 3',
    note: 'PO create, receipt, close and cancel, plus the read-only requisition list.',
  },
  invoices: {
    phase: 'Phase 3',
    note: 'Invoice list, detail and ingest. Three-way match is agent-only, so there is no button for it.',
  },
  sourcing: {
    phase: 'Phase 3',
    note: 'RFQs, quotes and award. An award raises a PO and links the two records.',
  },
  followup: {
    phase: 'Phase 3',
    note: 'Late-PO worklist, the idempotent pass, and delay notification. A re-run reports skips, not failures.',
  },
  audit: {
    phase: 'Phase 3',
    note: 'Paged chain viewer with hash linkage and a verify button.',
  },
  settlement: {
    phase: 'Phase 4',
    note: 'APPROVED approvals only. Separation of duties is enforced server-side, so a self-settlement is a 409.',
  },
  console: {
    phase: 'Phase 5',
    note: 'Chat plus the tool-call transcript, including runs that end in MAX_STEPS_EXCEEDED.',
  },
};

function Root() {
  const { session } = useSession();
  const [active, setActive] = useState('');

  if (!session) {
    return <Login />;
  }

  const nav = NAV[session.role];
  // Derived rather than stored: a stale id from a previous role falls back to
  // that role's first view instead of rendering an empty shell.
  const activeId = nav.some((item) => item.id === active)
    ? active
    : (nav[0]?.id ?? '');
  const view = VIEWS[activeId] ?? {
    phase: 'a later phase',
    note: 'No view is registered for this id.',
  };
  const label = nav.find((item) => item.id === activeId)?.label ?? activeId;

  return (
    <Layout nav={nav} active={activeId} onNavigate={setActive}>
      <ComingSoon label={label} phase={view.phase} note={view.note} />
    </Layout>
  );
}

export function App() {
  return (
    <ToastProvider>
      <SessionProvider>
        <Root />
      </SessionProvider>
    </ToastProvider>
  );
}
