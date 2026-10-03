import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { KIND_LABEL, when } from "../format";
import type { CaseDetail, Evidence } from "../types";
import { Empty, ErrorBlock, Field, Modal, Pill, Skeleton, Spinner, useToast } from "./ui";

const FORMAT_HINT: Record<string, string> = {
  invoice: 'JSON: {"invoice_id", "currency", "period": {"start","end"}, "lines": [{"line_id","sku","description","quantity","unit_price","amount"}], "total"}',
  contract: 'JSON: {"contract_id", "rules": [{"id","type","sku",...}]}. Rule types: flat, per_unit, tiered, included_quota, discount_percent, minimum_commit, usage_exclusion, clause.',
  usage: 'JSON {"events": [...]} or CSV with columns event_id,timestamp,sku,quantity,tags. Bad rows are skipped with a warning.',
  payments: 'JSON {"entries": [...]} or CSV with columns id,type,date,amount,invoice_id,applies_to_lines,reason. Types: payment, credit, adjustment, refund.',
  dispute: "Plain text: the customer's email or ticket.",
  note: "Plain text: call notes, emails, or anything that helps interpret the contract.",
};

export default function EvidenceTab({ detail, onChanged }: { detail: CaseDetail; onChanged: () => void }) {
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [viewing, setViewing] = useState<string | null>(null);
  const [withdrawing, setWithdrawing] = useState<Evidence | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const active = detail.evidence.filter((e) => e.active);
  const inactive = detail.evidence.filter((e) => !e.active);
  const present = new Set(active.map((e) => e.kind));

  async function addFollowup(index: number) {
    setBusy(index); setError(null);
    try {
      await api(`/api/cases/${detail.case.id}/sample-followups/${index}`, { method: "POST" });
      toast("ok", "Evidence added. Earlier conclusions that depend on it are now marked stale.");
      onChanged();
    } catch (e) { setError(e); } finally { setBusy(null); }
  }

  const followupsLeft = detail.sample_followups.filter((f) => !detail.evidence.some((e) => e.filename === f.filename));

  return (
    <div className="evidence-tab">
      <div className="findings-head">
        <h2>Evidence on file</h2>
        <button className="btn btn-primary btn-sm" onClick={() => setAdding(true)}>Add evidence</button>
      </div>
      <div className="checklist" aria-label="Evidence checklist">
        {["invoice", "contract", "usage", "payments", "dispute"].map((k) => {
          const ok = present.has(k) || (k === "dispute" && !!detail.case.dispute_description);
          return <span key={k} className={`check-item ${ok ? "is-ok" : ""}`}>{ok ? "✓" : "○"} {KIND_LABEL[k]}</span>;
        })}
      </div>
      {detail.case.dispute_description && (
        <blockquote className="dispute-quote"><span className="muted small">Customer's statement</span>{detail.case.dispute_description}</blockquote>
      )}
      {active.length === 0 ? (
        <Empty title="No evidence yet"><p>Add the invoice, the contract's pricing rules, usage events and payment history. Each file is validated as you add it.</p></Empty>
      ) : (
        <ul className="ev-list">
          {active.map((e) => (
            <li key={e.id}>
              <div>
                <span className="ev-kind">{KIND_LABEL[e.kind] || e.kind}</span>
                <strong>{e.filename}</strong>
                <span className="muted"> {e.summary}</span>
                <div className="muted small">{e.id}, added by {e.added_by}, {when(e.created_at)}</div>
                {e.warnings.length > 0 && (
                  <details className="warn-details"><summary>{e.warnings.length} warning(s) during import</summary><ul>{e.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></details>
                )}
              </div>
              <div className="ev-actions">
                <button className="btn btn-sm btn-quiet" onClick={() => setViewing(e.id)}>View</button>
                <button className="btn btn-sm btn-quiet" onClick={() => setWithdrawing(e)}>Withdraw</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {inactive.length > 0 && (
        <details className="inactive">
          <summary>{inactive.length} replaced or withdrawn document(s)</summary>
          <ul className="ev-list ev-inactive">
            {inactive.map((e) => (
              <li key={e.id}>
                <div><span className="ev-kind">{KIND_LABEL[e.kind]}</span> <strong>{e.filename}</strong>
                  <div className="muted small">{e.id}: {e.superseded_by ? `replaced by ${e.superseded_by}` : `withdrawn: ${e.withdrawn_reason}`}</div></div>
                <button className="btn btn-sm btn-quiet" onClick={() => setViewing(e.id)}>View</button>
              </li>
            ))}
          </ul>
        </details>
      )}

      {followupsLeft.length > 0 && (
        <section className="followups">
          <h3 className="sub">Sample follow-up evidence</h3>
          <p className="muted">This sample case has evidence that "arrives later". Adding it reopens a resolved case and marks the affected conclusions stale.</p>
          {followupsLeft.map((f) => (
            <div key={f.index} className="followup">
              <span><Pill tone="grey">{KIND_LABEL[f.kind]}</Pill> {f.label}</span>
              <button className="btn btn-sm" disabled={busy !== null} onClick={() => addFollowup(f.index)}>{busy === f.index ? <Spinner /> : "Add to case"}</button>
            </div>
          ))}
        </section>
      )}
      <ErrorBlock error={error} />

      {adding && <AddEvidence detail={detail} onClose={() => setAdding(false)} onDone={() => { setAdding(false); onChanged(); }} />}
      {viewing && <ViewEvidence id={viewing} onClose={() => setViewing(null)} />}
      {withdrawing && <Withdraw caseId={detail.case.id} ev={withdrawing} onClose={() => setWithdrawing(null)} onDone={() => { setWithdrawing(null); onChanged(); }} />}
    </div>
  );
}

function AddEvidence({ detail, onClose, onDone }: { detail: CaseDetail; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [kind, setKind] = useState("invoice");
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [replaces, setReplaces] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [local, setLocal] = useState("");
  const sameKind = detail.evidence.filter((e) => e.active && e.kind === kind);

  function onFile(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    if (f.size > 2_000_000) { setLocal("That file is over 2 MB."); return; }
    setLocal("");
    setFilename(f.name);
    const reader = new FileReader();
    reader.onload = () => setContent(String(reader.result || ""));
    reader.onerror = () => setLocal("The file could not be read.");
    reader.readAsText(f);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!content.trim()) { setLocal("Choose a file or paste the content."); return; }
    setBusy(true); setError(null); setLocal("");
    try {
      const r = await api(`/api/cases/${detail.case.id}/evidence`, { method: "POST", body: { kind, filename: filename || `${kind}.txt`, content, replaces_id: replaces || null } });
      toast(r.warnings.length ? "info" : "ok", r.warnings.length ? `Added with ${r.warnings.length} warning(s); invalid rows were skipped.` : `Added ${r.evidence.filename}.`);
      onDone();
    } catch (err) { setError(err); } finally { setBusy(false); }
  }

  return (
    <Modal title="Add evidence" onClose={onClose} wide>
      <form onSubmit={submit} noValidate>
        <div className="split">
          <Field label="Type">
            <select value={kind} onChange={(e) => { setKind(e.target.value); setReplaces(""); }}>
              {Object.entries(KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </Field>
          {sameKind.length > 0 && (
            <Field label="Replaces" hint={kind === "invoice" || kind === "contract" ? "A new invoice or contract always replaces the current one." : "Leave empty to add alongside the existing files."}>
              <select value={replaces} onChange={(e) => setReplaces(e.target.value)}>
                <option value="">{kind === "invoice" || kind === "contract" ? "The current file" : "Nothing, add alongside"}</option>
                {sameKind.map((e) => <option key={e.id} value={e.id}>{e.filename} ({e.id})</option>)}
              </select>
            </Field>
          )}
        </div>
        <p className="format-hint">{FORMAT_HINT[kind]}</p>
        <Field label="File"><input type="file" accept=".json,.csv,.txt,.md" onChange={onFile} /></Field>
        <Field label="Or paste the content">
          <textarea rows={10} className="mono-area" value={content} onChange={(e) => setContent(e.target.value)} spellCheck={false} />
        </Field>
        {local && <div className="callout callout-error">{local}</div>}
        <ErrorBlock error={error} />
        <div className="modal-actions">
          <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy}>{busy ? <><Spinner /> Validating</> : "Add evidence"}</button>
        </div>
      </form>
    </Modal>
  );
}

function ViewEvidence({ id, onClose }: { id: string; onClose: () => void }) {
  const [ev, setEv] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api(`/api/evidence/${id}`).then(setEv).catch(setError); }, [id]);
  return (
    <Modal title={ev ? ev.filename : "Evidence"} onClose={onClose} wide>
      <ErrorBlock error={error} />
      {!ev && !error && <Skeleton lines={8} />}
      {ev && <pre className="raw">{ev.raw_text}</pre>}
    </Modal>
  );
}

function Withdraw({ caseId, ev, onClose, onDone }: { caseId: string; ev: Evidence; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api(`/api/cases/${caseId}/evidence/${ev.id}/withdraw`, { method: "POST", body: { reason } });
      toast("ok", `Withdrew ${ev.filename}. It stays in the history.`);
      onDone();
    } catch (err) { setError(err); } finally { setBusy(false); }
  }
  return (
    <Modal title="Withdraw evidence" onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <p>{ev.filename} will stop counting as evidence. It is kept for the audit trail, and analyses that used it become stale.</p>
        <Field label="Reason"><textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <ErrorBlock error={error} />
        <div className="modal-actions">
          <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button className="btn btn-danger" disabled={busy}>{busy ? <Spinner /> : "Withdraw"}</button>
        </div>
      </form>
    </Modal>
  );
}
