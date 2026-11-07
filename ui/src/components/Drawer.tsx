import { useEffect, type ReactNode } from 'react';

/**
 * A right-side drawer.
 *
 * Escape closes it, and a click on the backdrop closes it; a click inside the
 * drawer does not, so a form in the body is not dismissed by a stray click.
 */
export function Drawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(event) => event.stopPropagation()}>
        <header className="drawer-head">
          <h2>{title}</h2>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={onClose}
          >
            Close
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}
