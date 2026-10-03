"""Persistence model.

Calculation output (CalculationRecord) and AI interpretation (Analysis, Finding,
ResolutionOption) live in separate tables on purpose: the AI never writes money.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CASE"))
    title: Mapped[str] = mapped_column(String(200))
    customer_name: Mapped[str] = mapped_column(String(200))
    dispute_description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    sample_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("EVD"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(255))
    raw_text: Mapped[str] = mapped_column(Text)
    parsed: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RUN"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    simulate_failures: Mapped[list] = mapped_column(JSON, default=list)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CalculationRecord(Base):
    """Deterministic engine output. Never written by the AI."""
    __tablename__ = "calculations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CALC"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    engine_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Analysis(Base):
    """AI (or fallback) interpretation of a calculation. Holds no authoritative money."""
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ANL"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    calculation_id: Mapped[str | None] = mapped_column(ForeignKey("calculations.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # llm | fallback
    case_summary: Mapped[str] = mapped_column(Text, default="")
    claim_assessment: Mapped[str] = mapped_column(Text, default="")
    missing_evidence: Mapped[list] = mapped_column(JSON, default=list)
    guardrail_report: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_llm_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("F"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(40))
    explanation: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="medium")
    discrepancy_ids: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    invalid_citations: Mapped[list] = mapped_column(JSON, default=list)
    unverified_numbers: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed|accepted|edited|rejected
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    original: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResolutionOption(Base):
    __tablename__ = "resolution_options"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("OPT"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    rationale: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(32))
    discrepancy_ids: Mapped[list] = mapped_column(JSON, default=list)
    ambiguity_choices: Mapped[dict] = mapped_column(JSON, default=dict)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    computation: Mapped[dict] = mapped_column(JSON, default=dict)  # deterministic breakdown at proposal time
    source: Mapped[str] = mapped_column(String(16))


class Adjustment(Base):
    """A mock credit/debit note approved by a reviewer. No money actually moves."""
    __tablename__ = "adjustments"
    __table_args__ = (UniqueConstraint("case_id", "fingerprint", name="uq_adjustment_fingerprint"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ADJ"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"))
    option_id: Mapped[str] = mapped_column(ForeignKey("resolution_options.id"))
    kind: Mapped[str] = mapped_column(String(16))  # credit | debit
    amount: Mapped[str] = mapped_column(String(32))
    computed_amount: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    discrepancy_ids: Mapped[list] = mapped_column(JSON, default=list)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    note: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AdjustmentItem(Base):
    __tablename__ = "adjustment_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    adjustment_id: Mapped[str] = mapped_column(ForeignKey("adjustments.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    discrepancy_id: Mapped[str] = mapped_column(String(32))
    amount: Mapped[str] = mapped_column(String(32))


class InfoRequest(Base):
    __tablename__ = "info_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("IR"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    evidence_kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    requested_by: Mapped[str] = mapped_column(String(64))
    fulfilled_by_evidence_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseEvent(Base):
    """Append-only audit history. Rows are never updated or deleted."""
    __tablename__ = "case_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(48))
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
