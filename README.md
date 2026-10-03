# Billing Dispute Investigation and Resolution Agent

A reviewer's workbench for disputed invoices. It recalculates the invoice from the contract, usage and payment
history with deterministic code, then uses an LLM agent to explain the differences, separate arithmetic errors
from contract-interpretation questions, ask for missing evidence, and propose resolutions. Each conclusion cites
the invoice line, usage event or contract rule behind it. A human reviewer accepts, edits or rejects every finding
and approves any (mock) credit.

- **Live app:** `<add your Render URL here>`
- **Reviewer sign-in:** see the submission remarks (credentials are set by environment variable, never committed)
- **Three sample disputes** are pre-loaded, so the app can be evaluated without preparing data.

---

## Contents

1. [Five-minute walkthrough for reviewers](#five-minute-walkthrough-for-reviewers)
2. [Architecture](#architecture)
3. [How calculation is kept separate from AI interpretation](#how-calculation-is-kept-separate-from-ai-interpretation)
4. [The agent](#the-agent)
5. [Requirement coverage](#requirement-coverage)
6. [Local setup](#local-setup)
7. [Tests](#tests)
8. [Deployment](#deployment)
9. [Configuration](#configuration)
10. [Logs and observability](#logs-and-observability)
11. [Evidence formats](#evidence-formats)
12. [Completed scope, excluded scope and limitations](#completed-scope-excluded-scope-and-limitations)

---

## Five-minute walkthrough for reviewers

1. **Sign in** with the credentials from the remarks.
2. **Acme Logistics — "API overage and missing loyalty discount".** Click *Run analysis* and watch the seven agent
   steps complete.
   - *Investigation:* the AI summary (blue text is AI-written) and findings, each citing records such as `L2`, `R2`,
     `EV-1012`. Hover a citation to see the record.
   - *Invoice check:* the invoice marked up in red pen. 283,000 billed calls become 260,000 (a duplicated meter
     event and a July event were billed), and the missing 10% discount is added: **2,994.00 → 2,582.00**.
   - *Resolution:* try *Approve credit*. It is blocked until the supporting findings are accepted. Accept them on
     *Investigation*, then approve **412.00**. Approve again: the app refuses ("Nothing remains to adjust").
3. **Globex Health — "Mid-month seats billed for the full month".** Run the analysis. The seat proration error
   (200.00) is real, but an **80.00 credit already sits in the payment history**, so the credit option offers
   **120.00**, not 200.00. The engine also finds the invoice *omitted* a 200.00 minimum-commitment true-up, so the
   net-settlement option is an **80.00 charge**. The customer is right about proration and still not owed money.
4. **Initech Systems — "Maintenance-window usage and unclear tier pricing".** Run the analysis. There is no
   confirmed error. Three contract and data questions (tier mode, whether emergency maintenance counts as
   maintenance, and two suspicious same-second events) produce eight priced scenarios, from **−100.00 to +390.00**.
   On *Invoice check*, switch the readings to see the invoice under each one. The AI asks for the missing payment
   history.
   - On *Evidence*, add the sample follow-up "Account manager's email about tier pricing". The case is marked stale:
     findings that depend on the changed evidence are flagged, and approvals pause. Re-run, and the AI can now weigh
     the email (cited by its evidence id) when discussing the tier reading.
   - Review every finding, then resolve the case. Add the second follow-up (payment history): the case **reopens
     automatically** and is marked stale.
5. **Failure handling.** On any case, open *Test failure handling*, tick "Model provider" and "Payment ledger", and
   run. The run completes *with warnings*: a deterministic fallback analysis replaces the model, the payment gap is
   listed as missing evidence, and approving a credit then requires an explicit "I have checked for earlier
   credits" acknowledgement. *Agent trace* shows every step's status, timing and error.
6. **History** shows the append-only record of every evidence change, run, review decision and approval.

---

## Architecture

```
 Browser (React + TypeScript, Vite)
        │  JSON over HTTPS, Bearer token
        ▼
 FastAPI app ───────────────────────────────────────────────────────────────┐
  ├─ api/routes.py           REST endpoints, validation, serialization      │
  ├─ services/               case lifecycle, evidence, staleness, approvals │
  ├─ evidence/parser.py      validates uploads into canonical JSON          │
  ├─ engine/                 DETERMINISTIC: Decimal recalculation, scenarios│
  │    calculator.py           and resolution amounts (no AI involved)      │
  │    resolution.py                                                        │
  ├─ agent/                  the investigation agent                        │
  │    orchestrator.py         7 traced tool steps, partial-failure handling│
  │    prompts.py              system prompt + context builder              │
  │    llm.py                  Anthropic / OpenAI-compatible / mock clients │
  │    grounding.py            citation, category and number guardrails    │
  │    fallback.py             deterministic analysis when the model fails  │
  └─ static frontend         built React app served from the same origin   │
        │                                                                    │
        ▼                                                                    │
 PostgreSQL (hosted) or SQLite (local) via SQLAlchemy ◄─────────────────────┘
```

One container serves the API and the built frontend, so there is one URL, no CORS, and one thing to deploy.
Analysis runs execute as background tasks; the UI polls the run and shows step-by-step progress.

**Data model.** `cases`, `evidence` (immutable, versioned by supersession or withdrawal), `agent_runs` and
`agent_steps` (the trace), `calculations` (engine output), `analyses`, `findings` and `resolution_options`
(AI interpretation), `adjustments` and `adjustment_items` (approved mock credits), `info_requests`, and
`case_events` (append-only history).

---

## How calculation is kept separate from AI interpretation

This is the core design decision.

**1. All money is computed by code, with `Decimal`.** JSON is parsed with `parse_float=Decimal`, values are stored as
strings, and lines are rounded half-up to cents. The engine handles flat fees, per-unit, graduated and volume tiers,
included quotas, peak/sum/snapshot measures, daily proration, percentage discounts, minimum-commitment true-ups,
contract usage exclusions, duplicate and out-of-period events, invoice arithmetic and invoice-total mismatches, and
prior credits from payment history.

**2. Ambiguity is priced, never decided.** When the evidence can be read more than one way, the engine records an
*open question* with a fixed set of readings:

| Question type | Example | Readings |
|---|---|---|
| `tier_mode` | Tiers listed without saying graduated or volume | graduated / volume |
| `disputed_usage` | Usage tagged `emergency_maintenance` under a vague "maintenance windows" clause | billable / not billable |
| `possible_duplicate` | Two events, same SKU, quantity and timestamp, different ids | count all / exclude repeats |

It prices the full invoice under every combination of readings (a *scenario*; up to 64, then one-at-a-time
variations). For each line:

- if every scenario agrees, the difference is a **calculation error**;
- otherwise the part with the same sign under *every* reading is still a **calculation error** (owed whichever
  reading wins), and the remainder is **interpretation-dependent**, with one outcome per reading.

Clauses the engine cannot price (free-text `clause` rules) are passed to the AI as interpretation-only material.

**3. The AI references; code computes.** The model proposes resolution options as *discrepancy ids plus chosen
readings*. `engine/resolution.py` turns that into an amount, deducting credits already in the payment history and
adjustments already approved. Any amount field the model invents is ignored.

**4. Guardrails check the model's prose** (`agent/grounding.py`):

- Citations are resolved against real records. Unknown ids are removed and reported. A finding with no valid
  citation is marked *ungrounded* and the API refuses to accept it.
- Categories are checked against the engine. A "calculation error" must rest on a discrepancy the engine classed
  as one; otherwise it is recategorized, with a visible note. The reverse is checked too.
- Every money-looking figure in the AI's text is checked against figures that exist in the evidence or the
  calculation. Unmatched figures are flagged on the finding.

**5. They are stored separately.** `calculations` holds engine output (with engine version and an input
fingerprint). `analyses` and `findings` hold interpretation. The UI colours AI-written text blue so reviewers always
know which is which.

Hand-checked figures for the samples (also asserted in the tests):

| Case | Check |
|---|---|
| Acme | API: 260,000 billable = 100,000 × 0.0100 + 160,000 × 0.0080 = 2,280.00 vs 2,464.00 billed (+184.00). Discount 10% × 2,280.00 = 228.00 missing (+228.00). Storage peak 620 − 500 = 120 × 0.25 = 30.00 (correct). **Total 412.00.** |
| Globex | Seats: (20 × 15 days + 30 × 15 days) / 30 × 40.00 = 1,000.00 vs 1,200.00 (+200.00). Charges 1,300.00 < 1,500.00 minimum, so a 200.00 true-up was omitted (−200.00). Prior credit 80.00 on L1 leaves 120.00 creditable. |
| Initech | Billable 170,000 after the definite scheduled-maintenance exclusion. Graduated 2,800.00 / volume 2,550.00; with emergency usage and the repeat excluded, down to 2,560.00 / 2,310.00. Against 2,700.00 billed: −100.00 to +390.00. |

---

## The agent

`agent/orchestrator.py` runs a fixed, auditable pipeline. Each step is a tool call whose status (`ok`, `partial`,
`failed`, `skipped`), duration, summary and details are stored in `agent_steps` and shown on *Agent trace*.

| # | Step | What it does | If it fails |
|---|---|---|---|
| 1 | `collect_evidence` | Loads active evidence; merges usage and payment files | A failing usage store or payment ledger is recorded; the run continues without it |
| 2 | `check_missing_evidence` | Deterministic gap check (invoice, contract, usage for usage-priced SKUs, payments, dispute) | — |
| 3 | `recalculate_invoice` | Runs the engine and stores a `CalculationRecord` | Run continues; no amounts are offered, and the AI is told the calculation is unavailable |
| 4 | `ai_analysis` | One structured LLM call (temperature 0, JSON); one repair attempt if the JSON is invalid; retries with backoff on 429/5xx/timeouts | Deterministic `fallback_analysis` step produces findings and options in the same shape |
| 5 | `ground_and_validate` | Citation, category and number guardrails; fills gaps from the fallback | — |
| 6 | `price_resolution_options` | Engine prices every option | — |
| 7 | `save_analysis` | Persists analysis, findings, options; detects evidence changing mid-run | Run marked `failed`, error recorded |

Runs end as `completed`, `completed_with_warnings` (anything degraded) or `failed`. Only one run per case can be
active, runs are rate-limited per hour, and runs interrupted by a restart are marked failed on startup.

**Why a fixed pipeline rather than letting the model choose tools.** Every run must use the same evidence, the same
calculation and the same checks, so results are reproducible and auditable. The model's job is judgement and
explanation, which is where it adds value. The prompt (`agent/prompts.py`) treats the customer's text as data, not
instructions, to resist prompt injection.

**Human review gates.**

- Findings start as *proposed*. The reviewer accepts, edits (the original wording is preserved) or rejects them,
  with a note.
- A credit can only be approved once every discrepancy it covers is backed by an accepted finding.
- Approval is blocked if evidence changed since the analysis.
- If payment history was unavailable, approval needs an explicit acknowledgement.
- The reviewer can approve less than the calculated amount, never more.
- Resolving needs a current analysis, every finding reviewed, and no open information requests.

---

## Requirement coverage

| Requirement | Where |
|---|---|
| Evidence inputs: invoice lines, contract rules, usage, payments/adjustments, dispute | `evidence/parser.py`; *Evidence* tab (upload a file or paste; JSON or CSV) |
| Deterministic money | `engine/calculator.py`, `engine/resolution.py`, `engine/money.py` |
| Identify relevant evidence | Step 1, plus the citations on each finding |
| Explain possible causes | Engine `causes` (e.g. "difference matches 15,000 from repeated event EV-1012…") plus AI explanation |
| Calculation error vs. unclear contract | Scenario engine plus the category guardrail ([see above](#how-calculation-is-kept-separate-from-ai-interpretation)) |
| Ask for missing evidence | Step 2 plus AI suggestions; *Request it* creates a tracked request, auto-fulfilled when matching evidence arrives |
| Evidence-grounded case summary | *Investigation* summary; guardrail line shows citations checked and removed |
| Resolution options | *Resolution* tab; engine-priced, with a per-discrepancy equation |
| Cite invoice, usage event or rule | Citation chips with hover detail; engine adds line and rule refs for each referenced discrepancy |
| Accept / edit / reject findings | `PATCH /api/findings/{id}` |
| Approve mock credit or adjustment | `POST /api/cases/{id}/adjustments` (credit or debit notes, optional lower amount) |
| Request additional information | `POST /api/cases/{id}/info-requests` |
| Compare original and recalculated invoice | *Invoice check*, under any combination of readings |
| Preserve dispute and decision history | `case_events` (append-only), *History* tab |
| Prevent duplicate credits | Idempotency key, remaining-balance accounting (including ledger credits), DB unique constraint, per-case lock / `SELECT … FOR UPDATE` |
| Handle partial tool failure | Orchestrator degradation per step; *Test failure handling* lets reviewers trigger it |
| Record calculations separately | `calculations` table vs `analyses` and `findings` |
| Case reopening on new evidence | `services/cases.py::_on_evidence_changed` |
| Show stale conclusions | Analysis fingerprint; per-finding `stale_reasons` by evidence type; stale banner; approvals paused |
| Loading / empty / validation / success / failure states | Skeletons, empty states with next actions, field-level and server validation details, toasts, error callouts with retry |
| Structured logs, focused tests | JSON logs with request and run ids; 33 tests |

---

## Local setup

Requirements: Python 3.12, Node 20+.

```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp ../.env.example ../.env                               # then edit; or export variables
export LLM_PROVIDER=mock REVIEWER_ACCOUNTS=reviewer:demo  # mock = no API key needed
uvicorn app.main:app --reload --port 8000

# frontend (second terminal) — dev server proxies /api to :8000
cd frontend
npm install
npm run dev                                              # http://localhost:5173
```

For a production-like run, `npm run build` in `frontend/` and start uvicorn; the API serves `frontend/dist`
at http://localhost:8000.

With Docker:

```bash
docker build -t dispute-desk .
docker run -p 8000:8000 -e LLM_PROVIDER=gemini -e LLM_API_KEY=... \
  -e REVIEWER_ACCOUNTS=reviewer:demo -e AUTH_SECRET=$(openssl rand -hex 32) dispute-desk
```

---

## Tests

```bash
cd backend
python -m pytest -q                                   # SQLite
TEST_DATABASE_URL=postgresql://user:pw@localhost/db python -m pytest -q   # same suite on Postgres
```

33 tests, all passing on SQLite and PostgreSQL 16. CI (`.github/workflows/ci.yml`) runs both, plus the frontend
type-check and build.

| File | Covers |
|---|---|
| `test_engine.py` | Tier maths; half-up rounding without floats; all three samples' figures; confirmed-vs-interpretation split when every reading overcharges; invoice-total mismatch; unsupported charges; determinism; snapshot peak billing |
| `test_parser.py` | JSON error positions; field-level errors; money never becomes float; bad CSV rows skipped with warnings; rule validation |
| `test_resolution_and_grounding.py` | Prior-credit and prior-adjustment deduction; conservative default reading; credit ceiling; fake citations removed; category correction; invented figures flagged; model-supplied amounts ignored |
| `test_api_workflow.py` | Auth; full workflow; idempotent replay; duplicate refusal; reopen and stale findings; stale analysis blocks approval; simulated usage, ledger, calculator and model failures; unverified-payments acknowledgement; partial approvals; invalid model JSON repaired; ungrounded findings unacceptable; edit preserves original; validation and duplicate evidence; **six concurrent approvals create exactly one adjustment**; one run at a time |

The real LLM path is exercised by the same pipeline with a scripted client (`MockClient`), including malformed and
hallucinating responses. The provider HTTP clients are thin; verify them on the deployment (see `AGENT_USAGE.md`).

---

## Deployment

The reference deployment runs entirely on free tiers, with no payment or card needed: **Render** (free web
service, Docker), **Neon** (free Postgres) and **Google Gemini** (free API tier). Render's free filesystem is wiped
on restart, so a hosted database is needed for persistence.

1. **Database.** Create a free project at neon.tech and copy the connection string
   (`postgresql://…?sslmode=require`).
2. **LLM key.** Create a free Gemini API key at aistudio.google.com ("Get API key"). No billing is needed.
3. **Repository.** Push this repository to GitHub.
4. **Render.** Go to *New → Blueprint*, select the repo (it reads `render.yaml`), and fill in the secret values:
   - `DATABASE_URL`: the Neon string
   - `LLM_API_KEY`: your Gemini key
   - `REVIEWER_ACCOUNTS`: e.g. `reviewer:<a password you choose>`

   `AUTH_SECRET` is generated for you.
5. **Verify.** When the deploy is live, open `https://<app>.onrender.com/api/health`. Check that `database` is `ok`
   and `llm.configured` is `true`. Then sign in and run the Acme sample: the summary badge should read
   *AI analysis, &lt;provider&gt; &lt;model&gt;*, not *Deterministic fallback*.

Free Render services sleep after about 15 minutes idle; the first request then takes about 30–60 seconds. Open the
app shortly before review, or use a paid instance.

---

## Configuration

All configuration is by environment variable; see `.env.example`.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL; `postgres://` and `postgresql://` are normalised to psycopg 3 |
| `LLM_PROVIDER` | `gemini` (default, free), `anthropic`, `openai` (any OpenAI-compatible endpoint) or `mock` |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` | Model, key, and (for `openai`) the endpoint |
| `REVIEWER_ACCOUNTS` | `user:password,user2:password2` |
| `AUTH_SECRET` | HMAC secret for session tokens (12-hour expiry) |
| `MAX_ANALYSIS_RUNS_PER_HOUR` | Protects the LLM budget on a public deployment |
| `ALLOW_FAILURE_SIMULATION` | Enables the *Test failure handling* controls |
| `SEED_SAMPLE_CASES` | Seeds the three samples when the database is empty |

Provider examples:

- **Google Gemini** (default, free tier): `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.8-flash`. Uses Gemini's
  OpenAI-compatible endpoint, so no extra SDK is needed.
- **Anthropic** (paid): `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5`.
- **Groq** (free tier): `LLM_PROVIDER=openai`, `LLM_BASE_URL=https://api.groq.com/openai/v1`,
  `LLM_MODEL=llama-3.3-70b-versatile`.

Check the provider's current model names. A typical analysis uses about 4,000 input tokens.

---

## Logs and observability

Every log line is one JSON object on stdout, viewable in the Render logs. Main events:

- `http_request`: method, path, status, duration_ms, request_id
- `agent_run_start` / `agent_run_end`: run_id, status, duration_ms
- `agent_step`: run_id, case_id, step, status, duration_ms, error
- `llm_call` / `llm_call_failed`: provider, model, latency_ms, tokens, attempt, retryable
- `case_event`: case_id, event_type, actor
- `adjustment_approved`: case_id, adjustment_id, amount

Errors return `{"error": {"code", "message", "details", "request_id"}}`; the `x-request-id` header ties a UI error
to its log lines. In the app, *Agent trace* shows each run's steps, guardrail report and the raw model output.

---

## Evidence formats

Upload a file or paste content on the *Evidence* tab. Sample files are in `backend/app/samples/`.

- **Invoice (JSON):** `invoice_id`, `currency`, `period {start, end}`, `lines[] {line_id, sku, description,
  quantity, unit_price, amount}`, `total`.
- **Contract (JSON):** `contract_id`, `rules[]`. Every rule has `id` and `type`; `text` holds the contract wording.
  - `flat {sku, amount}`
  - `per_unit {sku, unit_price, measure: sum|max|snapshot, proration: none|daily}`
  - `tiered {sku, tiers[{up_to|null, unit_price}], tier_mode?: graduated|volume}`
  - `included_quota {sku, included, overage_unit_price, measure}`
  - `discount_percent {sku, percent, applies_to[]}`
  - `minimum_commit {amount, sku?}`
  - `usage_exclusion {sku, exclude_tags[], disputed_tags[]}`
  - `clause {text}`
- **Usage:** JSON `{"events": [...]}` or CSV `event_id,timestamp,sku,quantity,tags`.
- **Payments and credits:** JSON `{"entries": [...]}` or CSV `id,type,date,amount,invoice_id,applies_to_lines,reason`.
  Types: `payment`, `credit`, `adjustment`, `refund`.
- **Dispute / note:** plain text.

A new invoice or contract supersedes the current one. Usage and payment files can be added alongside or replace a
specific file. Identical evidence is refused. Invalid usage or payment rows are skipped with warnings; structural
errors are rejected with field-level messages.

---

## Completed scope, excluded scope and limitations

**Completed:** everything in the brief, plus the following extras:

- multi-reading scenario pricing
- citation, category and figure guardrails
- failure simulation
- idempotent, concurrency-safe approvals
- partial approvals
- per-finding staleness
- automatic fulfilment of information requests
- a Postgres-tested CI pipeline

**Excluded, as the brief allows:**

- real payment processing and accounting integration (credits are mock credit or debit notes)
- tax calculation (tax on an invoice produces a warning)
- automatic adjustments (every adjustment needs a human)

**Known limitations:**

- One invoice per case; multi-invoice and cross-period disputes are not modelled.
- No currency conversion; a currency mismatch is flagged, not converted.
- The pricing-rule vocabulary covers common SaaS billing models. Anything else can be entered as a `clause`, which
  the AI interprets but the engine does not price.
- Authentication is simple shared reviewer accounts with signed tokens; production would use SSO and roles
  (for example, separating approvers from investigators).
- The database schema is created on startup (`create_all`); production would use Alembic migrations.
- Analysis runs are in-process background tasks; production would use a job queue. Interrupted runs are marked
  failed at startup.
- Evidence is entered as structured JSON or CSV (or pasted text). PDF invoice extraction is not included.
- The hosted demo uses Gemini's free tier, which has per-minute and per-day request limits. When a limit is hit,
  the app retries and then falls back to the deterministic analysis, recording the reason in the agent trace.
  Google may use free-tier prompts to improve its products, so only sample data should be used on the demo.
- The model is non-deterministic in wording. Its numbers, categories and citations are constrained by the
  guardrails, but its explanations can vary between runs.
