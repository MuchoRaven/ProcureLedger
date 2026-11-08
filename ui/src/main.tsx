import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles.css';
// Imported after ``styles.css`` so the Overview-only dark theme wins on equal
// specificity. Every rule in it is scoped to ``.shell-dark`` / ``.ov-root``.
import './styles-dark.css';

const root = document.getElementById('root');
if (!root) {
  throw new Error('root element missing from index.html');
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
