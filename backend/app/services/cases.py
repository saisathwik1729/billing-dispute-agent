"""Case lifecycle: evidence intake, history, staleness and reopening."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..evidence.parser import SINGLE_DOC_KINDS, EvidenceValidationError, parse_evidence
from ..logging_setup import app_log
from ..models import Analysis, Case, CaseEvent, Evidence, InfoRequest

CASE_STATUSES = ("draft", "in_review", "awaiting_info", "resolved", "reopened")

# Which evidence each citation family depends on. Discrepancy and ambiguity ids are
# products of the calculation, so they depend on every calculation input.
CALC_KINDS = {"invoice", "contract", "usage", "payments"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or []


def record(db: Session, case_id: str, actor: str, type_: str, summary: str, data: dict | None = None) -> CaseEvent:
    ev = CaseEvent(case_id=case_id, actor=actor, type=type_, summary=summary, data=data or {})
    db.add(ev)
    app_log.info("case_event", extra={"case_id": case_id, "event_type": type_, "actor": actor})
    return ev


def get_case(db: Session, case_id: str) -> Case:
    case = db.get(Case, case_id)
    if not case:
        raise DomainError(f"Case {case_id} not found", 404, "not_found")
    return case


def create_case(db: Session, title: str, customer_name: str, dispute: str, actor: str,
                sample_key: str | None = None) -> Case:
    title, customer_name = (title or "").strip(), (customer_name or "").strip()
    errors = []
    if not title:
        errors.append("title: required")
    if not customer_name:
        errors.append("customer_name: required")
    if len(title) > 200:
        errors.append("title: 200 characters at most")
    if errors:
        raise DomainError("Case details are incomplete", 422, "validation_error", errors)
    case = Case(title=title, customer_name=customer_name, dispute_description=(dispute or "").strip(),
                created_by=actor, sample_key=sample_key)
    db.add(case)
    db.flush()
    record(db, case.id, actor, "case_created", f"Case opened for {customer_name}", {"title": title})
    return case


def active_evidence(db: Session, case_id: str) -> list[Evidence]:
    return list(db.scalars(select(Evidence).where(Evidence.case_id == case_id, Evidence.active.is_(True))
                           .order_by(Evidence.created_at)))


def evidence_snapshot(db: Session, case_id: str) -> tuple[dict, str]:
    snap: dict[str, list[str]] = defaultdict(list)
    hashes = []
    for ev in active_evidence(db, case_id):
        snap[ev.kind].append(ev.id)
        hashes.append(f"{ev.id}:{ev.content_hash}")
    case = db.get(Case, case_id)
    dispute_hash = hashlib.sha256((case.dispute_description or "").encode()).hexdigest()[:16]
    snap["dispute"] = snap.get("dispute", []) + [f"desc:{dispute_hash}"]
    fp = hashlib.sha256("|".join(sorted(hashes) + [dispute_hash]).encode()).hexdigest()
    return {k: sorted(v) for k, v in snap.items()}, fp


def add_evidence(db: Session, case_id: str, kind: str, filename: str, content: str, actor: str,
                 replaces_id: str | None = None) -> tuple[Evidence, list[str]]:
    case = get_case(db, case_id)
    try:
        parsed = parse_evidence(kind, content)
    except EvidenceValidationError as exc:
        raise DomainError(f"The {kind} file could not be used", 422, "evidence_invalid", exc.errors)
    content_hash = hashlib.sha256(json.dumps(parsed.data, sort_keys=True).encode()).hexdigest()
    for ev in active_evidence(db, case_id):
        if ev.content_hash == content_hash and ev.kind == kind:
            raise DomainError(f"This {kind} evidence is already attached as {ev.id} ({ev.filename}).",
                              409, "duplicate_evidence")
    supersede: list[Evidence] = []
    if replaces_id:
        target = db.get(Evidence, replaces_id)
        if not target or target.case_id != case_id or not target.active:
            raise DomainError(f"Evidence {replaces_id} is not active on this case", 422, "validation_error")
        if target.kind != kind:
            raise DomainError(f"Evidence {replaces_id} is {target.kind}, not {kind}", 422, "validation_error")
        supersede.append(target)
    elif kind in SINGLE_DOC_KINDS:
        supersede.extend(ev for ev in active_evidence(db, case_id) if ev.kind == kind)

    ev = Evidence(case_id=case_id, kind=kind, filename=(filename or f"{kind}.txt")[:255], raw_text=content,
                  parsed=parsed.data, warnings=parsed.warnings, content_hash=content_hash, added_by=actor)
    db.add(ev)
    db.flush()
    for old in supersede:
        old.active = False
        old.superseded_by = ev.id
    record(db, case_id, actor, "evidence_added",
           f"Added {kind} evidence {ev.filename}" + (f", replacing {', '.join(o.id for o in supersede)}" if supersede else ""),
           {"evidence_id": ev.id, "kind": kind, "replaced": [o.id for o in supersede], "warnings": parsed.warnings})
    if kind == "dispute" and not case.dispute_description:
        case.dispute_description = parsed.data["text"]

    for req in db.scalars(select(InfoRequest).where(InfoRequest.case_id == case_id, InfoRequest.status == "open",
                                                    InfoRequest.evidence_kind == kind)):
        req.status = "fulfilled"
        req.fulfilled_by_evidence_id = ev.id
        req.fulfilled_at = datetime.now(timezone.utc)
        record(db, case_id, actor, "info_request_fulfilled", f"Request {req.id} fulfilled by {ev.id}",
               {"request_id": req.id, "evidence_id": ev.id})
    _on_evidence_changed(db, case, actor)
    return ev, parsed.warnings


def withdraw_evidence(db: Session, case_id: str, evidence_id: str, reason: str, actor: str) -> Evidence:
    case = get_case(db, case_id)
    ev = db.get(Evidence, evidence_id)
    if not ev or ev.case_id != case_id:
        raise DomainError("Evidence not found", 404, "not_found")
    if not ev.active:
        raise DomainError("Evidence is already inactive", 409, "conflict")
    if not (reason or "").strip():
        raise DomainError("Give a reason for withdrawing evidence", 422, "validation_error", ["reason: required"])
    ev.active = False
    ev.withdrawn_reason = reason.strip()
    record(db, case_id, actor, "evidence_withdrawn", f"Withdrew {ev.kind} evidence {ev.filename}",
           {"evidence_id": ev.id, "reason": reason})
    _on_evidence_changed(db, case, actor)
    return ev


def _on_evidence_changed(db: Session, case: Case, actor: str) -> None:
    if case.status == "resolved":
        case.status = "reopened"
        record(db, case.id, actor, "case_reopened", "Case reopened because evidence changed after resolution", {})
    latest = latest_analysis(db, case.id)
    if latest:
        db.flush()
        info = staleness(db, latest)
        if info["stale"]:
            record(db, case.id, "system", "analysis_stale",
                   f"Analysis {latest.id} is now stale: {', '.join(info['changed_kinds'])} evidence changed",
                   {"analysis_id": latest.id, "changed_kinds": info["changed_kinds"]})


def latest_analysis(db: Session, case_id: str) -> Analysis | None:
    return db.scalars(select(Analysis).where(Analysis.case_id == case_id)
                      .order_by(Analysis.created_at.desc())).first()


def staleness(db: Session, analysis: Analysis) -> dict:
    snap, fp = evidence_snapshot(db, analysis.case_id)
    if fp == analysis.evidence_fingerprint:
        return {"stale": False, "changed_kinds": []}
    old = analysis.evidence_snapshot or {}
    kinds = set(old) | set(snap)
    changed = sorted(k for k in kinds if sorted(old.get(k, [])) != sorted(snap.get(k, [])))
    return {"stale": True, "changed_kinds": changed}


def citation_kind(cite: dict) -> set[str]:
    t = cite.get("type")
    return {
        "invoice_line": {"invoice"}, "rule": {"contract"}, "usage_event": {"usage"},
        "payment": {"payments"}, "dispute": {"dispute"}, "note": {"note"},
        "discrepancy": CALC_KINDS, "ambiguity": CALC_KINDS, "data_quality": {"usage", "contract"},
    }.get(t, set())


def finding_staleness(finding_citations: list[dict], changed_kinds: list[str]) -> list[str]:
    deps: set[str] = set()
    for c in finding_citations:
        deps |= citation_kind(c)
    return sorted(deps & set(changed_kinds))


def set_status(db: Session, case: Case, status: str, actor: str, reason: str = "") -> None:
    if status not in CASE_STATUSES:
        raise DomainError(f"Unknown status {status}", 422, "validation_error")
    if case.status != status:
        old = case.status
        case.status = status
        record(db, case.id, actor, "status_changed", f"Status {old} → {status}" + (f": {reason}" if reason else ""),
               {"from": old, "to": status})
