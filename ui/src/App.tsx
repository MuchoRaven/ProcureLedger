/**
 * Phase 0 placeholder.
 *
 * This file exists so ``npm run typecheck`` has a real module to check and so
 * the design tokens in ``styles.css`` can be eyeballed before any view lands.
 * Phase 2 replaces it with the session provider, the login screen and the
 * role-based shell.
 */
export function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">PL</span>
          <span className="brand-name">ProcureLedger</span>
        </div>
        <div className="role-badge">
          <span className="chip chip-muted">phase 0</span>
        </div>
        <nav className="nav">
          <button type="button" className="nav-item nav-item-active">
            Scaffold
          </button>
          <button type="button" className="nav-item" disabled>
            Overview
          </button>
          <button type="button" className="nav-item" disabled>
            Purchases
          </button>
          <button type="button" className="nav-item" disabled>
            Approvals
          </button>
        </nav>
        <div className="sidebar-foot">
          <span className="muted small">no session</span>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>Scaffold</h1>
        </header>
        <div className="content">
          <div className="stack">
            <div className="banner banner-info">
              <div className="banner-head">
                <strong>Phase 0 complete</strong>
              </div>
              <div className="banner-detail">
                Vite, TypeScript and the design tokens are in place. The API
                client, session provider and views arrive in later phases.
              </div>
            </div>

            <section className="panel">
              <header className="panel-head">
                <h2>Status chips</h2>
              </header>
              <div className="content">
                <span className="chip chip-ok">ok</span>{' '}
                <span className="chip chip-warn">warn</span>{' '}
                <span className="chip chip-danger">danger</span>{' '}
                <span className="chip chip-accent">accent</span>{' '}
                <span className="chip chip-muted">muted</span>
              </div>
            </section>

            <section className="panel">
              <header className="panel-head">
                <h2>Buttons</h2>
              </header>
              <div className="content toolbar">
                <button type="button" className="btn btn-primary">
                  Primary
                </button>
                <button type="button" className="btn btn-ghost">
                  Ghost
                </button>
                <button type="button" className="btn btn-danger">
                  Danger
                </button>
                <button type="button" className="btn btn-sm btn-ghost">
                  Small
                </button>
                <button type="button" className="btn btn-primary" disabled>
                  Disabled
                </button>
              </div>
            </section>

            <section className="panel">
              <header className="panel-head">
                <h2>Table and monospace IDs</h2>
              </header>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>PO</th>
                      <th>Vendor</th>
                      <th className="num">Amount</th>
                      <th>Status</th>
                      <th>Late</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="mono">PO-1001</td>
                      <td>Acme Industrial Supply</td>
                      <td className="num">$12,500.00</td>
                      <td>
                        <span className="chip chip-accent">OPEN</span>
                      </td>
                      <td>
                        <span className="chip chip-danger">late · 41d</span>
                      </td>
                    </tr>
                    <tr>
                      <td className="mono">PO-1002</td>
                      <td>Globex Components</td>
                      <td className="num">$800.00</td>
                      <td>
                        <span className="chip chip-accent">OPEN</span>
                      </td>
                      <td>
                        <span className="chip chip-muted">on time</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>

            <section className="panel">
              <header className="panel-head">
                <h2>Progress</h2>
              </header>
              <div className="content stack">
                <div className="progress">
                  <div className="progress-bar">
                    <div className="progress-fill" style={{ width: '100%' }} />
                  </div>
                  <div className="progress-label">
                    $12,500.00 <span className="muted">/ $12,500.00</span>
                  </div>
                </div>
                <div className="progress">
                  <div className="progress-bar">
                    <div className="progress-fill" style={{ width: '35%' }} />
                  </div>
                  <div className="progress-label">
                    $4,375.00 <span className="muted">/ $12,500.00</span>
                  </div>
                </div>
              </div>
            </section>

            <section className="panel">
              <header className="panel-head">
                <h2>Error envelope</h2>
              </header>
              <div className="content">
                <div className="banner banner-error">
                  <div className="banner-head">
                    <strong>RECEIPT_EXCEEDS_PO</strong>
                    <button type="button" className="btn btn-sm btn-ghost">
                      Retry
                    </button>
                  </div>
                  <div className="banner-detail">
                    po PO-1001 amount 12500.0, received 12500.0, requested 100.0
                  </div>
                  <div className="banner-cid">
                    correlation_id: 9f2c1a7b4e8d4c0fa1b2c3d4e5f60718
                  </div>
                </div>
              </div>
            </section>
          </div>
        </div>
      </main>
    </div>
  );
}
