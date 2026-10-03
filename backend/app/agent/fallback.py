"""Deterministic analysis used when the model is unavailable.

It produces the same JSON shape as the model, so it passes through the same grounding
checks. It is plainer than the model's analysis and cannot weigh supporting documents,
which the UI makes clear.
"""
from __future__ import annotations

from ..engine.money import D


def _f(v) -> str:
    return f"{D(v):,.2f}"


def build_fallback(calc: dict | None, ctx: dict) -> dict:
    findings: list[dict] = []
    options: list[dict] = []
    missing = list(ctx.get("deterministic_missing_evidence") or [])
    cur = (calc or {}).get("currency", "USD")

    if not calc:
        findings.append({
            "title": "Invoice could not be recalculated",
            "category": "needs_evidence",
            "explanation": "The deterministic recalculation did not run, so no amount can be confirmed. "
                           "See the missing evidence list and the agent trace for the reason.",
            "confidence": "high", "discrepancy_ids": [], "citations": ["DISPUTE"],
        })
        options.append({"title": "Request missing evidence", "action": "request_information",
                        "rationale": "Nothing can be adjusted until the invoice can be recalculated.",
                        "discrepancy_ids": [], "ambiguity_choices": {}, "citations": ["DISPUTE"]})
        return {"case_summary": "The invoice could not be recalculated with the evidence available. "
                                "Collect the missing evidence and run the analysis again.",
                "claim_assessment": "Not assessed: no recalculation available.",
                "findings": findings, "missing_evidence": missing, "resolution_options": options}

    amb = {a["id"]: a for a in calc["ambiguities"]}
    for d in calc["discrepancies"]:
        refs = d["line_ids"] + d["rule_ids"] + d["event_ids"][:10] + [d["id"]]
        if d["kind"] == "calculation_error":
            direction = "Overcharge" if D(d["amount"]) > 0 else "Undercharge"
            label = "invoice total" if d["key"] == "__invoice_total__" else d["key"]
            text = d["description"] + (" " + " ".join(d["causes"]) if d["causes"] else "")
            if D(d.get("prior_credit_applied", "0")) > 0:
                text += (f" Credit {', '.join(d['prior_credit_ids'])} already covers {cur} "
                         f"{_f(d['prior_credit_applied'])} of this.")
                refs += d["prior_credit_ids"]
            findings.append({"title": f"{direction} on {label}", "category": "calculation_error",
                             "explanation": text, "confidence": "high", "discrepancy_ids": [d["id"]],
                             "citations": refs})
        elif d["kind"] == "interpretation_dependent":
            questions = " ".join(amb[a]["description"] for a in d["ambiguity_ids"] if a in amb)
            findings.append({"title": f"{d['key']} depends on how the contract is read",
                             "category": "contract_interpretation",
                             "explanation": d["description"] + " " + questions,
                             "confidence": "medium", "discrepancy_ids": [d["id"]],
                             "citations": refs + d["ambiguity_ids"]})
        else:
            findings.append({"title": f"{d['key']} has no contract basis on file", "category": "needs_evidence",
                             "explanation": d["description"], "confidence": "high",
                             "discrepancy_ids": [d["id"]], "citations": refs})
    for q in calc["data_quality"]:
        findings.append({"title": "Usage data issue: " + q["type"].replace("_", " "), "category": "data_quality",
                         "explanation": q["description"], "confidence": "high", "discrepancy_ids": [],
                         "citations": [q["id"]] + q.get("event_ids", [])[:10] + q.get("rule_ids", [])})
    for r in calc["unsupported_rules"]:
        findings.append({"title": f"Clause {r['id']} needs a human reading", "category": "contract_interpretation",
                         "explanation": f"The engine cannot price clause {r['id']}: “{r['text']}”. "
                                        "A reviewer should decide whether it affects this invoice.",
                         "confidence": "low", "discrepancy_ids": [], "citations": [r["id"]]})

    calc_pos = [d["id"] for d in calc["discrepancies"] if d["kind"] == "calculation_error" and D(d["amount"]) > 0]
    calc_all = [d["id"] for d in calc["discrepancies"] if d["kind"] == "calculation_error"]
    interp = [d for d in calc["discrepancies"] if d["kind"] == "interpretation_dependent"]
    if calc_pos:
        options.append({"title": "Credit confirmed overcharges", "action": "credit",
                        "rationale": "Corrects only errors that hold under every contract reading.",
                        "discrepancy_ids": calc_pos, "ambiguity_choices": {}, "citations": calc_pos})
    if len(calc_all) > len(calc_pos):
        options.append({"title": "Net settlement of all confirmed errors", "action": "net_settlement",
                        "rationale": "Corrects overcharges and undercharges together, which can offset.",
                        "discrepancy_ids": calc_all, "ambiguity_choices": {}, "citations": calc_all})
    if interp:
        choices: dict[str, str] = {}
        for d in interp:
            best = max(d["outcomes"], key=lambda o: D(o["delta"]))
            choices.update(best["choices"])
        ids = calc_pos + [d["id"] for d in interp]
        options.append({"title": "Credit using the customer-favourable readings", "action": "credit",
                        "rationale": "Resolves every open contract question in the customer's favour. "
                                     "Choose only if the business accepts those readings.",
                        "discrepancy_ids": ids, "ambiguity_choices": choices,
                        "citations": ids + list(choices)})
    if missing:
        options.append({"title": "Request missing evidence before deciding", "action": "request_information",
                        "rationale": "Some conclusions depend on evidence not yet on file.",
                        "discrepancy_ids": [], "ambiguity_choices": {}, "citations": ["DISPUTE"]})
    if not options:
        options.append({"title": "No adjustment", "action": "no_adjustment",
                        "rationale": "The invoice matches the contract and usage on file.",
                        "discrepancy_ids": [], "ambiguity_choices": {}, "citations": ["DISPUTE"]})

    t = calc["totals"]
    summary = (f"Invoice {calc['invoice_id']} totals {cur} {_f(t['invoiced_total'])}. Recalculation confirms a net "
               f"difference of {cur} {_f(t['confirmed_net_difference'])} (overcharges {_f(t['confirmed_overcharge'])}, "
               f"undercharges {_f(t['confirmed_undercharge'])}). "
               f"{len(interp)} charge(s) depend on contract interpretation and {len(calc['data_quality'])} usage data "
               f"issue(s) were found. Prior credits on file: {cur} {_f(t['prior_credits_total'])}.")
    return {"case_summary": summary,
            "claim_assessment": "Generated without the language model; compare the customer's claims against the "
                                "findings below.",
            "findings": findings, "missing_evidence": missing, "resolution_options": options}
