"""Reviewer approval of mock credits/debits.

Duplicate protection, in layers:
1. Idempotency-Key: a retried request (double click, network retry) returns the
   adjustment it already created instead of creating another.
2. Remaining-balance accounting: each discrepancy can only be adjusted up to what is
   left after credits in the payment history and adjustments already approved here.
3. A unique (case, fingerprint) constraint in the database, so two concurrent
   requests for the same thing cannot both commit.
4. A per-case lock (plus SELECT ... FOR UPDATE on Postgres) serialising approvals.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..engine.money import D, ZERO, m2s
from ..engine.resolution import compute_option
from ..logging_setup import app_log
from ..models import Adjustment, AdjustmentItem, Analysis, CalculationRecord, Case, Finding, ResolutionOption
from .cases import DomainError, latest_analysis, record, staleness

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()
NON_FINANCIAL_ACTIONS = ("request_information", "no_adjustment")


def _case_lock(case_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks[case_id]


def credited_by_discrepancy(db: Session, case_id: str) -> tuple[dict[str, Decimal], Decimal]:
    per: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for item in db.scalars(select(AdjustmentItem).where(AdjustmentItem.case_id == case_id)):
        per[item.discrepancy_id] += D(item.amount)
    total = ZERO
    for adj in db.scalars(select(Adjustment).where(Adjustment.case_id == case_id)):
        if D(adj.amount) > 0:
            total += D(adj.amount)
    return dict(per), total


def preview_option(db: Session, option: ResolutionOption) -> dict:
    """Live amount for an option given everything approved so far."""
    analysis = db.get(Analysis, option.analysis_id)
    if not analysis or not analysis.calculation_id:
        return {"amount": "0.00", "kind": "none", "items": [], "warnings": ["No calculation available."],
                "choices": {}, "capped": False, "unknown_discrepancies": []}
    calc = db.get(CalculationRecord, analysis.calculation_id).result
    per, total = credited_by_discrepancy(db, option.case_id)
    return compute_option(calc, option.discrepancy_ids, option.ambiguity_choices, per, total)


def _allocate(items: list[dict], amount: Decimal) -> list[tuple[str, Decimal]]:
    """Spread the approved amount across the option's items, in order, same sign only."""
    out, left = [], amount
    for it in items:
        net = D(it["net"])
        if net == 0 or (net > 0) != (amount > 0):
            continue
        take = min(abs(net), abs(left))
        if take <= 0:
            break
        out.append((it["discrepancy_id"], take if amount > 0 else -take))
        left = left - take if amount > 0 else left + take
    return out


