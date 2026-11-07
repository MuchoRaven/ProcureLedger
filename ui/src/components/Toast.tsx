import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react';

export type ToastKind = 'ok' | 'error' | 'info';

type ToastItem = {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  cid?: string;
};

type ToastContextValue = {
  push: (toast: Omit<ToastItem, 'id'>) => void;
};

const ToastContext = createContext<ToastContextValue>({ push: () => {} });

export function useToast() {
  return useContext(ToastContext);
}

/**
 * Transient notifications.
 *
 * Every error toast carries the ``correlation_id``, because that is the only
 * handle support has: the server logs the same value, and without it a report
 * of "the approve button failed" cannot be tied to a request.
 *
 * Errors live longer than successes (12s vs 5s) - a success is confirmation,
 * an error is something the user has to read and possibly act on.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((toast: Omit<ToastItem, 'id'>) => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev, { ...toast, id }]);
    const ttl = toast.kind === 'error' ? 12000 : 5000;
    window.setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id));
    }, ttl);
  }, []);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((item) => (
          <div key={item.id} className={`toast toast-${item.kind}`}>
            <div className="toast-title">{item.title}</div>
            {item.detail ? (
              <div className="toast-detail">{item.detail}</div>
            ) : null}
            {item.cid ? (
              <div className="toast-cid">correlation_id: {item.cid}</div>
            ) : null}
            <button
              type="button"
              className="toast-close"
              aria-label="Dismiss"
              onClick={() => dismiss(item.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
