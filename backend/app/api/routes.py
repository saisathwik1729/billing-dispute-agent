from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..agent import orchestrator
from ..auth import check_credentials, current_user, issue_token
from ..config import settings
from ..db import get_db
from ..engine.calculator import compare
from ..engine.money import D, ZERO, m2s
from ..evidence.parser import EVIDENCE_KINDS
from ..models import (Adjustment, AgentRun, AgentStep, Analysis, CalculationRecord, Case, CaseEvent, Evidence,
                      Finding, InfoRequest, ResolutionOption)
from ..services import adjustments as adj_svc
from ..services import samples as sample_svc
from ..services.cases import (DomainError, add_evidence, create_case, finding_staleness, get_case,
                              latest_analysis, record, set_status, staleness, withdraw_evidence)

router = APIRouter(prefix="/api")
APP_VERSION = "1.0.0"


# --------------------------------------------------------------------------- schemas
class LoginIn(BaseModel):
    username: str
    password: str


class CaseIn(BaseModel):
    title: str = Field(default="", max_length=200)
    customer_name: str = Field(default="", max_length=200)
    dispute_description: str = Field(default="", max_length=20000)


class EvidenceIn(BaseModel):
    kind: str
    filename: str = Field(default="", max_length=255)
    content: str
    replaces_id: str | None = None


class WithdrawIn(BaseModel):
    reason: str = ""


class AnalyzeIn(BaseModel):
    simulate_failures: list[str] = []


class FindingReviewIn(BaseModel):
    action: str
    note: str = ""
    title: str | None = None
    explanation: str | None = None
    category: str | None = None


class InfoRequestIn(BaseModel):
    evidence_kind: str
    message: str = ""


class ApproveIn(BaseModel):
    option_id: str
    note: str = ""
    amount: str | None = None
    acknowledge_unverified_payments: bool = False


class ResolveIn(BaseModel):
    summary: str = ""


class ReasonIn(BaseModel):
    reason: str = ""


# --------------------------------------------------------------------------- serializers
def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _evidence_summary(ev: Evidence) -> str:
    p = ev.parsed
    if ev.kind == "invoice":
        return f"{p['invoice_id']}: {len(p['lines'])} line(s), period {p['period']['start']} to {p['period']['end']}"
    if ev.kind == "contract":
        return f"{p['contract_id']}: {len(p['rules'])} rule(s)"
    if ev.kind == "usage":
        return f"{len(p['events'])} event(s)"
    if ev.kind == "payments":
        return f"{len(p['entries'])} entr(y/ies)"
    t = p.get("text", "")
    return t[:140] + ("…" if len(t) > 140 else "")


def ser_evidence(ev: Evidence, full: bool = False) -> dict:
    out = {"id": ev.id, "kind": ev.kind, "filename": ev.filename, "active": ev.active,
           "superseded_by": ev.superseded_by, "withdrawn_reason": ev.withdrawn_reason, "warnings": ev.warnings,
           "added_by": ev.added_by, "created_at": _iso(ev.created_at), "summary": _evidence_summary(ev)}
    if full:
        out["parsed"] = ev.parsed
        out["raw_text"] = ev.raw_text
    return out


def ser_case(c: Case) -> dict:
    return {"id": c.id, "title": c.title, "customer_name": c.customer_name, "status": c.status,
            "dispute_description": c.dispute_description, "sample_key": c.sample_key, "created_by": c.created_by,
            "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
            "resolution_summary": c.resolution_summary}


def ser_run(r: AgentRun, steps: list[AgentStep] | None = None) -> dict:
    out = {"id": r.id, "status": r.status, "provider": r.provider, "model": r.model,
           "simulate_failures": r.simulate_failures, "error": r.error, "triggered_by": r.triggered_by,
           "created_at": _iso(r.created_at), "finished_at": _iso(r.finished_at)}
    if steps is not None:
        out["steps"] = [{"seq": s.seq, "name": s.name, "status": s.status, "summary": s.summary, "error": s.error,
                         "duration_ms": s.duration_ms, "detail": s.detail} for s in steps]
    return out


