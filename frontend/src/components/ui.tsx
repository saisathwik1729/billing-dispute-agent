import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import type { Citation } from "../types";

/* ---------------------------------------------------------------- toasts */
type Toast = { id: number; tone: "ok" | "error" | "info"; text: string };
const ToastCtx = createContext<(tone: Toast["tone"], text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, tone, text }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), tone === "error" ? 7000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>{t.text}</div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ---------------------------------------------------------------- modal */
export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input, textarea, select, button")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- feedback states */
export function ErrorBlock({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : new ApiError(0, "error", String((error as Error)?.message || error));
  return (
    <div className="callout callout-error" role="alert">
      <strong>{e.message}</strong>
      {e.details.length > 0 && (
        <ul className="detail-list">{e.details.slice(0, 12).map((d, i) => <li key={i}>{d}</li>)}</ul>
      )}
      {onRetry && <button className="btn btn-quiet" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <svg width="44" height="52" viewBox="0 0 44 52" aria-hidden="true">
        <rect x="2" y="2" width="40" height="48" rx="3" fill="none" stroke="currentColor" strokeWidth="2" />
        <path d="M10 14h24M10 22h18M10 30h22" stroke="currentColor" strokeWidth="2" opacity=".45" />
      </svg>
      <p className="empty-title">{title}</p>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function Skeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }).map((_, i) => <div key={i} className="sk-line" style={{ width: `${92 - i * 11}%` }} />)}
    </div>
  );
}

export function Spinner() { return <span className="spinner" aria-hidden="true" />; }

/* ---------------------------------------------------------------- citations */
const CITE_KIND: Record<string, string> = {
  invoice_line: "Invoice line", rule: "Contract rule", usage_event: "Usage event", payment: "Payment record",
  discrepancy: "Calculated discrepancy", ambiguity: "Open question", data_quality: "Data issue", dispute: "Dispute", note: "Note",
};

export function Cite({ c }: { c: Citation }) {
  return (
    <span className={`cite cite-${c.type} ${c.origin === "engine" ? "cite-engine" : ""}`} tabIndex={0}
      aria-label={`${CITE_KIND[c.type] || c.type} ${c.ref}: ${c.label}`}>
      {c.ref}
      <span className="cite-tip" role="tooltip">
        <span className="cite-kind">{CITE_KIND[c.type] || c.type}{c.origin === "engine" ? ", linked by the engine" : ""}</span>
        {c.label}
      </span>
    </span>
  );
}

export function Cites({ list }: { list: Citation[] }) {
  if (!list?.length) return null;
  return <span className="cites">{list.map((c) => <Cite key={c.ref} c={c} />)}</span>;
}

/* ---------------------------------------------------------------- misc */
export function Pill({ tone, children }: { tone: string; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function Field({ label, hint, children, error }: { label: string; hint?: string; children: ReactNode; error?: string }) {
  return (
    <label className={`field ${error ? "field-error" : ""}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && !error && <span className="field-hint">{hint}</span>}
      {error && <span className="field-err">{error}</span>}
    </label>
  );
}
