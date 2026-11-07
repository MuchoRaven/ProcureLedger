import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { probeRole } from '../api/client';
import type { Role } from '../api/types';

export type Session = { token: string; role: Role };

type SessionContextValue = {
  session: Session | null;
  /** Probe the token and store the result. Throws if the token is rejected. */
  signIn: (token: string) => Promise<void>;
  signOut: () => void;
};

const STORAGE_KEY = 'procureledger.session';

const SessionContext = createContext<SessionContextValue>({
  session: null,
  signIn: async () => {},
  signOut: () => {},
});

/**
 * Read the stored session, if any.
 *
 * ``sessionStorage`` rather than ``localStorage`` on purpose: the token should
 * die with the tab, and two tabs holding different credentials is a feature
 * here - it is how you exercise the approval/execution split without signing
 * in and out. Neither store is a security boundary; any script on the origin
 * can read both, so the real control is that the token is short-lived and
 * scoped to one role.
 */
function loadStored(): Session | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Session>;
    if (typeof parsed?.token === 'string' && typeof parsed?.role === 'string') {
      return { token: parsed.token, role: parsed.role as Role };
    }
    return null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(loadStored);

  const signIn = useCallback(async (token: string) => {
    // ``probeRole`` throws on a bad token, so nothing is stored until the
    // credential is known to be real and its role is known.
    const role = await probeRole(token);
    const next: Session = { token, role };
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      // A full or disabled store must not block sign-in; the session simply
      // does not survive a reload.
    }
    setSession(next);
  }, []);

  const signOut = useCallback(() => {
    try {
      window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Ignore: the in-memory session is cleared below either way.
    }
    setSession(null);
  }, []);

  const value = useMemo(
    () => ({ session, signIn, signOut }),
    [session, signIn, signOut],
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession() {
  return useContext(SessionContext);
}