def ser_adjustment(a: Adjustment) -> dict:
    return {"id": a.id, "kind": a.kind, "amount": a.amount, "computed_amount": a.computed_amount,
            "currency": a.currency, "option_id": a.option_id, "analysis_id": a.analysis_id,
            "discrepancy_ids": a.discrepancy_ids, "note": a.note, "approved_by": a.approved_by,
            "created_at": _iso(a.created_at), "breakdown": a.breakdown}


def analysis_payload(db: Session, a: Analysis) -> dict:
    st = staleness(db, a)
    findings = list(db.scalars(select(Finding).where(Finding.analysis_id == a.id).order_by(Finding.seq)))
    options = list(db.scalars(select(ResolutionOption).where(ResolutionOption.analysis_id == a.id)
                              .order_by(ResolutionOption.seq)))
    run = db.get(AgentRun, a.run_id)
    accepted = {d for f in findings if f.status in ("accepted", "edited") for d in f.discrepancy_ids}
    opt_out = []
    for o in options:
        live = adj_svc.preview_option(db, o) if o.action not in adj_svc.NON_FINANCIAL_ACTIONS else None
        needs = [it["discrepancy_id"] for it in (live or {}).get("items", []) if D(it["net"]) != 0]
        opt_out.append({"id": o.id, "seq": o.seq, "title": o.title, "rationale": o.rationale, "action": o.action,
                        "discrepancy_ids": o.discrepancy_ids, "ambiguity_choices": o.ambiguity_choices,
                        "citations": o.citations, "proposed": o.computation, "live": live, "source": o.source,
                        "flags": (o.computation or {}).get("flags", []),
                        "unaccepted_discrepancies": [d for d in needs if d not in accepted]})
    return {
        "id": a.id, "run_id": a.run_id, "source": a.source, "created_at": _iso(a.created_at),
        "provider": run.provider if run else None, "model": run.model if run else None,
        "run_status": run.status if run else None,
        "case_summary": a.case_summary, "claim_assessment": a.claim_assessment,
        "missing_evidence": a.missing_evidence, "guardrail_report": a.guardrail_report,
        "calculation_id": a.calculation_id, "stale": st["stale"], "changed_kinds": st["changed_kinds"],
        "findings": [{
            "id": f.id, "seq": f.seq, "title": f.title, "category": f.category, "explanation": f.explanation,
            "confidence": f.confidence, "discrepancy_ids": f.discrepancy_ids, "citations": f.citations,
            "invalid_citations": f.invalid_citations, "unverified_numbers": f.unverified_numbers,
            "flags": f.flags, "source": f.source, "status": f.status, "reviewer_note": f.reviewer_note,
            "original": f.original, "reviewed_by": f.reviewed_by, "reviewed_at": _iso(f.reviewed_at),
            "stale_reasons": finding_staleness(f.citations, st["changed_kinds"]) if st["stale"] else [],
        } for f in findings],
        "options": opt_out,
    }


def _latest_calc(db: Session, case_id: str) -> tuple[Analysis | None, dict | None]:
    a = latest_analysis(db, case_id)
    if not a or not a.calculation_id:
        return a, None
    return a, db.get(CalculationRecord, a.calculation_id).result


