import { useEffect, useState } from "react";
import { api } from "../api";
import { STEP_LABEL, when } from "../format";
import type { CaseDetail, Run } from "../types";
import { Empty, ErrorBlock, Modal, Pill, Skeleton } from "./ui";

const STATUS_TONE: Record<string, string> = { ok: "green", partial: "amber", failed: "red", skipped: "grey" };
const RUN_TONE: Record<string, string> = { completed: "green", completed_with_warnings: "amber", failed: "red", running: "carbon", queued: "grey" };

export default function TraceTab({ detail, liveRun }: { detail: CaseDetail; liveRun: Run | null }) {
  const [selected, setSelected] = useState<string | null>(detail.runs[0]?.id || null);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [raw, setRaw] = useState<string | null>(null);

  // Follow a run that is in progress, so its steps appear live.
  useEffect(() => {
    if (liveRun) setSelected(liveRun.id);
  }, [liveRun?.id]);

  // Load a run only when a different run is selected (not on every progress poll).
  useEffect(() => {
    if (!selected) return;
    if (liveRun && liveRun.id === selected && liveRun.steps) return;
    setRun(null);
    api<Run>(`/api/runs/${selected}`).then(setRun).catch(setError);
  }, [selected]);

  // Keep the live run's steps up to date as they arrive.
  useEffect(() => {
    if (liveRun && liveRun.id === selected && liveRun.steps) setRun(liveRun);
  }, [liveRun, selected]);

  if (!detail.runs.length) return <Empty title="No runs yet"><p>Each analysis run records every tool step it took, how long it took, and what failed.</p></Empty>;
  const analysis = detail.analyses.find((a) => detail.latest_analysis && a.id === detail.latest_analysis.id);

  async function showRaw() {
    if (!detail.latest_analysis) return;
    try { setRaw(await api<string>(`/api/analyses/${detail.latest_analysis.id}/raw`, { text: true })); }
    catch (e) { setError(e); }
  }

  return (
    <div className="trace">
      <div className="trace-runs" role="listbox" aria-label="Analysis runs">
        {detail.runs.map((r) => (
          <button key={r.id} role="option" aria-selected={selected === r.id} className={`run-item ${selected === r.id ? "is-selected" : ""}`} onClick={() => setSelected(r.id)}>
            <span className="ref">{r.id}</span>
            <Pill tone={RUN_TONE[r.status] || "grey"}>{r.status.replace(/_/g, " ")}</Pill>
            <span className="muted small">{when(r.created_at)}, by {r.triggered_by}</span>
            {r.simulate_failures.length > 0 && <span className="muted small">simulated: {r.simulate_failures.join(", ")}</span>}
          </button>
        ))}
      </div>
      <div className="trace-steps">
        <ErrorBlock error={error} />
        {!run && <Skeleton lines={7} />}
        {run && (
          <>
            <p className="muted">Model {run.provider}/{run.model}. {run.error && <span className="red">Run error: {run.error}</span>}</p>
            <ol className="steps">
              {(run.steps || []).map((s) => (
                <li key={s.seq} className={`step step-${s.status}`}>
                  <div className="step-head">
                    <span className="step-name">{STEP_LABEL[s.name] || s.name}</span>
                    <Pill tone={STATUS_TONE[s.status] || "grey"}>{s.status}</Pill>
                    <span className="muted small">{s.duration_ms} ms</span>
                  </div>
                  <p>{s.summary}</p>
                  {s.error && s.status !== "ok" && <p className="step-err">{s.error}</p>}
                  {s.detail && Object.keys(s.detail).length > 0 && (
                    <details><summary>Details</summary><pre className="raw small">{JSON.stringify(s.detail, null, 2)}</pre></details>
                  )}
                </li>
              ))}
            </ol>
            {analysis && detail.latest_analysis?.run_id === run.id && (
              <button className="btn btn-sm btn-quiet" onClick={showRaw}>Show raw model output</button>
            )}
          </>
        )}
      </div>
      {raw !== null && <Modal title="Raw model output" onClose={() => setRaw(null)} wide><pre className="raw">{raw}</pre></Modal>}
    </div>
  );
}
