"""Turn a resolution option (which discrepancies, which contract readings) into money.

The AI proposes options by *reference*: discrepancy ids plus, for interpretation
questions, the reading it recommends. This module is the only place an option's
amount is produced. It deducts credits already issued outside this tool (from the
payment history) and credits already approved inside it, so the same overcharge can
never be credited twice.
"""
from __future__ import annotations

from decimal import Decimal

from .money import D, ZERO, m2s


def compute_option(calc: dict, discrepancy_ids: list[str], choices: dict | None,
                   credited_by_discrepancy: dict[str, Decimal] | None = None,
                   credited_total: Decimal = ZERO) -> dict:
    choices = {k: v for k, v in (choices or {}).items() if v}
    credited_by_discrepancy = credited_by_discrepancy or {}
    by_id = {d["id"]: d for d in calc["discrepancies"]}
    amb_ids = {a["id"]: a for a in calc["ambiguities"]}
    items, warnings, unknown = [], [], []
    total = ZERO

    for bad in [k for k in choices if k not in amb_ids]:
        warnings.append(f"Reading for unknown question {bad} ignored.")
    for aid, label in choices.items():
        if aid in amb_ids and label not in [o["label"] for o in amb_ids[aid]["options"]]:
            warnings.append(f"'{label}' is not a valid reading for {aid}; the conservative reading is used.")
    valid_choices = {k: v for k, v in choices.items()
                     if k in amb_ids and v in [o["label"] for o in amb_ids[k]["options"]]}

    for did in dict.fromkeys(discrepancy_ids):
        d = by_id.get(did)
        if d is None:
            unknown.append(did)
            continue
        note = ""
        if d["kind"] == "needs_evidence":
            items.append({"discrepancy_id": did, "kind": d["kind"], "gross": "0.00", "prior_credit": "0.00",
                          "already_adjusted": "0.00", "net": "0.00",
                          "note": "Not adjustable until supporting evidence confirms the charge."})
            continue
        if d["kind"] == "calculation_error":
            gross = D(d["amount"])
        else:
            outcomes = [o for o in d["outcomes"]
                        if all(o["choices"].get(k) == v for k, v in valid_choices.items() if k in o["choices"])]
            if not outcomes:
                outcomes = d["outcomes"]
            missing = [a for a in d["ambiguity_ids"] if a not in valid_choices]
            # Unanswered questions resolve to the reading least favourable to a credit.
            gross = min(D(o["delta"]) for o in outcomes)
            if missing:
                note = ("No reading chosen for " + ", ".join(missing) +
                        "; the reading least favourable to a credit is used.")
        prior = D(d.get("prior_credit_applied", "0"))
        already = credited_by_discrepancy.get(did, ZERO)
        if gross > 0:
            net = max(ZERO, gross - prior - already)
        elif gross < 0:
            net = min(ZERO, gross - already)
        else:
            net = ZERO
        total += net
        items.append({"discrepancy_id": did, "kind": d["kind"], "gross": m2s(gross), "prior_credit": m2s(prior),
                      "already_adjusted": m2s(already), "net": m2s(net), "note": note})

    ceiling = D(calc["totals"]["credit_ceiling"]) - max(ZERO, credited_total)
    capped = False
    if total > ceiling:
        total = max(ZERO, ceiling)
        capped = True
        warnings.append("Amount capped so total credits never exceed the invoice net of earlier credits.")
    if unknown:
        warnings.append("Unknown discrepancy reference(s) ignored: " + ", ".join(unknown))
    return {
        "amount": m2s(total),
        "kind": "credit" if total > 0 else ("debit" if total < 0 else "none"),
        "items": items,
        "choices": valid_choices,
        "capped": capped,
        "warnings": warnings,
        "unknown_discrepancies": unknown,
        "currency": calc["currency"],
    }
