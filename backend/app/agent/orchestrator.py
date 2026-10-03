"""The investigation agent: a fixed sequence of tool steps with recorded outcomes.

collect_evidence -> check_missing_evidence -> recalculate_invoice -> ai_analysis
-> ground_and_validate -> price_resolution_options -> save_analysis

Each step is timed, logged as structured JSON and stored in ``agent_steps`` so the UI
can show exactly what happened. A failing step degrades the run instead of ending it:
a broken usage export means usage-priced lines can't be verified; a model outage
means a deterministic fallback analysis. The run then finishes as
``completed_with_warnings`` rather than ``failed``.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..engine.calculator import ENGINE_VERSION, CalculationInputError, calculate, input_hash
from ..logging_setup import agent_log
from ..models import (AgentRun, AgentStep, Analysis, CalculationRecord, Case, Finding, ResolutionOption, now)
from ..services.adjustments import credited_by_discrepancy
from ..services.cases import DomainError, active_evidence, evidence_snapshot, record
from .fallback import build_fallback
from .grounding import allowed_numbers, build_reference_index, dumps, ground
from .llm import BaseClient, LLMError, get_client
from .prompts import PROMPT_VERSION, REPAIR_PROMPT, SYSTEM_PROMPT, build_context, build_messages, parse_model_json

SIMULATABLE = ("usage_lookup", "payments_lookup", "calculator", "llm")
ACTIVE_RUN_STATES = ("queued", "running")


class ToolFailure(Exception):
    pass


def start_run(db: Session, case_id: str, actor: str, simulate: list[str] | None = None) -> AgentRun:
    case = db.get(Case, case_id)
    if not case:
        raise DomainError("Case not found", 404, "not_found")
    simulate = [s for s in (simulate or []) if s]
    unknown = [s for s in simulate if s not in SIMULATABLE]
    if unknown:
        raise DomainError("Unknown failure simulation: " + ", ".join(unknown), 422, "validation_error")
    if simulate and not settings.allow_failure_simulation:
        raise DomainError("Failure simulation is disabled on this deployment", 403, "forbidden")
    if not active_evidence(db, case_id) and not case.dispute_description:
        raise DomainError("Add evidence before running the analysis", 422, "no_evidence")
    stale_cutoff = now() - timedelta(minutes=10)
    busy = db.scalars(select(AgentRun).where(AgentRun.case_id == case_id,
                                             AgentRun.status.in_(ACTIVE_RUN_STATES),
                                             AgentRun.created_at > stale_cutoff)).first()
    if busy:
        raise DomainError(f"An analysis is already running for this case ({busy.id})", 409, "run_in_progress")
    hour_ago = now() - timedelta(hours=1)
    recent = db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.created_at > hour_ago)) or 0
    if recent >= settings.max_runs_per_hour:
        raise DomainError("Analysis limit reached for this hour. Try again later.", 429, "rate_limited")
    _, fp = evidence_snapshot(db, case_id)
    run = AgentRun(case_id=case_id, status="queued", simulate_failures=simulate, triggered_by=actor,
                   evidence_fingerprint=fp, provider=settings.llm_provider, model=settings.resolved_model)
    db.add(run)
    db.flush()
    record(db, case_id, actor, "analysis_started", f"Analysis {run.id} started"
           + (f" (simulating failure: {', '.join(simulate)})" if simulate else ""), {"run_id": run.id})
    return run


class _Runner:
    def __init__(self, db: Session, run: AgentRun, client_factory: Callable[[], BaseClient]):
        self.db = db
        self.run = run
        self.case = db.get(Case, run.case_id)
        self.client_factory = client_factory
        self.seq = 0
        self.degraded = False
        self.sim = set(run.simulate_failures or [])

    @contextmanager
    def step(self, name: str):
        self.seq += 1
        started = time.monotonic()
        holder = {"status": "ok", "summary": "", "detail": {}}
        error = None
        try:
            yield holder
        except Exception as exc:  # recorded, then re-raised for the caller to decide
            holder["status"] = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ms = int((time.monotonic() - started) * 1000)
            if holder["status"] != "ok":
                self.degraded = True
            self.db.add(AgentStep(run_id=self.run.id, seq=self.seq, name=name, status=holder["status"],
                                  summary=holder["summary"] or (error or ""), detail=holder["detail"],
                                  error=error or holder.get("error"), duration_ms=ms))
            self.db.commit()
            agent_log.info("agent_step", extra={"run_id": self.run.id, "case_id": self.case.id, "step": name,
                                                "status": holder["status"], "duration_ms": ms,
                                                "error": error or holder.get("error")})

    # ------------------------------------------------------------------ steps
    def collect(self) -> dict:
        bundle = {"invoice": None, "contract": None, "usage": [], "payments": [], "payments_available": False,
                  "notes": [], "dispute_text": self.case.dispute_description or "", "tool_failures": [],
                  "evidence_ids": {}, "warnings": []}
        with self.step("collect_evidence") as s:
            docs = active_evidence(self.db, self.case.id)
            for ev in docs:
                bundle["evidence_ids"].setdefault(ev.kind, []).append(ev.id)
                bundle["warnings"] += [f"{ev.filename}: {w}" for w in ev.warnings]
            for kind in ("invoice", "contract"):
                latest = [ev for ev in docs if ev.kind == kind]
                if latest:
                    bundle[kind] = latest[-1].parsed
            usage_docs = [ev for ev in docs if ev.kind == "usage"]
            if usage_docs:
                try:
                    if "usage_lookup" in self.sim:
                        raise ToolFailure("usage store timed out (simulated)")
                    for ev in usage_docs:
                        bundle["usage"].extend(ev.parsed["events"])
                except ToolFailure as exc:
                    bundle["tool_failures"].append({"tool": "usage_lookup", "error": str(exc)})
                    s["status"] = "partial"
                    s["error"] = str(exc)
            pay_docs = [ev for ev in docs if ev.kind == "payments"]
            if pay_docs:
                try:
                    if "payments_lookup" in self.sim:
                        raise ToolFailure("payment ledger unavailable (simulated)")
                    seen = set()
                    for ev in pay_docs:
                        for p in ev.parsed["entries"]:
                            if p["id"] in seen:
                                bundle["warnings"].append(f"Payment entry {p['id']} appears in more than one file; "
                                                          "counted once.")
                                continue
                            seen.add(p["id"])
                            bundle["payments"].append(p)
                    bundle["payments_available"] = True
                except ToolFailure as exc:
                    bundle["tool_failures"].append({"tool": "payments_lookup", "error": str(exc)})
                    s["status"] = "partial"
                    s["error"] = str(exc)
            for ev in docs:
                if ev.kind == "dispute":
                    bundle["dispute_text"] = (bundle["dispute_text"] + "\n\n" + ev.parsed["text"]).strip()
                if ev.kind == "note":
                    bundle["notes"].append({"evidence_id": ev.id, "filename": ev.filename, "text": ev.parsed["text"]})
            if bundle["warnings"] and s["status"] == "ok":
                s["status"] = "partial"
            counts = {k: len(v) for k, v in bundle["evidence_ids"].items()}
            s["summary"] = (f"Loaded {len(docs)} document(s): "
                            + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                            + (f"; {len(bundle['usage'])} usage events" if bundle["usage"] else "")
                            + (f". Tool failures: {', '.join(f['tool'] for f in bundle['tool_failures'])}"
                               if bundle["tool_failures"] else ""))
            s["detail"] = {"documents": counts, "usage_events": len(bundle["usage"]),
                           "payment_entries": len(bundle["payments"]), "warnings": bundle["warnings"][:20],
                           "tool_failures": bundle["tool_failures"]}
        return bundle

    def check_missing(self, bundle: dict) -> list[dict]:
        with self.step("check_missing_evidence") as s:
            missing = []
            failed = {f["tool"] for f in bundle["tool_failures"]}
            if not bundle["invoice"]:
                missing.append({"evidence_kind": "invoice", "blocking": True,
                                "reason": "The disputed invoice with its line items is needed to recalculate anything."})
            if not bundle["contract"]:
                missing.append({"evidence_kind": "contract", "blocking": True,
                                "reason": "Pricing or contract rules are needed to know what should have been billed."})
            usage_rules = [r for r in (bundle["contract"] or {}).get("rules", [])
                           if r["type"] in ("per_unit", "tiered", "included_quota")]
            if usage_rules and not bundle["usage"]:
                why = ("could not be loaded this run (usage store failure)" if "usage_lookup" in failed
                       else "are not on file")
                missing.append({"evidence_kind": "usage", "blocking": True,
                                "reason": f"Usage events {why}; charges for "
                                          f"{', '.join(r['sku'] for r in usage_rules)} cannot be verified."})
            if not bundle["payments_available"]:
                why = ("could not be loaded this run (payment ledger failure)" if "payments_lookup" in failed
                       else "is not on file")
                missing.append({"evidence_kind": "payments", "blocking": False,
                                "reason": f"Payment and credit history {why}, so credits issued outside this case "
                                          "cannot be ruled out. Approving a credit now risks duplicating one."})
            if not bundle["dispute_text"].strip():
                missing.append({"evidence_kind": "dispute", "blocking": False,
                                "reason": "The customer's dispute description is missing."})
            s["summary"] = (f"{len(missing)} gap(s): " + ", ".join(m["evidence_kind"] for m in missing)
                            if missing else "All core evidence types present")
            s["detail"] = {"missing": missing}
            if any(m["blocking"] for m in missing):
                s["status"] = "partial"
        return missing

    def recalc(self, bundle: dict, missing: list[dict]) -> tuple[dict | None, CalculationRecord | None]:
        with self.step("recalculate_invoice") as s:
            if not bundle["invoice"] or not bundle["contract"]:
                s["status"] = "skipped"
                s["summary"] = "Skipped: invoice or contract missing"
                return None, None
            try:
                if "calculator" in self.sim:
                    raise ToolFailure("calculation worker crashed (simulated)")
                usage = bundle["usage"]
                payments = bundle["payments"] if bundle["payments_available"] else []
                calc = calculate(bundle["invoice"], bundle["contract"], usage, payments)
            except (ToolFailure, CalculationInputError, ValueError, KeyError) as exc:
                s["status"] = "failed"
                s["summary"] = "Recalculation failed; no amounts can be confirmed this run"
                s["error"] = str(exc)
                bundle["tool_failures"].append({"tool": "calculator", "error": str(exc)})
                missing.append({"evidence_kind": "invoice", "blocking": True,
                                "reason": f"Recalculation failed ({exc}). Re-run the analysis; if it fails again, "
                                          "check the invoice and contract files."})
                return None, None
            if not bundle["usage"] and any(r["type"] in ("per_unit", "tiered", "included_quota")
                                           for r in bundle["contract"]["rules"]):
                s["status"] = "partial"
                calc["warnings"].append("Usage events were unavailable; usage-priced lines were recalculated as zero "
                                        "usage and must not be relied on.")
            rec = CalculationRecord(case_id=self.case.id, run_id=self.run.id, engine_version=ENGINE_VERSION,
                                    input_hash=input_hash(bundle["invoice"], bundle["contract"], bundle["usage"],
                                                          bundle["payments"]), result=calc)
            self.db.add(rec)
            self.db.flush()
            t = calc["totals"]
            s["summary"] = (f"{len(calc['discrepancies'])} discrepancy(ies), {len(calc['ambiguities'])} open "
                            f"question(s), {len(calc['scenarios'])} scenario(s); confirmed net difference "
                            f"{calc['currency']} {t['confirmed_net_difference']}")
            s["detail"] = {"calculation_id": rec.id, "engine_version": ENGINE_VERSION, "totals": t}
            return calc, rec

    def analyse(self, ctx: dict, calc: dict | None) -> tuple[dict, str, str | None]:
        output, source, raw = None, "llm", None
        with self.step("ai_analysis") as s:
            try:
                if "llm" in self.sim:
                    raise LLMError("model provider unavailable (simulated)")
                client = self.client_factory()
                messages = build_messages(ctx)
                resp = client.complete(SYSTEM_PROMPT, messages)
                raw = resp.text
                tokens = {"input": resp.input_tokens, "output": resp.output_tokens}
                try:
                    output = parse_model_json(resp.text)
                except (ValueError, TypeError) as exc:
                    agent_log.warning("llm_invalid_json", extra={"run_id": self.run.id, "error": str(exc)})
                    retry = client.complete(SYSTEM_PROMPT, messages + [
                        {"role": "assistant", "content": resp.text[:4000]},
                        {"role": "user", "content": REPAIR_PROMPT.format(error=exc)}])
                    raw = retry.text
                    tokens = {"input": tokens["input"] + retry.input_tokens,
                              "output": tokens["output"] + retry.output_tokens}
                    output = parse_model_json(retry.text)
                    s["status"] = "partial"
                    s["error"] = f"First reply was invalid JSON ({exc}); repaired on retry"
                self.run.provider, self.run.model = client.provider, resp.model
                s["summary"] = (f"{client.provider}/{resp.model} returned {len(output.get('findings', []))} "
                                f"finding(s) and {len(output.get('resolution_options', []))} option(s)")
                s["detail"] = {"provider": client.provider, "model": resp.model, "tokens": tokens,
                               "latency_ms": resp.latency_ms, "attempts": resp.attempts,
                               "prompt_version": PROMPT_VERSION}
            except (LLMError, ValueError, TypeError) as exc:
                s["status"] = "failed"
                s["summary"] = "Model unavailable; using the deterministic fallback analysis"
                s["error"] = str(exc)
                output, source = None, "fallback"
        if output is None:
            with self.step("fallback_analysis") as s:
                output = build_fallback(calc, ctx)
                s["summary"] = (f"Deterministic analysis produced {len(output['findings'])} finding(s) and "
                                f"{len(output['resolution_options'])} option(s)")
        return output, source, raw

    # ------------------------------------------------------------------ main
    def execute(self) -> None:
        self.run.status = "running"
        self.db.commit()
        bundle = self.collect()
        missing = self.check_missing(bundle)
        calc, calc_rec = self.recalc(bundle, missing)
        index = build_reference_index(bundle, calc)
        ctx = build_context(self.case, bundle, calc, missing, sorted(index))
        output, source, raw = self.analyse(ctx, calc)

        with self.step("ground_and_validate") as s:
            per, total = credited_by_discrepancy(self.db, self.case.id)
            allowed = allowed_numbers(calc or {}, bundle["invoice"] or {}, bundle["contract"] or {},
                                      bundle["payments"], [n["text"] for n in bundle["notes"]],
                                      bundle["dispute_text"])
            g = ground(output, index, calc, allowed, source, per, total, missing)
            for f in g["findings"]:
                f["source"] = source
            for o in g["resolution_options"]:
                o["source"] = source
            if not g["findings"] or not g["resolution_options"]:
                fb = ground(build_fallback(calc, ctx), index, calc, allowed, "fallback", per, total, missing)
                if not g["findings"]:
                    agent_log.warning("no_valid_findings", extra={"run_id": self.run.id})
                    g["findings"] = [{**f, "source": "fallback"} for f in fb["findings"]]
                    g["report"]["notes"].append("The model returned no usable findings; deterministic findings shown.")
                if not g["resolution_options"]:
                    g["resolution_options"] = [{**o, "source": "fallback"} for o in fb["resolution_options"]]
                    g["report"]["notes"].append("The model proposed no usable options; deterministic options shown.")
                if source == "llm":
                    source = "hybrid"
            if not g["case_summary"]:
                g["case_summary"] = build_fallback(calc, ctx)["case_summary"]
            r = g["report"]
            if r["citations_invalid"] or r["recategorized"] or r["ungrounded"] or r["unverified_numbers"]:
                s["status"] = "partial"
            s["summary"] = (f"{r['citations_checked']} citation(s) checked, {r['citations_invalid']} removed, "
                            f"{r['recategorized']} recategorized, {r['ungrounded']} ungrounded, "
                            f"{r['unverified_numbers']} unverified figure(s)")
            s["detail"] = r

        with self.step("price_resolution_options") as s:
            s["summary"] = "; ".join(
                f"{o['title']}: {o['computation'].get('amount', 'n/a')}" for o in g["resolution_options"])
            s["detail"] = {"options": [{"title": o["title"], "action": o["action"],
                                        "amount": o["computation"].get("amount")} for o in g["resolution_options"]]}

        with self.step("save_analysis") as s:
            snap, fp = evidence_snapshot(self.db, self.case.id)
            analysis = Analysis(case_id=self.case.id, run_id=self.run.id,
                                calculation_id=calc_rec.id if calc_rec else None, source=source,
                                case_summary=g["case_summary"], claim_assessment=g["claim_assessment"],
                                missing_evidence=g["missing_evidence"], guardrail_report=g["report"],
                                raw_llm_output=raw, evidence_fingerprint=fp, evidence_snapshot=snap)
            self.db.add(analysis)
            self.db.flush()
            for i, f in enumerate(g["findings"]):
                flags = f["flags"]
                self.db.add(Finding(case_id=self.case.id, analysis_id=analysis.id, seq=i + 1, title=f["title"],
                                    category=f["category"], explanation=f["explanation"], confidence=f["confidence"],
                                    discrepancy_ids=f["discrepancy_ids"], citations=f["citations"],
                                    invalid_citations=f["invalid_citations"],
                                    unverified_numbers=f["unverified_numbers"],
                                    flags=flags + ([] if f["grounded"] else ["ungrounded"]), source=f["source"]))
            for i, o in enumerate(g["resolution_options"]):
                self.db.add(ResolutionOption(case_id=self.case.id, analysis_id=analysis.id, seq=i + 1,
                                             title=o["title"], rationale=o["rationale"], action=o["action"],
                                             discrepancy_ids=o["discrepancy_ids"],
                                             ambiguity_choices=o["ambiguity_choices"], citations=o["citations"],
                                             computation={**o["computation"], "flags": o["flags"]}, source=o["source"]))
            if self.case.status in ("draft", "reopened", "awaiting_info"):
                old = self.case.status
                self.case.status = "in_review"
                record(self.db, self.case.id, "system", "status_changed", f"Status {old} → in_review",
                       {"from": old, "to": "in_review"})
            if fp != self.run.evidence_fingerprint:
                s["status"] = "partial"
                s["error"] = "Evidence changed while the analysis was running; this analysis is already stale"
            s["summary"] = f"Saved analysis {analysis.id} ({source})"
            s["detail"] = {"analysis_id": analysis.id}

        self.run.status = "completed_with_warnings" if self.degraded else "completed"
        self.run.finished_at = datetime.now(timezone.utc)
        record(self.db, self.case.id, "system", "analysis_completed",
               f"Analysis {analysis.id} completed ({self.run.status}, {source})",
               {"run_id": self.run.id, "analysis_id": analysis.id, "status": self.run.status, "source": source})
        self.db.commit()


def execute_run(run_id: str, client_factory: Callable[[], BaseClient] | None = None) -> None:
    client_factory = client_factory or (lambda: get_client())
    db = SessionLocal()
    try:
        run = db.get(AgentRun, run_id)
        if not run or run.status not in ACTIVE_RUN_STATES:
            return
        agent_log.info("agent_run_start", extra={"run_id": run_id, "case_id": run.case_id})
        started = time.monotonic()
        try:
            _Runner(db, run, client_factory).execute()
        except Exception as exc:  # noqa: BLE001 - last line of defence; the run must not hang
            db.rollback()
            run = db.get(AgentRun, run_id)
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(timezone.utc)
            record(db, run.case_id, "system", "analysis_failed", f"Analysis {run_id} failed: {exc}", {"run_id": run_id})
            db.commit()
            agent_log.exception("agent_run_failed", extra={"run_id": run_id})
        agent_log.info("agent_run_end", extra={"run_id": run_id, "status": run.status,
                                               "duration_ms": int((time.monotonic() - started) * 1000)})
    finally:
        db.close()


def recover_interrupted_runs() -> int:
    db = SessionLocal()
    try:
        runs = list(db.scalars(select(AgentRun).where(AgentRun.status.in_(ACTIVE_RUN_STATES))))
        for run in runs:
            run.status = "failed"
            run.error = "Interrupted by a server restart; run the analysis again."
            run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return len(runs)
    finally:
        db.close()
