import { useState } from 'react';
import { SessionProvider, useSession } from './auth/session';
import { ToastProvider } from './components/Toast';
import { Layout, type NavItem } from './components/Layout';
import { Login } from './views/Login';
import { ComingSoon } from './views/ComingSoon';
import { Overview } from './views/Overview';
import { Purchases } from './views/Purchases';
import { Approvals } from './views/Approvals';
import { Procurement } from './views/Procurement';
import { Invoices } from './views/Invoices';
import { Sourcing } from './views/Sourcing';
import { Followup } from './views/Followup';
import { Audit } from './views/Audit';
import { Settlement } from './views/Settlement';
import { AgentConsole } from './views/AgentConsole';
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

/**
 * Every nav entry routes to a real view as of Phase 5. ``ComingSoon`` is kept
 * only for the default case, which the nav cannot reach - it exists so a stale
 * or unknown id renders an explanation rather than a blank shell.
 */
function View({
  id,
  label,
  onNavigate,
}: {
  id: string;
  label: string;
  onNavigate: (id: string) => void;
}) {
  switch (id) {
    case 'overview':
      return <Overview onNavigate={onNavigate} />;
    case 'purchases':
      return <Purchases />;
    case 'approvals':
      return <Approvals />;
    case 'procurement':
      return <Procurement />;
    case 'invoices':
      return <Invoices />;
    case 'sourcing':
      return <Sourcing />;
    case 'followup':
      return <Followup />;
    case 'audit':
      return <Audit />;
    case 'settlement':
      return <Settlement />;
    case 'console':
      return <AgentConsole />;
    default:
      return (
        <ComingSoon
          label={label}
          phase="a later phase"
          note="No view is registered for this id."
        />
      );
  }
}

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
  const label = nav.find((item) => item.id === activeId)?.label ?? activeId;

  return (
    <Layout
      nav={nav}
      active={activeId}
      onNavigate={setActive}
      // The dark control-plane chrome is scoped to Overview; every other view
      // keeps the original light shell.
      variant={activeId === 'overview' ? 'dark' : 'default'}
    >
      <View id={activeId} label={label} onNavigate={setActive} />
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
