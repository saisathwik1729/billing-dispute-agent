"""Guardrails between the model and the reviewer.

Every model (or fallback) output passes through here before anyone sees it:

* citations are resolved against the real evidence; unknown ids are stripped and
  reported, and a finding left with no valid citation is marked ungrounded and cannot
  be accepted;
* the finding's category is checked against the engine: a "calculation error" must rest
  on a discrepancy the engine classed as a calculation error, and vice versa;
* every money-looking number in the prose is checked against numbers that exist in the
  evidence or the calculation, and unverified figures are flagged;
* resolution options get their amounts from ``engine.resolution`` only.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from ..engine.money import D, MoneyError, ZERO
from ..engine.resolution import compute_option

CATEGORIES = ("calculation_error", "contract_interpretation", "data_quality", "customer_misunderstanding",
              "needs_evidence")
ACTIONS = ("credit", "rebill", "net_settlement", "no_adjustment", "request_information")
CONFIDENCE = ("high", "medium", "low")
EVIDENCE_KINDS = ("invoice", "contract", "usage", "payments", "dispute", "note")

_MONEY_RE = re.compile(
    r"(?:USD|US\$|\$|€|£|₹)\s?(-?\d[\d,]*(?:\.\d+)?)"
    r"|(?<![\w.\-/])(-?\d{1,3}(?:,\d{3})+\.\d{2}|-?\d+\.\d{2})(?!\d)")
_ANY_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _q(v: Decimal) -> Decimal:
    return abs(v).quantize(Decimal("0.01"))


def _walk_strings(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_strings(v, out)
    elif obj is not None and not isinstance(obj, bool):
        out.append(str(obj))


def allowed_numbers(*sources) -> set[Decimal]:
    strings: list[str] = []
    for s in sources:
        _walk_strings(s, strings)
    allowed: set[Decimal] = set()
    for s in strings:
        for tok in _ANY_NUM_RE.findall(s):
            try:
                allowed.add(_q(D(tok)))
            except MoneyError:
                pass
    return allowed


def unverified_numbers(text: str, allowed: set[Decimal]) -> list[str]:
    bad = []
    for m in _MONEY_RE.finditer(text or ""):
        tok = m.group(1) or m.group(2)
        try:
            if _q(D(tok)) not in allowed:
                bad.append(m.group(0).strip())
        except MoneyError:
            continue
    return sorted(set(bad))


def build_reference_index(bundle: dict, calc: dict | None) -> dict[str, dict]:
    idx: dict[str, dict] = {"DISPUTE": {"type": "dispute", "label": "Customer dispute statement"}}
    inv = bundle.get("invoice")
    if inv:
        for ln in inv["lines"]:
            idx[ln["line_id"]] = {"type": "invoice_line",
                                  "label": f"Invoice {inv['invoice_id']} line {ln['line_id']}: "
                                           f"{ln['description'] or ln['sku']} ({ln['amount']})"}
    con = bundle.get("contract")
    if con:
        for r in con["rules"]:
            idx[r["id"]] = {"type": "rule", "label": f"Rule {r['id']} ({r['type'].replace('_', ' ')})"
                                                      + (f": {r['text'][:110]}" if r.get("text") else "")}
    for e in bundle.get("usage") or []:
        idx.setdefault(e["event_id"], {"type": "usage_event",
                                       "label": f"Usage {e['event_id']}: {e['sku']} {e['quantity']} at "
                                                f"{e['timestamp'][:16].replace('T', ' ')}"
                                                + (f" [{', '.join(e['tags'])}]" if e["tags"] else "")})
    if bundle.get("payments_available"):
        for p in bundle.get("payments") or []:
            idx[p["id"]] = {"type": "payment", "label": f"{p['type'].title()} {p['id']}: {p['amount']} on {p['date']}"
                                                        + (f" ({p['reason'][:60]})" if p["reason"] else "")}
    for n in bundle.get("notes") or []:
        idx[n["evidence_id"]] = {"type": "note", "label": f"Note {n['filename']}"}
    if calc:
        for d in calc["discrepancies"]:
            idx[d["id"]] = {"type": "discrepancy", "label": f"Discrepancy {d['id']}: {d['description'][:120]}"}
        for a in calc["ambiguities"]:
            idx[a["id"]] = {"type": "ambiguity", "label": f"Open question {a['id']}: {a['description'][:120]}"}
        for q in calc["data_quality"]:
            idx[q["id"]] = {"type": "data_quality", "label": f"Data issue {q['id']}: {q['description'][:120]}"}
    return idx


def _ids(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]


def _resolve(refs: list[str], index: dict, origin: str) -> tuple[list[dict], list[str]]:
    good, bad = [], []
    for r in dict.fromkeys(refs):
        if r in index:
            good.append({"ref": r, "type": index[r]["type"], "label": index[r]["label"], "origin": origin})
        else:
            bad.append(r)
    return good, bad


def ground(output: dict, index: dict, calc: dict | None, allowed: set[Decimal], source: str,
           credited: dict | None = None, credited_total: Decimal = ZERO,
           deterministic_missing: list[dict] | None = None) -> dict:
    disc = {d["id"]: d for d in (calc or {}).get("discrepancies", [])}
    clause_ids = {r["id"] for r in (calc or {}).get("unsupported_rules", [])}
    report = {"source": source, "citations_checked": 0, "citations_invalid": 0, "recategorized": 0,
              "ungrounded": 0, "unverified_numbers": 0, "dropped_items": 0, "notes": []}

    findings = []
    for i, f in enumerate(output.get("findings") or []):
        if not isinstance(f, dict):
            report["dropped_items"] += 1
            continue
        flags: list[str] = []
        title = str(f.get("title") or "").strip()[:300] or f"Finding {i + 1}"
        explanation = str(f.get("explanation") or "").strip()
        if not explanation:
            report["dropped_items"] += 1
            continue
        category = f.get("category")
        if category not in CATEGORIES:
            flags.append(f"Unknown category '{category}' replaced with needs_evidence")
            category = "needs_evidence"
        confidence = f.get("confidence") if f.get("confidence") in CONFIDENCE else "medium"
        raw_d = _ids(f.get("discrepancy_ids"))
        d_ids = [d for d in raw_d if d in disc]
        bad_d = [d for d in raw_d if d not in disc]
        cites_raw = _ids(f.get("citations"))
        d_ids += [c for c in cites_raw if c in disc and c not in d_ids]
        cites, bad = _resolve(cites_raw, index, "agent")
        bad += [b for b in bad_d if b not in bad]
        report["citations_checked"] += len(cites_raw) + len(raw_d)
        report["citations_invalid"] += len(bad)
        if bad:
            flags.append("Removed citations that match no evidence: " + ", ".join(bad))
        have = {c["ref"] for c in cites}
        engine_refs = []
        for did in d_ids:
            d = disc[did]
            for r in [did, *d["line_ids"], *d["rule_ids"]]:
                if r not in have and r in index:
                    engine_refs.append(r)
                    have.add(r)
        extra, _ = _resolve(engine_refs, index, "engine")
        cites += extra

        kinds = {disc[d]["kind"] for d in d_ids}
        cited_types = {c["type"] for c in cites}
        interp_signal = ("interpretation_dependent" in kinds or "ambiguity" in cited_types
                         or any(c["ref"] in clause_ids for c in cites) or "note" in cited_types)
        if category == "calculation_error" and "calculation_error" not in kinds:
            new = ("contract_interpretation" if interp_signal
                   else "needs_evidence")
            flags.append(f"Recategorized from calculation_error to {new}: the engine found no calculation error "
                         "in the cited records")
            category = new
            report["recategorized"] += 1
        elif category == "contract_interpretation" and kinds == {"calculation_error"} and not interp_signal:
            flags.append("Recategorized from contract_interpretation to calculation_error: the cited discrepancy "
                         "holds under every reading of the contract")
            category = "calculation_error"
            report["recategorized"] += 1

        nums = unverified_numbers(title + " " + explanation, allowed)
        if nums:
            flags.append("Figures not found in the evidence or calculation: " + ", ".join(nums))
            report["unverified_numbers"] += len(nums)
        grounded = bool(cites)
        if not grounded:
            report["ungrounded"] += 1
            flags.append("No valid citation: this finding cannot be accepted until it is grounded")
        findings.append({"title": title, "category": category, "explanation": explanation,
                         "confidence": confidence, "discrepancy_ids": d_ids, "citations": cites,
                         "invalid_citations": bad, "unverified_numbers": nums, "flags": flags,
                         "grounded": grounded})

    options = []
    for o in (output.get("resolution_options") or [])[:6]:
        if not isinstance(o, dict):
            report["dropped_items"] += 1
            continue
        flags = []
        action = o.get("action")
        raw_d = _ids(o.get("discrepancy_ids"))
        d_ids = [d for d in raw_d if d in disc]
        if action not in ACTIONS:
            flags.append(f"Unknown action '{action}'")
            action = "credit" if d_ids else "request_information"
        if len(d_ids) != len(raw_d):
            flags.append("Ignored unknown discrepancy ids: " + ", ".join(d for d in raw_d if d not in disc))
        choices = o.get("ambiguity_choices") if isinstance(o.get("ambiguity_choices"), dict) else {}
        choices = {str(k): str(v) for k, v in choices.items()}
        cites, bad = _resolve(_ids(o.get("citations")) + d_ids, index, "agent")
        comp = None
        if calc and action not in ("no_adjustment", "request_information"):
            comp = compute_option(calc, d_ids, choices, credited or {}, credited_total)
            flags += comp["warnings"]
            if action == "credit" and D(comp["amount"]) <= 0:
                flags.append("The calculated amount for this option is not a credit")
            if action == "rebill" and D(comp["amount"]) >= 0:
                flags.append("The calculated amount for this option is not an additional charge")
        rationale = str(o.get("rationale") or "").strip()
        nums = unverified_numbers(rationale + " " + str(o.get("title") or ""), allowed)
        if nums:
            flags.append("Figures not found in the evidence or calculation: " + ", ".join(nums))
            report["unverified_numbers"] += len(nums)
        options.append({"title": str(o.get("title") or "Option").strip()[:300], "action": action,
                        "rationale": rationale, "discrepancy_ids": d_ids,
                        "ambiguity_choices": (comp or {}).get("choices", {}) if comp else {},
                        "citations": cites, "computation": comp or {}, "flags": flags})

    missing, seen = [], set()
    for m in (deterministic_missing or []):
        key = (m["evidence_kind"], m["reason"].lower())
        if key not in seen:
            seen.add(key)
            missing.append({**m, "source": "engine"})
    for m in output.get("missing_evidence") or []:
        if not isinstance(m, dict) or m.get("evidence_kind") not in EVIDENCE_KINDS or not m.get("reason"):
            continue
        key = (m["evidence_kind"], str(m["reason"]).lower())
        if key not in seen:
            seen.add(key)
            missing.append({"evidence_kind": m["evidence_kind"], "reason": str(m["reason"])[:500],
                            "blocking": bool(m.get("blocking")), "source": source})

    summary = str(output.get("case_summary") or "").strip()
    assessment = str(output.get("claim_assessment") or "").strip()
    sum_nums = unverified_numbers(summary + " " + assessment, allowed)
    if sum_nums:
        report["notes"].append("Summary mentions figures not found in the evidence: " + ", ".join(sum_nums))
        report["unverified_numbers"] += len(sum_nums)
    return {"case_summary": summary, "claim_assessment": assessment, "findings": findings,
            "resolution_options": options, "missing_evidence": missing, "report": report}


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
