import type { ReactNode } from 'react';
import { useSession } from '../auth/session';
import type { Role } from '../api/types';

export type NavItem = { id: string; label: string };

const ROLE_TONE: Record<Role, string> = {
  operator: 'accent',
  executor: 'warn',
  agent: 'ok',
};

/**
 * The shell: a dark sidebar against a light work surface.
 *
 * The nav is passed in rather than derived here, because the set of reachable
 * views is a property of the credential, not of the shell. A role never sees a
 * nav entry it would get a 403 on - the asymmetry in ``app.py`` (``/rfqs`` is
 * operator-only while ``/pos`` accepts any credential) is reflected in the nav
 * rather than hidden behind a disabled button.
 */
export function Layout({
  nav,
  active,
  onNavigate,
  children,
}: {
  nav: NavItem[];
  active: string;
  onNavigate: (id: string) => void;
  children: ReactNode;
}) {
  const { session, signOut } = useSession();
  const current = nav.find((item) => item.id === active);
  const role = session?.role;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">PL</span>
          <span className="brand-name">ProcureLedger</span>
        </div>

        <div className="role-badge">
          <span className={`chip chip-${role ? ROLE_TONE[role] : 'muted'}`}>
            {role ?? 'unknown'}
          </span>
        </div>

        <nav className="nav">
          {nav.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-item${item.id === active ? ' nav-item-active' : ''}`}
              aria-current={item.id === active ? 'page' : undefined}
              onClick={() => onNavigate(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={signOut}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>{current?.label ?? 'ProcureLedger'}</h1>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}
