import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { KIND_LABEL, money, PIPELINE, STATUS_LABEL, STEP_LABEL } from "../format";
import type { CaseDetail, Run } from "../types";
import EvidenceTab from "./EvidenceTab";
import HistoryTab from "./HistoryTab";
import InvestigationTab from "./InvestigationTab";
import InvoiceTab from "./InvoiceTab";
import ResolutionTab from "./ResolutionTab";
import TraceTab from "./TraceTab";
import { ErrorBlock, Skeleton, Spinner, useToast } from "./ui";

const TABS = [
  { key: "investigation", label: "Investigation" },
  { key: "invoice", label: "Invoice check" },
  { key: "resolution", label: "Resolution" },
  { key: "evidence", label: "Evidence" },
  { key: "trace", label: "Agent trace" },
  { key: "history", label: "History" },
];

export default function CaseView({ caseId, tab, onTab, onChanged }: { caseId: string; tab: string; onTab: (t: string) => void; onChanged: () => void }) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [run, setRun] = useState<Run | null>(null);
  const toast = useToast();
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api<CaseDetail>(`/api/cases/${caseId}`);
      setDetail(d); setError(null);
      return d;
    } catch (e) { setError(e); return null; }
  }, [caseId]);

  const refresh = useCallback(async () => { await load(); onChanged(); }, [load, onChanged]);

  const pollRun = useCallback((runId: string) => {
    if (pollRef.current) window.clearTimeout(pollRef.current);
    const tick = async () => {
      try {
        const r = await api<Run>(`/api/runs/${runId}`);
        setRun(r);
        if (r.status === "queued" || r.status === "running") {
          pollRef.current = window.setTimeout(tick, 900);
        } else {
          await refresh();
          if (r.status === "failed") toast("error", `Analysis failed: ${r.error || "see the agent trace"}`);
          else if (r.status === "completed_with_warnings") toast("info", "Analysis finished with warnings. Check the agent trace.");
          else toast("ok", "Analysis complete. Review the findings.");
        }
      } catch {
        pollRef.current = window.setTimeout(tick, 2500);
      }
    };
    tick();
  }, [refresh, toast]);

  useEffect(() => {
    load().then((d) => { if (d?.active_run) pollRun(d.active_run.id); });
    return () => { if (pollRef.current) window.clearTimeout(pollRef.current); };
  }, [load, pollRun]);

  if (error && !detail) return <div className="pad"><ErrorBlock error={error} onRetry={load} /></div>;
  if (!detail) return <div className="pad"><Skeleton lines={8} /></div>;

  const { case: c, latest_analysis: a, calculation: calc } = detail;
  const running = run && (run.status === "queued" || run.status === "running");
  const proposed = a ? a.findings.filter((f) => f.status === "proposed").length : 0;
  const approved = detail.adjustments.reduce((s, x) => s + Number(x.amount), 0);
  const cur = calc?.currency || "USD";

  return (
    <div className="case">
      <header className="case-head">
        <div className="case-id-row">
          <span className="case-id">{c.id}</span>
          <span className={`status status-${c.status}`}>{STATUS_LABEL[c.status] || c.status}</span>
        </div>
        <h1>{c.title}</h1>
        <p className="case-customer">{c.customer_name}{calc && <> &nbsp;/&nbsp; Invoice {calc.invoice_id}, {calc.period.start} to {calc.period.end}</>}</p>
        <div className="figures">
          <Figure label="Invoiced" value={calc ? money(calc.totals.invoiced_total, cur) : "—"} />
          <Figure label="Recalculated (confirmed errors only)" value={calc ? money(calc.totals.recalculated_total_confirmed, cur) : "—"} />
          <Figure label="Confirmed difference" value={calc ? money(calc.totals.confirmed_net_difference, cur) : "—"}
            tone={calc && Number(calc.totals.confirmed_net_difference) > 0 ? "red" : undefined} />
          <Figure label="Open contract questions" value={calc ? String(calc.ambiguities.length) : "—"}
            tone={calc && calc.ambiguities.length ? "amber" : undefined} />
          <Figure label="Approved adjustments" value={money(approved, cur)} />
        </div>
        <RunControl detail={detail} running={!!running} run={run} onStarted={(r) => { setRun(r); pollRun(r.id); }} />
        {a?.stale && !running && (
          <div className="callout callout-stale" role="status">
            <strong>Evidence changed after the last analysis.</strong> New or withdrawn {a.changed_kinds.map((k) => (KIND_LABEL[k] || k).toLowerCase()).join(", ")} evidence
            may change its conclusions. Findings that depend on it are marked; approvals and resolution are paused until you re-run.
          </div>
        )}
      </header>

      <nav className="tabs" role="tablist" aria-label="Case sections">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} className={`tab ${tab === t.key ? "is-active" : ""}`} onClick={() => onTab(t.key)}>
            {t.label}
            {t.key === "investigation" && proposed > 0 && <span className="count">{proposed}</span>}
            {t.key === "evidence" && <span className="count count-quiet">{detail.evidence.filter((e) => e.active).length}</span>}
          </button>
        ))}
      </nav>

      <section className="tab-body" role="tabpanel">
        {tab === "investigation" && <InvestigationTab detail={detail} onChanged={refresh} onTab={onTab} running={!!running} />}
        {tab === "invoice" && <InvoiceTab detail={detail} />}
        {tab === "resolution" && <ResolutionTab detail={detail} onChanged={refresh} onTab={onTab} />}
        {tab === "evidence" && <EvidenceTab detail={detail} onChanged={refresh} />}
        {tab === "trace" && <TraceTab detail={detail} liveRun={run} />}
        {tab === "history" && <HistoryTab caseId={caseId} version={detail.case.updated_at + detail.runs.length} />}
      </section>
    </div>
  );
}

