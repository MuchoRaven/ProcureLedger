import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, call, type RequestOptions } from './client';
import { useSession } from '../auth/session';

type ResourceState<T> = {
  data: T | null;
  error: Error | null;
  /** True only while there is no data yet, so polling does not flicker. */
  loading: boolean;
  /** True while a refetch is in flight over existing data. */
  refreshing: boolean;
};

/**
 * A polling read.
 *
 * ``key`` must change whenever the request would change (the ``as_of`` date,
 * the page offset, the selected id). ``interval`` re-runs the fetch on a timer
 * so the monitoring views stay live without a websocket.
 *
 * ``loading`` is deliberately "no data yet" rather than "a request is in
 * flight": a 30s poll that flipped ``loading`` would blank the table every
 * cycle. ``refreshing`` carries the in-flight signal for a subtle indicator.
 *
 * The fetcher is held in a ref so an inline arrow does not re-trigger the
 * effect on every render; ``key`` is the only thing that should.
 */
export function useResource<T>(
  key: string,
  fetcher: () => Promise<T>,
  options: { interval?: number; enabled?: boolean } = {},
) {
  const { interval, enabled = true } = options;
  const [state, setState] = useState<ResourceState<T>>({
    data: null,
    error: null,
    loading: enabled,
    refreshing: false,
  });
  const [nonce, setNonce] = useState(0);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    setState((prev) => ({
      data: prev.data,
      error: null,
      loading: prev.data === null,
      refreshing: prev.data !== null,
    }));

    fetcherRef.current().then(
      (data) => {
        if (cancelled) return;
        setState({ data, error: null, loading: false, refreshing: false });
      },
      (error: Error) => {
        if (cancelled) return;
        setState((prev) => ({
          data: prev.data,
          error,
          loading: false,
          refreshing: false,
        }));
      },
    );

    return () => {
      cancelled = true;
    };
  }, [key, nonce, enabled]);

  useEffect(() => {
    if (!enabled || !interval) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), interval);
    return () => window.clearInterval(id);
  }, [interval, enabled]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { ...state, refresh };
}

type ActionState = {
  running: boolean;
  error: Error | null;
  result: any;
};

/**
 * A mutation.
 *
 * Deliberately not optimistic. ``_commit`` is audit-before-mutation, so a
 * failed write means *nothing happened* - the UI must show "not applied",
 * never "unknown", and that is only possible if the result is awaited before
 * any local state changes.
 *
 * ``run`` resolves to a discriminated result rather than throwing, so a caller
 * can branch on ``ok`` without a try/catch around every button handler.
 */
export function useAction() {
  const [state, setState] = useState<ActionState>({
    running: false,
    error: null,
    result: null,
  });

  const run = useCallback(async (fn: () => Promise<any>) => {
    setState({ running: true, error: null, result: null });
    try {
      const result = await fn();
      setState({ running: false, error: null, result });
      return { ok: true as const, result };
    } catch (err) {
      setState({ running: false, error: err as Error, result: null });
      return { ok: false as const, error: err as Error };
    }
  }, []);

  const reset = useCallback(() => {
    setState({ running: false, error: null, result: null });
  }, []);

  return { ...state, run, reset };
}

/**
 * Bind the session token to ``call`` so views never thread it by hand.
 *
 * A 401 means the stored token is no longer accepted - expired, rotated, or
 * the deployment changed its credentials. Rather than let every view render a
 * 401 banner forever, the session is dropped and the login screen returns.
 * The error is still re-thrown so the caller's own handling runs first.
 *
 * Only 401 triggers this. A 403 is a role problem, not a credential problem:
 * signing out would be the wrong response to "this endpoint needs the operator
 * credential", and the nav is built so a role never reaches one.
 */
export function useApi() {
  const { session, signOut } = useSession();
  const token = session?.token ?? '';

  const request = useCallback(
    async <T,>(path: string, init?: RequestOptions): Promise<T> => {
      try {
        return await call<T>(path, token, init);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          signOut();
        }
        throw err;
      }
    },
    [token, signOut],
  );

  return { token, request };
}
