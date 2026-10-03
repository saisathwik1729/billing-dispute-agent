import { useEffect, useState } from "react";
import { api } from "../api";
import { when } from "../format";
import { Empty, ErrorBlock, Skeleton } from "./ui";

type Ev = { id: number; actor: string; type: string; summary: string; data: any; created_at: string };

const GROUP: Record<string, string> = {
  case_created: "case", status_changed: "case", case_reopened: "case",
  evidence_added: "evidence", evidence_withdrawn: "evidence", info_requested: "evidence", info_request_fulfilled: "evidence", info_request_cancelled: "evidence",
  analysis_started: "agent", analysis_completed: "agent", analysis_failed: "agent", analysis_stale: "agent",
  finding_accept: "review", finding_reject: "review", finding_edit: "review", finding_reset: "review",
  adjustment_approved: "money",
};

export default function HistoryTab({ caseId, version }: { caseId: string; version: string }) {
  const [events, setEvents] = useState<Ev[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [kind, setKind] = useState("all");

  useEffect(() => { api<Ev[]>(`/api/cases/${caseId}/events`).then(setEvents).catch(setError); }, [caseId, version]);

  if (error) return <ErrorBlock error={error} />;
  if (!events) return <Skeleton lines={8} />;
  if (!events.length) return <Empty title="No history yet" />;
  const shown = events.filter((e) => kind === "all" || GROUP[e.type] === kind);

  return (
    <div className="history">
      <div className="findings-head">
        <h2>Dispute and decision history</h2>
        <div className="seg" role="group" aria-label="Filter history">
          {[["all", "All"], ["review", "Reviews"], ["money", "Adjustments"], ["evidence", "Evidence"], ["agent", "Agent"], ["case", "Case"]].map(([k, l]) => (
            <button key={k} className={kind === k ? "is-on" : ""} onClick={() => setKind(k)}>{l}</button>
          ))}
        </div>
      </div>
      <p className="muted">Append-only. Entries are never edited or deleted.</p>
      <ol className="timeline">
        {shown.map((e) => (
          <li key={e.id} className={`tl tl-${GROUP[e.type] || "case"}`}>
            <span className="tl-when">{when(e.created_at)}</span>
            <div>
              <p className="tl-summary">{e.summary}</p>
              <span className="muted small">{e.actor}</span>
              {e.data?.note && <p className="note">“{e.data.note}”</p>}
              {e.data?.before && e.data?.after && e.data.before.explanation !== e.data.after.explanation && (
                <details><summary>Wording change</summary><p className="small"><s>{e.data.before.explanation}</s></p><p className="small">{e.data.after.explanation}</p></details>
              )}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