function Figure({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className={`figure ${tone ? "figure-" + tone : ""}`}>
      <span className="figure-value">{value}</span>
      <span className="figure-label">{label}</span>
    </div>
  );
}

function RunControl({ detail, running, run, onStarted }: { detail: CaseDetail; running: boolean; run: Run | null; onStarted: (r: Run) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [open, setOpen] = useState(false);
  const [sim, setSim] = useState<string[]>([]);
  const has = !!detail.latest_analysis;
  const evidence = detail.evidence.filter((e) => e.active).length;

  async function start() {
    setBusy(true); setError(null);
    try {
      const r = await api<Run>(`/api/cases/${detail.case.id}/analyze`, { method: "POST", body: { simulate_failures: sim } });
      onStarted(r);
    } catch (e) { setError(e); } finally { setBusy(false); }
  }

  const steps = run?.steps || [];
  const done = new Map(steps.map((s) => [s.name, s]));
  return (
    <div className="runbar">
      <div className="runbar-row">
        <button className="btn btn-primary" onClick={start} disabled={busy || running || evidence === 0}>
          {running || busy ? <><Spinner /> Analysing</> : has ? "Re-run analysis" : "Run analysis"}
        </button>
        {evidence === 0 && <span className="muted">Add evidence first.</span>}
        <button className="link-btn" onClick={() => setOpen(!open)} aria-expanded={open}>Test failure handling</button>
      </div>
      {open && (
        <fieldset className="simbox">
          <legend>Simulate a failing tool on the next run</legend>
          {[["usage_lookup", "Usage store"], ["payments_lookup", "Payment ledger"], ["calculator", "Calculator"], ["llm", "Model provider"]].map(([k, l]) => (
            <label key={k} className="check">
              <input type="checkbox" checked={sim.includes(k)} onChange={(e) => setSim(e.target.checked ? [...sim, k] : sim.filter((x) => x !== k))} />{l}
            </label>
          ))}
        </fieldset>
      )}
      {running && (
        <ol className="progress" aria-live="polite">
          {PIPELINE.map((name) => {
            const s = done.get(name) || (name === "ai_analysis" ? done.get("fallback_analysis") : undefined);
            const current = !s && PIPELINE.findIndex((n) => !done.has(n)) === PIPELINE.indexOf(name);
            return (
              <li key={name} className={`progress-step ${s ? "st-" + s.status : current ? "st-current" : ""}`}>
                {STEP_LABEL[name]}
              </li>
            );
          })}
        </ol>
      )}
      <ErrorBlock error={error} />
    </div>
  );
}
