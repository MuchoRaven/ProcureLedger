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
 * Inline SVG glyphs, one per nav id.
 *
 * Hand-rolled rather than pulled from an icon package: the UI has no icon
 * dependency, and adding one for eight 16px glyphs is not worth the bundle.
 * Every path is stroke-based and inherits ``currentColor``, so the active
 * sky-blue state colours the icon for free.
 */
const ICON_PATHS: Record<string, ReactNode> = {
  overview: (
    <>
      <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
    </>
  ),
  purchases: (
    <>
      <path d="M2.5 5.5 8 2.5l5.5 3v5L8 13.5 2.5 10.5z" />
      <path d="M2.5 5.5 8 8.5l5.5-3" />
      <path d="M8 8.5v5" />
    </>
  ),
  approvals: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="m5.6 8.1 1.7 1.7 3.1-3.4" />
    </>
  ),
  procurement: (
    <>
      <rect x="3.5" y="3.5" width="9" height="10" rx="1.5" />
      <path d="M6 2.5h4v2H6z" />
      <path d="M6 7.5h4M6 10.5h4" />
    </>
  ),
  invoices: (
    <>
      <path d="M4 2.5h5l3 3v8H4z" />
      <path d="M9 2.5v3h3" />
      <path d="M6 8.5h4M6 11h4" />
    </>
  ),
  sourcing: (
    <>
      <circle cx="7" cy="7" r="4" />
      <path d="m10 10 3.5 3.5" />
    </>
  ),
  followup: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5v3.2l2 1.3" />
    </>
  ),
  audit: (
    <>
      <path d="M8 2.5 13 4.5v4c0 3-2.2 4.6-5 5.5-2.8-.9-5-2.5-5-5.5v-4z" />
      <path d="m5.8 8 1.6 1.6 3-3" />
    </>
  ),
};

function NavIcon({ id }: { id: string }) {
  const paths = ICON_PATHS[id];
  if (!paths) return null;
  return (
    <svg
      className="nav-icon"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths}
    </svg>
  );
}

/**
 * The shell: a dark sidebar against a light work surface.
 *
 * The nav is passed in rather than derived here, because the set of reachable
 * views is a property of the credential, not of the shell. A role never sees a
 * nav entry it would get a 403 on - the asymmetry in ``app.py`` (``/rfqs`` is
 * operator-only while ``/pos`` accepts any credential) is reflected in the nav
 * rather than hidden behind a disabled button.
 *
 * ``variant`` is opt-in and defaults to the light shell, so only the view that
 * asks for it changes appearance. ``dark`` swaps in the slate control-plane
 * chrome and renders a glyph per nav item; the light shell keeps its original
 * text-only nav.
 */
export function Layout({
  nav,
  active,
  onNavigate,
  children,
  variant = 'default',
}: {
  nav: NavItem[];
  active: string;
  onNavigate: (id: string) => void;
  children: ReactNode;
  variant?: 'default' | 'dark';
}) {
  const { session, signOut } = useSession();
  const current = nav.find((item) => item.id === active);
  const role = session?.role;
  const dark = variant === 'dark';

  return (
    <div className={`shell${dark ? ' shell-dark' : ''}`}>
      <aside className={`sidebar${dark ? ' sidebar-dark' : ''}`}>
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
              {dark ? <NavIcon id={item.id} /> : null}
              <span>{item.label}</span>
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

      <main className={`main${dark ? ' main-dark' : ''}`}>
        <header className={`topbar${dark ? ' topbar-dark' : ''}`}>
          <h1>{current?.label ?? 'ProcureLedger'}</h1>
        </header>
        <div className={`content${dark ? ' content-dark' : ''}`}>
          {children}
        </div>
      </main>
    </div>
  );
}
