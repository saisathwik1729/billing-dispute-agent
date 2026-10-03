import { FormEvent, useState } from "react";
import { api, ApiError, session } from "../api";
import { Spinner } from "./ui";

export default function Login({ onSignedIn, health }: { onSignedIn: (u: string) => void; health: any }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) { setError("Enter your username and password."); return; }
    setBusy(true); setError(null);
    try {
      const r = await api<{ token: string; username: string }>("/api/auth/login", { method: "POST", body: { username, password } });
      session.save(r.token, r.username);
      onSignedIn(r.username);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
    } finally { setBusy(false); }
  }

  return (
    <div className="login">
      <div className="login-sheet">
        <div className="brand brand-lg"><span className="brand-mark" aria-hidden="true" />Dispute desk</div>
        <p className="login-lede">Investigate disputed invoices against the contract, usage and payment record.
          Every figure is recalculated by code; the AI explains and cites, and you decide.</p>
        <form onSubmit={submit} noValidate>
          <label className="field"><span className="field-label">Username</span>
            <input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
          <label className="field"><span className="field-label">Password</span>
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          {error && <div className="callout callout-error" role="alert">{error}</div>}
          <button className="btn btn-primary btn-block" disabled={busy}>{busy ? <><Spinner /> Signing in</> : "Sign in"}</button>
        </form>
        {health && (
          <p className="login-meta">
            Model: {health.llm.configured ? `${health.llm.provider} / ${health.llm.model}` : "not configured, fallback analysis only"}
          </p>
        )}
      </div>
    </div>
  );
}
