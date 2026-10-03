import { useCallback, useEffect, useState } from "react";
import { api, session, setUnauthorizedHandler } from "./api";
import CaseRail from "./components/CaseRail";
import CaseView from "./components/CaseView";
import Login from "./components/Login";
import { Empty } from "./components/ui";
import type { CaseSummary } from "./types";

function parseHash(): { caseId: string | null; tab: string } {
  const m = window.location.hash.match(/^#\/case\/([^/]+)(?:\/([a-z]+))?/);
  return { caseId: m ? decodeURIComponent(m[1]) : null, tab: m?.[2] || "investigation" };
}

export default function App() {
  const [user, setUser] = useState<string | null>(session.token ? session.user : null);
  const [route, setRoute] = useState(parseHash());
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [casesError, setCasesError] = useState<unknown>(null);
  const [health, setHealth] = useState<any>(null);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    api("/api/health").then(setHealth).catch(() => setHealth(null));
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const loadCases = useCallback(async () => {
    try {
      setCasesError(null);
      setCases(await api<CaseSummary[]>("/api/cases"));
    } catch (e) {
      setCasesError(e);
    }
  }, []);

  useEffect(() => { if (user) loadCases(); }, [user, loadCases]);

  const go = (caseId: string | null, tab = "investigation") => {
    window.location.hash = caseId ? `#/case/${encodeURIComponent(caseId)}/${tab}` : "#/";
  };

  if (!user) return <Login onSignedIn={(u) => setUser(u)} health={health} />;

  return (
    <div className="shell">
      <CaseRail cases={cases} error={casesError} selected={route.caseId} onSelect={(id) => go(id)}
        onReload={loadCases} onCreated={(id) => { loadCases(); go(id, "evidence"); }}
        user={user} health={health} onSignOut={() => { session.clear(); setUser(null); }} />
      <main className="workspace" id="main">
        {route.caseId ? (
          <CaseView key={route.caseId} caseId={route.caseId} tab={route.tab} onTab={(t) => go(route.caseId, t)}
            onChanged={loadCases} />
        ) : (
          <div className="welcome">
            <Empty title="Choose a dispute to investigate">
              <p>Each case holds the disputed invoice, the contract's pricing rules, usage, payment history and the
                customer's complaint. Run the analysis to recalculate the invoice and get cited findings to review.</p>
              <p>Three sample disputes are ready in the list. Start with <em>API overage and missing loyalty
                discount</em> for clear calculation errors, or <em>Maintenance-window usage</em> for contract
                questions with no single right answer.</p>
            </Empty>
          </div>
        )}
      </main>
    </div>
  );
}
