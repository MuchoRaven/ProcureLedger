import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The backend is a single-worker FastAPI process: ``app.py`` takes an
 * exclusive ``flock`` on ``<AUDIT_LOG_PATH>.lock`` at boot, so a second worker
 * fails to start rather than forking the audit chain.
 *
 * The proxy exists so the browser sees one origin. Without it every request
 * would be cross-origin, which means a CORS preflight on every call and a
 * ``PL_*`` token crossing an origin boundary the backend never agreed to.
 *
 * ``loadEnv`` is used rather than ``process.env`` so this file typechecks
 * without ``@types/node``.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8000';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        // One entry per top-level route in ``app.py``. Kept explicit rather
        // than a catch-all so a typo in a path fails loudly in the browser
        // instead of being silently proxied.
        '^/(agent|invoices|pos|purchases|requisitions|approvals|rfqs|quotes|followup|audit|health|ready)':
          {
            target,
            changeOrigin: true,
          },
      },
    },
  };
});