def approve(db: Session, case_id: str, option_id: str, idempotency_key: str, note: str, actor: str,
            amount_override: str | None = None, acknowledge_unverified_payments: bool = False
            ) -> tuple[Adjustment, bool]:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise DomainError("An Idempotency-Key header (1-128 characters) is required to approve an adjustment",
                          400, "idempotency_key_required")
    existing = db.scalars(select(Adjustment).where(Adjustment.idempotency_key == key)).first()
    if existing:
        if existing.case_id != case_id or existing.option_id != option_id:
            raise DomainError("This Idempotency-Key was already used for a different approval", 409, "idempotency_conflict")
        return existing, True

    note = (note or "").strip()
    if len(note) < 5:
        raise DomainError("Add a reviewer note explaining the decision", 422, "validation_error",
                          ["note: at least 5 characters"])

    with _case_lock(case_id):
        case = db.scalars(select(Case).where(Case.id == case_id).with_for_update()).first()
        if not case:
            raise DomainError("Case not found", 404, "not_found")
        option = db.get(ResolutionOption, option_id)
        if not option or option.case_id != case_id:
            raise DomainError("Resolution option not found on this case", 404, "not_found")
        if option.action in NON_FINANCIAL_ACTIONS:
            raise DomainError("This option does not change the invoice; nothing to approve", 422, "not_financial")
        analysis = db.get(Analysis, option.analysis_id)
        latest = latest_analysis(db, case_id)
        if not latest or latest.id != analysis.id:
            raise DomainError("This option belongs to an older analysis. Use the latest analysis.", 409, "superseded")
        st = staleness(db, analysis)
        if st["stale"]:
            raise DomainError("Evidence changed since this analysis (" + ", ".join(st["changed_kinds"]) +
                              "). Re-run the analysis before approving.", 409, "stale_analysis")

        payments_gap = any(m.get("evidence_kind") == "payments" for m in (analysis.missing_evidence or []))
        if payments_gap and not acknowledge_unverified_payments:
            raise DomainError("Payment and credit history was not available to this analysis, so an earlier credit "
                              "for the same issue cannot be ruled out. Confirm you have checked before approving.",
                              409, "payments_unverified")

        findings = list(db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)))
        supported = {d for f in findings if f.status in ("accepted", "edited") for d in f.discrepancy_ids}
        preview = preview_option(db, option)
        needed = [it["discrepancy_id"] for it in preview["items"] if D(it["net"]) != 0]
        unsupported = [d for d in needed if d not in supported]
        if unsupported:
            raise DomainError("Accept the findings that support this option first", 422, "findings_not_accepted",
                              [f"No accepted finding covers {d}" for d in unsupported])

        computed = D(preview["amount"])
        if computed == 0:
            prior = [a.id for a in db.scalars(select(Adjustment).where(Adjustment.case_id == case_id))]
            raise DomainError("Nothing remains to adjust for this option" +
                              (f"; already covered by {', '.join(prior)}" if prior else
                               " after credits already in the payment history"), 409, "already_adjusted")
        amount = computed
        if amount_override not in (None, ""):
            try:
                amount = D(amount_override)
            except Exception:
                raise DomainError("Amount must be a number", 422, "validation_error", ["amount: not a number"])
            if amount == 0 or (amount > 0) != (computed > 0) or abs(amount) > abs(computed):
                raise DomainError(f"Amount must be between 0.01 and {m2s(abs(computed))} with the same sign as the "
                                  "calculated amount", 422, "validation_error", ["amount: out of range"])
            if amount.as_tuple().exponent < -2:
                raise DomainError("Amount can have at most two decimal places", 422, "validation_error")
        amount = D(m2s(amount))
        allocation = _allocate(preview["items"], amount)
        fingerprint = hashlib.sha256(json.dumps(
            [case_id, sorted((d, m2s(a)) for d, a in allocation), preview["choices"]], sort_keys=True).encode()
        ).hexdigest()
        dup = db.scalars(select(Adjustment).where(Adjustment.case_id == case_id,
                                                  Adjustment.fingerprint == fingerprint)).first()
        if dup:
            raise DomainError(f"An identical adjustment already exists ({dup.id})", 409, "duplicate_adjustment")

        adj = Adjustment(case_id=case_id, analysis_id=analysis.id, option_id=option.id,
                         kind="credit" if amount > 0 else "debit", amount=m2s(amount), computed_amount=m2s(computed),
                         currency=preview["currency"], discrepancy_ids=[d for d, _ in allocation],
                         breakdown=preview, fingerprint=fingerprint, idempotency_key=key, note=note, approved_by=actor)
        db.add(adj)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            again = db.scalars(select(Adjustment).where(Adjustment.idempotency_key == key)).first()
            if again:
                return again, True
            raise DomainError("A matching adjustment was approved at the same moment", 409, "duplicate_adjustment")
        for did, amt in allocation:
            db.add(AdjustmentItem(adjustment_id=adj.id, case_id=case_id, discrepancy_id=did, amount=m2s(amt)))
        verb = "credit" if amount > 0 else "debit"
        record(db, case_id, actor, "adjustment_approved",
               f"Approved mock {verb} {adj.id} for {preview['currency']} {m2s(abs(amount))} from option “{option.title}”",
               {"adjustment_id": adj.id, "amount": m2s(amount), "computed_amount": m2s(computed),
                "option_id": option.id, "discrepancy_ids": adj.discrepancy_ids, "note": note,
                "override": amount != computed, "acknowledged_unverified_payments": payments_gap})
        app_log.info("adjustment_approved", extra={"case_id": case_id, "adjustment_id": adj.id,
                                                   "amount": m2s(amount), "option_id": option.id})
        return adj, False