# --------------------------------------------------------------------------- public
@router.get("/health")
def health(db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    configured = settings.llm_provider == "mock" or bool(settings.llm_api_key)
    return {"status": "ok" if db_ok else "degraded", "version": APP_VERSION, "database": "ok" if db_ok else "error",
            "llm": {"provider": settings.llm_provider, "model": settings.resolved_model, "configured": configured},
            "failure_simulation": settings.allow_failure_simulation}


@router.post("/auth/login")
def login(body: LoginIn):
    if not check_credentials(body.username, body.password):
        raise DomainError("Username or password is incorrect", 401, "invalid_credentials")
    return {"token": issue_token(body.username.strip()), "username": body.username.strip()}


# --------------------------------------------------------------------------- cases
@router.get("/cases")
def list_cases(db: Session = Depends(get_db), user: str = Depends(current_user)):
    out = []
    for c in db.scalars(select(Case).order_by(Case.updated_at.desc())):
        a = latest_analysis(db, c.id)
        info = {"has_analysis": bool(a), "stale": False, "confirmed_net_difference": None, "currency": None,
                "open_findings": 0}
        if a:
            info["stale"] = staleness(db, a)["stale"]
            info["open_findings"] = len(list(db.scalars(select(Finding.id).where(
                Finding.analysis_id == a.id, Finding.status == "proposed"))))
            if a.calculation_id:
                calc = db.get(CalculationRecord, a.calculation_id).result
                info["confirmed_net_difference"] = calc["totals"]["confirmed_net_difference"]
                info["currency"] = calc["currency"]
        evidence_count = len(list(db.scalars(select(Evidence.id).where(Evidence.case_id == c.id,
                                                                         Evidence.active.is_(True)))))
        out.append({**ser_case(c), **info, "evidence_count": evidence_count})
    return out


@router.post("/cases", status_code=201)
def new_case(body: CaseIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = create_case(db, body.title, body.customer_name, body.dispute_description, user)
    db.commit()
    return ser_case(case)


@router.get("/cases/{case_id}")
def case_detail(case_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = get_case(db, case_id)
    evidence = list(db.scalars(select(Evidence).where(Evidence.case_id == case_id).order_by(Evidence.created_at)))
    analyses = list(db.scalars(select(Analysis).where(Analysis.case_id == case_id)
                               .order_by(Analysis.created_at.desc())))
    runs = list(db.scalars(select(AgentRun).where(AgentRun.case_id == case_id)
                           .order_by(AgentRun.created_at.desc()).limit(10)))
    active_run = next((r for r in runs if r.status in orchestrator.ACTIVE_RUN_STATES), None)
    calc = None
    if analyses and analyses[0].calculation_id:
        rec = db.get(CalculationRecord, analyses[0].calculation_id)
        calc = {"id": rec.id, "engine_version": rec.engine_version, "input_hash": rec.input_hash,
                "created_at": _iso(rec.created_at), **{k: rec.result[k] for k in (
                    "currency", "invoice_id", "period", "line_comparison", "discrepancies", "ambiguities",
                    "data_quality", "unsupported_rules", "prior_credits", "totals", "warnings")}}
    adjs = list(db.scalars(select(Adjustment).where(Adjustment.case_id == case_id).order_by(Adjustment.created_at)))
    reqs = list(db.scalars(select(InfoRequest).where(InfoRequest.case_id == case_id)
                           .order_by(InfoRequest.created_at.desc())))
    sample = None
    if case.sample_key:
        try:
            sample = sample_svc.get_sample(case.sample_key)
        except DomainError:
            sample = None
    return {
        "case": ser_case(case),
        "evidence": [ser_evidence(e) for e in evidence],
        "latest_analysis": analysis_payload(db, analyses[0]) if analyses else None,
        "analyses": [{"id": a.id, "created_at": _iso(a.created_at), "source": a.source,
                      "stale": staleness(db, a)["stale"]} for a in analyses],
        "calculation": calc,
        "runs": [ser_run(r) for r in runs],
        "active_run": ser_run(active_run) if active_run else None,
        "adjustments": [ser_adjustment(a) for a in adjs],
        "info_requests": [{"id": r.id, "evidence_kind": r.evidence_kind, "message": r.message, "status": r.status,
                           "requested_by": r.requested_by, "created_at": _iso(r.created_at),
                           "fulfilled_by_evidence_id": r.fulfilled_by_evidence_id,
                           "fulfilled_at": _iso(r.fulfilled_at)} for r in reqs],
        "sample_followups": sample["followups"] if sample else [],
    }


@router.get("/cases/{case_id}/events")
def case_events(case_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    get_case(db, case_id)
    return [{"id": e.id, "actor": e.actor, "type": e.type, "summary": e.summary, "data": e.data,
             "created_at": _iso(e.created_at)}
            for e in db.scalars(select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.id.desc()))]


# --------------------------------------------------------------------------- evidence
@router.post("/cases/{case_id}/evidence", status_code=201)
def upload_evidence(case_id: str, body: EvidenceIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    if body.kind not in EVIDENCE_KINDS:
        raise DomainError("Choose an evidence type", 422, "validation_error",
                          [f"kind: must be one of {', '.join(EVIDENCE_KINDS)}"])
    ev, warnings = add_evidence(db, case_id, body.kind, body.filename, body.content, user, body.replaces_id)
    db.commit()
    return {"evidence": ser_evidence(ev), "warnings": warnings}


@router.get("/evidence/{evidence_id}")
def evidence_detail(evidence_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    ev = db.get(Evidence, evidence_id)
    if not ev:
        raise DomainError("Evidence not found", 404, "not_found")
    return ser_evidence(ev, full=True)


@router.post("/cases/{case_id}/evidence/{evidence_id}/withdraw")
def withdraw(case_id: str, evidence_id: str, body: WithdrawIn, db: Session = Depends(get_db),
             user: str = Depends(current_user)):
    ev = withdraw_evidence(db, case_id, evidence_id, body.reason, user)
    db.commit()
    return ser_evidence(ev)


# --------------------------------------------------------------------------- analysis
@router.post("/cases/{case_id}/analyze", status_code=202)
def analyze(case_id: str, body: AnalyzeIn, background: BackgroundTasks, db: Session = Depends(get_db),
            user: str = Depends(current_user)):
    run = orchestrator.start_run(db, case_id, user, body.simulate_failures)
    db.commit()
    background.add_task(orchestrator.execute_run, run.id)
    return ser_run(run)


@router.get("/runs/{run_id}")
def run_detail(run_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    run = db.get(AgentRun, run_id)
    if not run:
        raise DomainError("Run not found", 404, "not_found")
    steps = list(db.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)))
    return ser_run(run, steps)


@router.get("/analyses/{analysis_id}")
def analysis_detail(analysis_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    a = db.get(Analysis, analysis_id)
    if not a:
        raise DomainError("Analysis not found", 404, "not_found")
    return analysis_payload(db, a)


@router.get("/analyses/{analysis_id}/raw", response_class=PlainTextResponse)
def analysis_raw(analysis_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    a = db.get(Analysis, analysis_id)
    if not a:
        raise DomainError("Analysis not found", 404, "not_found")
    return a.raw_llm_output or "(no model output: deterministic fallback was used)"


@router.patch("/findings/{finding_id}")
def review_finding(finding_id: str, body: FindingReviewIn, db: Session = Depends(get_db),
                   user: str = Depends(current_user)):
    from ..agent.grounding import CATEGORIES
    f = db.get(Finding, finding_id)
    if not f:
        raise DomainError("Finding not found", 404, "not_found")
    latest = latest_analysis(db, f.case_id)
    if not latest or latest.id != f.analysis_id:
        raise DomainError("This finding belongs to an older analysis and is read-only", 409, "superseded")
    note = (body.note or "").strip()
    st = staleness(db, latest)
    stale_reasons = finding_staleness(f.citations, st["changed_kinds"]) if st["stale"] else []
    before = {"status": f.status, "title": f.title, "explanation": f.explanation, "category": f.category}
    if body.action == "accept":
        if "ungrounded" in (f.flags or []):
            raise DomainError("This finding cites no valid evidence and cannot be accepted. Reject it or edit it.",
                              422, "ungrounded")
        if stale_reasons:
            raise DomainError("Evidence this finding relies on has changed (" + ", ".join(stale_reasons) +
                              "). Re-run the analysis before accepting it.", 409, "stale_finding")
        f.status = "accepted"
    elif body.action == "reject":
        if len(note) < 3:
            raise DomainError("Say why the finding is rejected", 422, "validation_error", ["note: required"])
        f.status = "rejected"
    elif body.action == "edit":
        if len(note) < 3:
            raise DomainError("Say what you changed and why", 422, "validation_error", ["note: required"])
        if stale_reasons:
            raise DomainError("Evidence this finding relies on has changed; re-run the analysis first.",
                              409, "stale_finding")
        if body.category and body.category not in CATEGORIES:
            raise DomainError("Unknown category", 422, "validation_error", [f"category: one of {', '.join(CATEGORIES)}"])
        new_title = (body.title or f.title).strip()
        new_expl = (body.explanation or f.explanation).strip()
        if not new_title or not new_expl:
            raise DomainError("Title and explanation cannot be empty", 422, "validation_error")
        if f.original is None:
            f.original = before
        f.title, f.explanation = new_title[:300], new_expl
        f.category = body.category or f.category
        f.status = "edited"
    elif body.action == "reset":
        f.status = "proposed"
    else:
        raise DomainError("action must be accept, reject, edit or reset", 422, "validation_error")
    f.reviewer_note = note or f.reviewer_note
    f.reviewed_by = user
    f.reviewed_at = datetime.now(timezone.utc)
    after = {"status": f.status, "title": f.title, "explanation": f.explanation, "category": f.category}
    record(db, f.case_id, user, f"finding_{body.action}", f"{body.action.title()} finding “{f.title[:80]}”",
           {"finding_id": f.id, "before": before, "after": after, "note": note})
    db.commit()
    return analysis_payload(db, latest)


# --------------------------------------------------------------------------- reviewer actions
@router.post("/cases/{case_id}/info-requests", status_code=201)
def request_info(case_id: str, body: InfoRequestIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = get_case(db, case_id)
    if body.evidence_kind not in EVIDENCE_KINDS:
        raise DomainError("Choose the evidence you need", 422, "validation_error",
                          [f"evidence_kind: one of {', '.join(EVIDENCE_KINDS)}"])
    if len(body.message.strip()) < 5:
        raise DomainError("Describe what you need", 422, "validation_error", ["message: at least 5 characters"])
    dup = db.scalars(select(InfoRequest).where(InfoRequest.case_id == case_id, InfoRequest.status == "open",
                                               InfoRequest.evidence_kind == body.evidence_kind)).first()
    if dup:
        raise DomainError(f"An open request for {body.evidence_kind} evidence already exists ({dup.id})", 409,
                          "duplicate_request")
    req = InfoRequest(case_id=case_id, evidence_kind=body.evidence_kind, message=body.message.strip(),
                      requested_by=user)
    db.add(req)
    db.flush()
    record(db, case_id, user, "info_requested", f"Requested {body.evidence_kind} evidence: {body.message.strip()[:120]}",
           {"request_id": req.id, "evidence_kind": body.evidence_kind})
    set_status(db, case, "awaiting_info", user)
    db.commit()
    return {"id": req.id, "status": req.status}


@router.post("/info-requests/{request_id}/cancel")
def cancel_request(request_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    req = db.get(InfoRequest, request_id)
    if not req or req.status != "open":
        raise DomainError("Open request not found", 404, "not_found")
    req.status = "cancelled"
    record(db, req.case_id, user, "info_request_cancelled", f"Cancelled request {req.id}", {"request_id": req.id})
    db.commit()
    return {"id": req.id, "status": req.status}


@router.post("/cases/{case_id}/adjustments", status_code=201)
def approve_adjustment(case_id: str, body: ApproveIn, db: Session = Depends(get_db),
                       user: str = Depends(current_user), idempotency_key: str | None = Header(default=None)):
    adj, replay = adj_svc.approve(db, case_id, body.option_id, idempotency_key or "", body.note, user, body.amount,
                                  body.acknowledge_unverified_payments)
    db.commit()
    return {**ser_adjustment(adj), "replayed": replay}


@router.get("/cases/{case_id}/comparison")
def comparison(case_id: str, choices: str = Query(default="{}"), db: Session = Depends(get_db),
               user: str = Depends(current_user)):
    get_case(db, case_id)
    a, calc = _latest_calc(db, case_id)
    if not calc:
        raise DomainError("Run an analysis with an invoice and contract to compare", 409, "no_calculation")
    try:
        parsed = json.loads(choices)
        if not isinstance(parsed, dict):
            raise ValueError
    except ValueError:
        raise DomainError("choices must be a JSON object", 422, "validation_error")
    result = compare(calc, parsed)
    adjs = list(db.scalars(select(Adjustment).where(Adjustment.case_id == case_id)))
    adjusted = sum((D(x.amount) for x in adjs), ZERO)
    prior = D(calc["totals"]["prior_credits_total"])
    result["approved_adjustments"] = [ser_adjustment(x) for x in adjs]
    result["prior_credits_total"] = m2s(prior)
    result["net_after_credits"] = m2s(D(result["invoiced_total"]) - prior - adjusted)
    result["remaining_difference"] = m2s(D(result["invoiced_total"]) - prior - adjusted - D(result["recalculated_total"]))
    result["ambiguities"] = calc["ambiguities"]
    result["stale"] = staleness(db, a)["stale"]
    result["analysis_id"] = a.id
    return result


@router.post("/cases/{case_id}/resolve")
def resolve(case_id: str, body: ResolveIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = get_case(db, case_id)
    if len(body.summary.strip()) < 10:
        raise DomainError("Write a short resolution summary", 422, "validation_error", ["summary: at least 10 characters"])
    a = latest_analysis(db, case_id)
    if not a:
        raise DomainError("Run an analysis before resolving", 409, "no_analysis")
    st = staleness(db, a)
    if st["stale"]:
        raise DomainError("Evidence changed since the last analysis; re-run it before resolving", 409, "stale_analysis")
    pending = list(db.scalars(select(Finding).where(Finding.analysis_id == a.id, Finding.status == "proposed")))
    if pending:
        raise DomainError(f"Review all findings first ({len(pending)} still proposed)", 409, "findings_pending")
    open_reqs = list(db.scalars(select(InfoRequest).where(InfoRequest.case_id == case_id, InfoRequest.status == "open")))
    if open_reqs:
        raise DomainError("Cancel or fulfil open information requests first", 409, "requests_open")
    case.resolution_summary = body.summary.strip()
    set_status(db, case, "resolved", user, body.summary.strip()[:200])
    db.commit()
    return ser_case(case)


@router.post("/cases/{case_id}/reopen")
def reopen(case_id: str, body: ReasonIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = get_case(db, case_id)
    if case.status != "resolved":
        raise DomainError("Only resolved cases can be reopened", 409, "conflict")
    if len(body.reason.strip()) < 5:
        raise DomainError("Say why the case is reopened", 422, "validation_error", ["reason: required"])
    set_status(db, case, "reopened", user, body.reason.strip())
    db.commit()
    return ser_case(case)


# --------------------------------------------------------------------------- samples
@router.get("/samples")
def samples(user: str = Depends(current_user)):
    return sample_svc.list_samples()


@router.get("/samples/{key}/files/{filename}", response_class=PlainTextResponse)
def sample_file(key: str, filename: str, followup: bool = False, user: str = Depends(current_user)):
    return sample_svc.sample_file(key, filename, followup)


@router.post("/samples/{key}/cases", status_code=201)
def case_from_sample(key: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = sample_svc.create_from_sample(db, key, user)
    db.commit()
    return ser_case(case)


@router.post("/cases/{case_id}/sample-followups/{index}", status_code=201)
def add_sample_followup(case_id: str, index: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    case = get_case(db, case_id)
    ev, warnings = sample_svc.add_followup(db, case, index, user)
    db.commit()
    return {"evidence": ser_evidence(ev), "warnings": warnings}
