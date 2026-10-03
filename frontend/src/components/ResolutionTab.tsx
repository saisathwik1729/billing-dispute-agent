import { FormEvent, useMemo, useState } from "react";
import { api, ApiError, newIdempotencyKey } from "../api";
import { ACTION_LABEL, KIND_LABEL, money, readingLabel, when } from "../format";
import type { CaseDetail, ResolutionOption } from "../types";
import { RequestDialog } from "./InvestigationTab";
import { Cites, Empty, ErrorBlock, Field, Modal, Pill, Spinner, useToast } from "./ui";

const FINANCIAL = ["credit", "rebill", "net_settlement"];

export default function ResolutionTab({ detail, onChanged, onTab }: { detail: CaseDetail; onChanged: () => void; onTab: (t: string) => void }) {
  const a = detail.latest_analysis;
  const [approving, setApproving] = useState<ResolutionOption | null>(null);
  const [requesting, setRequesting] = useState(false);
  const cur = detail.calculation?.currency || "USD";
  const paymentsGap = !!a?.missing_evidence.some((m) => m.evidence_kind === "payments");
  const labels: Record<string, string> = Object.fromEntries((detail.calculation?.discrepancies || []).map((d) => [
    d.id, d.key === "__invoice_total__" ? "Invoice total" : d.key.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()),
  ]));

  if (!a) return <Empty title="No resolution options yet"><p>Run the analysis to get options. Their amounts are computed by the engine, never by the AI.</p></Empty>;

  return (
    <div className="resolution">
      {a.stale && <div className="callout callout-stale">Approvals are paused: evidence changed after this analysis. Re-run it to refresh the options.</div>}
      <section>
        <h2>Options</h2>
        <p className="muted">The AI chose which discrepancies each option covers and wrote the rationale. The amount comes from the engine and already deducts credits in the payment history and adjustments approved here.</p>
        <div className="options">
          {a.options.map((o) => <OptionCard key={o.id} o={o} cur={cur} stale={a.stale} labels={labels} onApprove={() => setApproving(o)} onRequest={() => setRequesting(true)} onTab={onTab} />)}
        </div>
      </section>

      <section>
        <h2>Approved adjustments</h2>
        {detail.adjustments.length === 0 ? <p className="muted">None yet. Approved credits are mock credit notes: nothing is posted to a real ledger.</p> : (
          <table className="ledger">
            <thead><tr><th>Reference</th><th>Type</th><th className="num">Amount</th><th>Covers</th><th>Approved</th><th>Note</th></tr></thead>
            <tbody>
              {detail.adjustments.map((x) => (
                <tr key={x.id}>
                  <td className="ref-cell">{x.id}</td>
                  <td>{x.kind === "credit" ? "Credit note" : "Debit note"}</td>
                  <td className="num">{money(x.amount, x.currency)}{x.amount !== x.computed_amount && <div className="muted small">calculated {money(x.computed_amount)}</div>}</td>
                  <td>{x.discrepancy_ids.join(", ")}</td>
                  <td>{x.approved_by}<div className="muted small">{when(x.created_at)}</div></td>
                  <td>{x.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <div className="findings-head">
          <h2>Information requests</h2>
          <button className="btn btn-sm" onClick={() => setRequesting(true)}>Request information</button>
        </div>
        {detail.info_requests.length === 0 ? <p className="muted">No requests sent.</p> : (
          <ul className="req-list">
            {detail.info_requests.map((r) => <RequestRow key={r.id} r={r} onChanged={onChanged} />)}
          </ul>
        )}
      </section>

      <CloseCase detail={detail} onChanged={onChanged} />

      {approving && <ApproveDialog caseId={detail.case.id} o={approving} cur={cur} paymentsGap={paymentsGap} onClose={() => setApproving(null)} onDone={() => { setApproving(null); onChanged(); }} />}
      {requesting && <RequestDialog caseId={detail.case.id} need={null} onClose={() => setRequesting(false)} onDone={() => { setRequesting(false); onChanged(); }} />}
    </div>
  );
}

function OptionCard({ o, cur, stale, labels, onApprove, onRequest, onTab }: { o: ResolutionOption; cur: string; stale: boolean; labels: Record<string, string>; onApprove: () => void; onRequest: () => void; onTab: (t: string) => void }) {
  const live = o.live;
  const financial = FINANCIAL.includes(o.action);
  const amount = live ? Number(live.amount) : 0;
  const blocked = stale ? "Re-run the analysis first." : o.unaccepted_discrepancies.length ? `Accept the findings covering ${o.unaccepted_discrepancies.join(", ")} first.` : amount === 0 ? "Nothing left to adjust." : "";
  return (
    <article className="option">
      <div className="option-top">
        <Pill tone={financial ? "ink" : "grey"}>{ACTION_LABEL[o.action] || o.action}</Pill>
        {o.source === "fallback" && <Pill tone="amber">Deterministic</Pill>}
      </div>
      <h3>{o.title}</h3>
      {financial && live && (
        <div className={`option-amount ${amount > 0 ? "is-credit" : amount < 0 ? "is-debit" : ""}`}>
          {amount > 0 ? "Credit " : amount < 0 ? "Charge " : ""}{money(Math.abs(amount), cur)}
          {live.amount !== o.proposed?.amount && <span className="muted small"> (was {money(o.proposed?.amount)} when proposed)</span>}
        </div>
      )}
      <p className="ai-text">{o.rationale}</p>
      {Object.keys(o.ambiguity_choices || {}).length > 0 && (
        <p className="small">Readings: {Object.entries(o.ambiguity_choices).map(([k, v]) => <span key={k} className="reading-chip"><span className="ref">{k}</span> {readingLabel(v)}</span>)}</p>
      )}
      {financial && live && live.items.length > 0 && (
        <ul className="calc-lines" aria-label="How the amount is calculated">
          {live.items.map((it) => {
            const credited = Number(it.prior_credit) + Number(it.already_adjusted);
            return (
              <li key={it.discrepancy_id} title={it.note || undefined}>
                <span className="calc-name">{labels[it.discrepancy_id] || it.discrepancy_id}{it.note && <span className="note-mark"> *</span>} <span className="ref">{it.discrepancy_id}</span></span>
                <span className="calc-eq">
                  {money(it.gross)}
                  {credited !== 0 && <> − {money(credited)} <span className="muted">already credited</span></>}
                  {" "}= <strong>{money(it.net)}</strong>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {live?.items.some((it) => it.note) && <p className="muted small">* {live.items.find((it) => it.note)?.note}</p>}
      {[...(o.flags || [])].filter((f, i, arr) => arr.indexOf(f) === i).map((f, i) => <div key={i} className="flag">{f}</div>)}
      <div className="finding-cites"><Cites list={o.citations} /></div>
      <div className="finding-actions">
        {financial ? (
          <>
            <button className="btn btn-primary btn-sm" disabled={!!blocked} onClick={onApprove}>Approve {amount < 0 ? "charge" : "credit"}</button>
            {blocked && <span className="muted small">{blocked}</span>}
            {o.unaccepted_discrepancies.length > 0 && !stale && <button className="link-btn" onClick={() => onTab("investigation")}>Review findings</button>}
          </>
        ) : o.action === "request_information" ? (
          <button className="btn btn-sm" onClick={onRequest}>Request information</button>
        ) : <span className="muted small">No change to the invoice.</span>}
      </div>
    </article>
  );
}

function ApproveDialog({ caseId, o, cur, paymentsGap, onClose, onDone }: { caseId: string; o: ResolutionOption; cur: string; paymentsGap: boolean; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const computed = o.live?.amount || "0.00";
  const key = useMemo(() => newIdempotencyKey(), []); // one key per dialog: retries can't double-post
  const [amount, setAmount] = useState(Math.abs(Number(computed)).toFixed(2));
  const [note, setNote] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const sign = Number(computed) < 0 ? -1 : 1;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    const n = Number(amount);
    if (!/^\d+(\.\d{1,2})?$/.test(amount.trim()) || n <= 0) errs.amount = "Enter an amount like 120.00.";
    else if (n > Math.abs(Number(computed))) errs.amount = `Can't exceed the calculated ${money(Math.abs(Number(computed)))}.`;
    if (note.trim().length < 5) errs.note = "Explain the decision (at least 5 characters).";
    if (paymentsGap && !ack) errs.ack = "Confirm you have checked for earlier credits.";
    setFieldErr(errs);
    if (Object.keys(errs).length) return;
    setBusy(true); setError(null);
    try {
      const r = await api(`/api/cases/${caseId}/adjustments`, {
        method: "POST", headers: { "Idempotency-Key": key },
        body: { option_id: o.id, note, amount: (sign * n).toFixed(2), acknowledge_unverified_payments: ack },
      });
      toast("ok", r.replayed ? `Already approved as ${r.id}; no duplicate created.` : `Approved ${r.kind === "credit" ? "credit" : "charge"} ${r.id} for ${money(Math.abs(Number(r.amount)), cur)}.`);
      onDone();
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && err.code === "already_adjusted") toast("info", "This was already adjusted. Refreshing.");
    } finally { setBusy(false); }
  }

  return (
    <Modal title={`Approve ${sign < 0 ? "additional charge" : "credit"}`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <p><strong>{o.title}</strong></p>
        <p className="muted">This records a mock {sign < 0 ? "debit" : "credit"} note in the case. No payment or accounting system is touched.</p>
        <Field label={`Amount (${cur})`} error={fieldErr.amount} hint={`Calculated: ${money(Math.abs(Number(computed)))}. You can approve less, never more.`}>
          <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        </Field>
        <Field label="Decision note" error={fieldErr.note} hint="Saved to the case history with your name.">
          <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        {paymentsGap && (
          <label className={`check check-warn ${fieldErr.ack ? "field-error" : ""}`}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            The payment and credit history was not available to this analysis. I have checked that no credit was already issued for this.
          </label>
        )}
        {fieldErr.ack && <span className="field-err">{fieldErr.ack}</span>}
        <ErrorBlock error={error} />
        <div className="modal-actions">
          <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy}>{busy ? <><Spinner /> Approving</> : `Approve ${money(Number(amount) || 0, cur)}`}</button>
        </div>
      </form>
    </Modal>
  );
}

function RequestRow({ r, onChanged }: { r: CaseDetail["info_requests"][number]; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function cancel() {
    setBusy(true);
    try { await api(`/api/info-requests/${r.id}/cancel`, { method: "POST" }); toast("ok", "Request cancelled."); onChanged(); }
    catch (e) { toast("error", (e as Error).message); } finally { setBusy(false); }
  }
  return (
    <li>
      <div>
        <strong>{KIND_LABEL[r.evidence_kind] || r.evidence_kind}</strong> <Pill tone={r.status === "open" ? "amber" : r.status === "fulfilled" ? "green" : "grey"}>{r.status}</Pill>
        <p>{r.message}</p>
        <span className="muted small">{r.id}, by {r.requested_by}, {when(r.created_at)}{r.fulfilled_by_evidence_id && `, fulfilled by ${r.fulfilled_by_evidence_id}`}</span>
      </div>
      {r.status === "open" && <button className="btn btn-sm btn-quiet" disabled={busy} onClick={cancel}>Cancel</button>}
    </li>
  );
}

function CloseCase({ detail, onChanged }: { detail: CaseDetail; onChanged: () => void }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const resolved = detail.case.status === "resolved";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      if (resolved) await api(`/api/cases/${detail.case.id}/reopen`, { method: "POST", body: { reason: text } });
      else await api(`/api/cases/${detail.case.id}/resolve`, { method: "POST", body: { summary: text } });
      toast("ok", resolved ? "Case reopened." : "Case resolved.");
      setText("");
      onChanged();
    } catch (err) { setError(err); } finally { setBusy(false); }
  }

  return (
    <section className="close-case">
      <h2>{resolved ? "Resolved" : "Resolve the case"}</h2>
      {resolved && <p className="ai-free">{detail.case.resolution_summary}</p>}
      <p className="muted">{resolved ? "Adding evidence reopens the case automatically. You can also reopen it by hand." : "Requires a current analysis, every finding reviewed, and no open information requests."}</p>
      <form onSubmit={submit} noValidate className="inline-form">
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder={resolved ? "Why is the case being reopened?" : "Resolution summary for the record"} aria-label={resolved ? "Reason for reopening" : "Resolution summary"} />
        <button className="btn" disabled={busy}>{busy ? <Spinner /> : resolved ? "Reopen case" : "Resolve case"}</button>
      </form>
      <ErrorBlock error={error} />
    </section>
  );
}
