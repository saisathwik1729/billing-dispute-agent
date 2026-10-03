export class ApiError extends Error {
  status: number;
  code: string;
  details: string[];
  constructor(status: number, code: string, message: string, details: string[] = []) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const TOKEN_KEY = "dispute-desk-token";
const USER_KEY = "dispute-desk-user";

export const session = {
  get token(): string | null {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  },
  get user(): string | null {
    try { return localStorage.getItem(USER_KEY); } catch { return null; }
  },
  save(token: string, user: string) {
    try { localStorage.setItem(TOKEN_KEY, token); localStorage.setItem(USER_KEY, user); } catch { /* private mode */ }
  },
  clear() {
    try { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); } catch { /* ignore */ }
  },
};

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn; }

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; headers?: Record<string, string>; text?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers || {}) };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (session.token) headers["Authorization"] = `Bearer ${session.token}`;
  let res: Response;
  try {
    res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  } catch {
    throw new ApiError(0, "network", "The server could not be reached. Check your connection and try again.");
  }
  if (res.status === 401 && !path.endsWith("/auth/login")) {
    session.clear();
    onUnauthorized();
  }
  if (!res.ok) {
    let payload: any = null;
    try { payload = await res.json(); } catch { /* not json */ }
    const e = payload?.error || {};
    throw new ApiError(res.status, e.code || "error", e.message || `Request failed (HTTP ${res.status})`, e.details || []);
  }
  return (opts.text ? res.text() : res.json()) as Promise<T>;
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
