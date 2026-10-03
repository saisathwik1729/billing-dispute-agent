import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { money, STATUS_LABEL } from "../format";
import type { CaseSummary, Sample } from "../types";
import { ErrorBlock, Field, Modal, Skeleton, Spinner, useToast } from "./ui";

type Props = {
  cases: CaseSummary[] | null; error: unknown; selected: string | null; user: string; health: any;
  onSelect: (id: string) => void; onReload: () => void; onCreated: (id: string) => void; onSignOut: () => void;
};

export default function CaseRail({ cases, error, selected, user, health, onSelect, onReload, onCreated, onSignOut }: Props) {
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const shown = (cases || []).filter((c) => `${c.title} ${c.customer_name} ${c.id}`.toLowerCase().includes(filter.toLowerCase()));

  return (
    <aside className="rail" aria-label="Disputes">
      <div className="rail-head">
        <div className="brand"><span className="brand-mark" aria-hidden="true" />Dispute desk</div>
        <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>New case</button>
      </div>
      <input className="rail-search" placeholder="Filter by customer or title" value={filter}
        onChange={(e) => setFilter(e.target.value)} aria-label="Filter cases" />
      <div className="rail-list">
        {error ? <ErrorBlock error={error} onRetry={onReload} /> : null}
        {!cases && !error && <Skeleton lines={6} />}
        {cases && shown.length === 0 && (
          <p className="rail-empty">{cases.length ? "No case matches that filter." : "No disputes yet. Create one, or start from a sample."}</p>
        )}
        {shown.map((c) => (
          <button key={c.id} className={`rail-item ${selected === c.id ? "is-selected" : ""}`} onClick={() => onSelect(c.id)}
            aria-current={selected === c.id ? "page" : undefined}>
            <span className="rail-customer">{c.customer_name}</span>
            <span className="rail-title">{c.title}</span>
            <span className="rail-meta">
              <span className={`dot dot-${c.status}`} aria-hidden="true" />{STATUS_LABEL[c.status] || c.status}
              {c.stale && <span className="tag tag-stale">Stale</span>}
              {c.confirmed_net_difference && Number(c.confirmed_net_difference) !== 0 && (
                <span className="rail-amount">{money(c.confirmed_net_difference)}</span>
              )}
            </span>
          </button>
        ))}
      </div>
      <div className="rail-foot">
        <span>Signed in as <strong>{user}</strong></span>
        <button className="link-btn" onClick={onSignOut}>Sign out</button>
        {health && !health.llm.configured && <p className="rail-warn">No model key configured: analyses use the deterministic fallback.</p>}
      </div>
      {creating && <NewCaseDialog onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); onCreated(id); }} />}
    </aside>
  );
}

function NewCaseDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const toast = useToast();
  const [samples, setSamples] = useState<Sample[] | null>(null);
  const [form, setForm] = useState({ title: "", customer_name: "", dispute_description: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  useEffect(() => { api<Sample[]>("/api/samples").then(setSamples).catch(setFailure); }, []);

  async function createBlank(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!form.title.trim()) errs.title = "Give the case a short title.";
    if (!form.customer_name.trim()) errs.customer_name = "Name the customer.";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy("blank"); setFailure(null);
    try {
      const c = await api("/api/cases", { method: "POST", body: form });
      toast("ok", `Case ${c.id} opened. Add the evidence next.`);
      onCreated(c.id);
    } catch (err) { setFailure(err); } finally { setBusy(null); }
  }

  async function fromSample(key: string) {
    setBusy(key); setFailure(null);
    try {
      const c = await api(`/api/samples/${key}/cases`, { method: "POST" });
      toast("ok", `Sample case ${c.id} opened with its evidence.`);
      onCreated(c.id);
    } catch (err) { setFailure(err); } finally { setBusy(null); }
  }

  return (
    <Modal title="Open a dispute case" onClose={onClose} wide>
      <div className="split">
        <form onSubmit={createBlank} noValidate>
          <h3 className="sub">Your own dispute</h3>
          <Field label="Title" error={errors.title}>
            <input value={form.title} maxLength={200} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="e.g. August API overage" />
          </Field>
          <Field label="Customer" error={errors.customer_name}>
            <input value={form.customer_name} maxLength={200} onChange={(e) => setForm({ ...form, customer_name: e.target.value })} />
          </Field>
          <Field label="What the customer says" hint="Paste their email or ticket. You can also attach it as evidence later.">
            <textarea rows={5} value={form.dispute_description} onChange={(e) => setForm({ ...form, dispute_description: e.target.value })} />
          </Field>
          <button className="btn btn-primary" disabled={!!busy}>{busy === "blank" ? <><Spinner /> Opening</> : "Open case"}</button>
        </form>
        <div>
          <h3 className="sub">Or start from a sample</h3>
          {!samples && !failure && <Skeleton lines={5} />}
          <ul className="sample-list">
            {(samples || []).map((s) => (
              <li key={s.key}>
                <div>
                  <strong>{s.title}</strong>
                  <span className="muted">{s.customer_name}. {s.summary}</span>
                </div>
                <button className="btn btn-sm" onClick={() => fromSample(s.key)} disabled={!!busy}>
                  {busy === s.key ? <Spinner /> : "Open"}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
      <ErrorBlock error={failure} />
    </Modal>
  );
}
