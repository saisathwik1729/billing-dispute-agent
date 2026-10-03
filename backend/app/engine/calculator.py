"""Deterministic invoice recalculation.

Given the canonical invoice, contract, usage events and payment history, the engine
recomputes what the invoice should have been and explains every difference.

Calculation errors vs. interpretation
-------------------------------------
Some inputs can be read more than one way: a tiered price with no tier mode, usage
tagged with a maintenance type the contract is vague about, or two meter events that
look like duplicates but carry different ids. Each of these becomes an *ambiguity*
with a fixed set of readings. The engine prices the invoice under every combination
of readings (a *scenario*). For each invoice line:

* if every scenario gives the same answer, any difference is a ``calculation_error``;
* otherwise the part of the difference that has the same sign under every reading is
  still a ``calculation_error`` (it is owed whichever reading wins), and the rest is
  ``interpretation_dependent`` with one outcome per reading.

So the engine never decides a contract question; it prices each answer and leaves the
choice to a person. The AI explains; it never calculates.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from .money import D, ZERO, m2s, money, n2s

ENGINE_VERSION = "1.3.0"
MAX_FULL_SCENARIOS = 64
CREDIT_TYPES = ("credit", "adjustment")


class CalculationInputError(ValueError):
    pass


def _hid(prefix: str, *parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha1(raw.encode()).hexdigest()[:6].upper()}"


def _fmt_qty(value) -> str:
    d = D(value)
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return f"{d.normalize():,f}"


def _fmt_money(value) -> str:
    return f"{money(value):,.2f}"


def _edate(ev: dict) -> date:
    return datetime.fromisoformat(ev["timestamp"]).date()


def _label(rule: dict) -> str:
    """Short human name for a contract line: explicit label, else 'Name: ...' wording, else by type."""
    if rule.get("label"):
        return rule["label"]
    text = (rule.get("text") or "").strip()
    if ":" in text[:50]:
        return text.split(":")[0].strip()
    if rule["type"] == "minimum_commit":
        return "Minimum commitment true-up"
    if rule["type"] == "discount_percent":
        return f"{n2s(rule['percent'])}% discount"
    return rule.get("sku", rule["id"]).replace("_", " ").capitalize()


def input_hash(invoice, contract, usage, payments) -> str:
    blob = json.dumps([invoice, contract, usage, payments], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- pricing
def price_tiered(qty: Decimal, tiers: list[dict], mode: str) -> tuple[Decimal, str]:
    parts = []
    if mode == "volume":
        for t in tiers:
            if t["up_to"] is None or qty <= D(t["up_to"]):
                price = D(t["unit_price"])
                amt = money(qty * price)
                return amt, f"volume pricing: all {_fmt_qty(qty)} units at {n2s(price)} = {_fmt_money(amt)}"
    total = ZERO
    lower = ZERO
    for t in tiers:
        if qty <= lower:
            break
        upper = qty if t["up_to"] is None else min(qty, D(t["up_to"]))
        band = upper - lower
        if band > 0:
            price = D(t["unit_price"])
            total += band * price
            parts.append(f"{_fmt_qty(band)} × {n2s(price)}")
        lower = upper if t["up_to"] is None else D(t["up_to"])
    amt = money(total)
    return amt, "graduated pricing: " + " + ".join(parts or ["0"]) + f" = {_fmt_money(amt)}"


# --------------------------------------------------------------------------- engine
class _Engine:
    def __init__(self, invoice: dict, contract: dict, usage: list[dict], payments: list[dict]):
        if not invoice:
            raise CalculationInputError("An invoice is required to recalculate.")
        if not contract:
            raise CalculationInputError("Pricing or contract rules are required to recalculate.")
        self.invoice = invoice
        self.contract = contract
        self.payments = payments or []
        self.start = date.fromisoformat(invoice["period"]["start"])
        self.end = date.fromisoformat(invoice["period"]["end"])
        self.days = (self.end - self.start).days + 1
        self.rules = contract["rules"]
        self.rule_by_sku = {r["sku"]: r for r in self.rules
                            if r["type"] in ("flat", "per_unit", "tiered", "included_quota")}
        self.notes: list[dict] = []
        self.ambiguities: list[dict] = []
        self._prepare_usage(usage or [])
        self._detect_ambiguities()

    # ---------------------------------------------------------------- usage prep
    def _measure(self, sku: str) -> str:
        rule = self.rule_by_sku.get(sku)
        return (rule or {}).get("measure", "sum")

    def _prepare_usage(self, events: list[dict]) -> None:
        self.raw_by_sku: dict[str, list[dict]] = defaultdict(list)
        seen: dict[str, dict] = {}
        self.duplicate_ids: list[str] = []
        unique: list[dict] = []
        for ev in events:
            self.raw_by_sku[ev["sku"]].append(ev)
            prior = seen.get(ev["event_id"])
            if prior is not None:
                self.duplicate_ids.append(ev["event_id"])
                same = prior == ev
                self.notes.append({
                    "id": _hid("Q", "dup", ev["event_id"], len(self.duplicate_ids)),
                    "type": "duplicate_event_id",
                    "event_ids": [ev["event_id"]],
                    "description": (f"Event {ev['event_id']} appears more than once"
                                    + ("" if same else " with different contents")
                                    + "; it is counted once."),
                })
                continue
            seen[ev["event_id"]] = ev
            unique.append(ev)

        known_skus = set(self.rule_by_sku)
        self.out_of_period: dict[str, list[dict]] = defaultdict(list)
        self.excluded: dict[str, list[dict]] = defaultdict(list)
        self.base: dict[str, list[dict]] = defaultdict(list)  # billable before ambiguity choices
        exclusion_rules = [r for r in self.rules if r["type"] == "usage_exclusion"]
        unknown = defaultdict(list)
        for ev in unique:
            sku, d = ev["sku"], _edate(ev)
            if sku not in known_skus:
                unknown[sku].append(ev["event_id"])
            if self._measure(sku) == "snapshot":
                if d > self.end:
                    self.out_of_period[sku].append(ev)
                    continue
            elif d < self.start or d > self.end:
                self.out_of_period[sku].append(ev)
                continue
            rule = next((r for r in exclusion_rules if r["sku"] == sku
                         and set(ev["tags"]) & set(r["exclude_tags"])), None)
            if rule:
                self.excluded[sku].append({**ev, "_rule": rule["id"]})
                continue
            self.base[sku].append(ev)

        for sku, evs in self.out_of_period.items():
            self.notes.append({
                "id": _hid("Q", "oop", sku), "type": "outside_billing_period",
                "event_ids": [e["event_id"] for e in evs],
                "description": (f"{len(evs)} {sku} event(s) totalling {_fmt_qty(sum(D(e['quantity']) for e in evs))} "
                                f"fall outside the billing period {self.start} to {self.end} and are not billable "
                                "on this invoice."),
            })
        for sku, evs in self.excluded.items():
            rid = evs[0]["_rule"]
            self.notes.append({
                "id": _hid("Q", "excl", sku), "type": "contract_exclusion", "rule_ids": [rid],
                "event_ids": [e["event_id"] for e in evs],
                "description": (f"{len(evs)} {sku} event(s) totalling {_fmt_qty(sum(D(e['quantity']) for e in evs))} "
                                f"carry a tag that rule {rid} makes non-billable."),
            })
        for sku, ids in unknown.items():
            self.notes.append({
                "id": _hid("Q", "unk", sku), "type": "unknown_sku", "event_ids": ids[:20],
                "description": f"{len(ids)} usage event(s) for {sku}, which no pricing rule covers.",
            })

    # ---------------------------------------------------------------- ambiguities
    def _detect_ambiguities(self) -> None:
        for r in self.rules:
            if r["type"] == "tiered" and not r.get("tier_mode"):
                self.ambiguities.append({
                    "id": _hid("A", "tier", r["id"]), "type": "tier_mode", "rule_ids": [r["id"]],
                    "event_ids": [], "skus": [r["sku"]],
                    "description": (f"Rule {r['id']} lists price tiers for {r['sku']} but does not say whether "
                                    "each tier prices only the units inside it (graduated) or all units at the "
                                    "rate of the tier reached (volume)."),
                    "options": [
                        {"label": "graduated", "description": "Each tier prices only the units inside it."},
                        {"label": "volume", "description": "All units priced at the rate of the tier reached."},
                    ],
                })
            if r["type"] == "usage_exclusion" and r["disputed_tags"]:
                evs = [e for e in self.base.get(r["sku"], []) if set(e["tags"]) & set(r["disputed_tags"])]
                if evs:
                    qty = sum(D(e["quantity"]) for e in evs)
                    self.ambiguities.append({
                        "id": _hid("A", "excl", r["id"]), "type": "disputed_usage", "rule_ids": [r["id"]],
                        "event_ids": [e["event_id"] for e in evs], "skus": [r["sku"]],
                        "description": (f"{len(evs)} {r['sku']} event(s) totalling {_fmt_qty(qty)} are tagged "
                                        f"{', '.join(r['disputed_tags'])}; rule {r['id']} does not clearly say "
                                        "whether that usage is billable."),
                        "options": [
                            {"label": "billable", "description": "Treat the tagged usage as billable."},
                            {"label": "not_billable", "description": "Treat the tagged usage as excluded."},
                        ],
                    })
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for sku, evs in self.base.items():
            if self._measure(sku) == "snapshot":
                continue
            for e in evs:
                groups[(sku, e["timestamp"], e["quantity"])].append(e)
        for (sku, ts, qty), evs in sorted(groups.items()):
            if len(evs) > 1:
                ids = [e["event_id"] for e in evs]
                self.ambiguities.append({
                    "id": _hid("A", "pdup", *ids), "type": "possible_duplicate", "rule_ids": [],
                    "event_ids": ids, "skus": [sku],
                    "description": (f"Events {', '.join(ids)} record the same {sku} quantity "
                                    f"({_fmt_qty(qty)}) at the same moment ({ts}) under different ids. "
                                    "They may be a meter double-send or two genuine requests."),
                    "options": [
                        {"label": "count_all", "description": "Count every event."},
                        {"label": "exclude_repeats", "description": f"Count only {ids[0]}."},
                    ],
                })

    def scenarios(self) -> list[dict]:
        if not self.ambiguities:
            return [{}]
        ids = [a["id"] for a in self.ambiguities]
        opts = [[o["label"] for o in a["options"]] for a in self.ambiguities]
        total = 1
        for o in opts:
            total *= len(o)
        if total <= MAX_FULL_SCENARIOS:
            return [dict(zip(ids, combo)) for combo in itertools.product(*opts)]
        base = {aid: o[0] for aid, o in zip(ids, opts)}
        out = [base]
        for aid, o in zip(ids, opts):
            for label in o[1:]:
                out.append({**base, aid: label})
        return out

    # ---------------------------------------------------------------- pricing one scenario
    def _billable(self, sku: str, choices: dict) -> list[dict]:
        evs = list(self.base.get(sku, []))
        drop: set[str] = set()
        for a in self.ambiguities:
            if sku not in a["skus"]:
                continue
            choice = choices.get(a["id"])
            if a["type"] == "possible_duplicate" and choice == "exclude_repeats":
                drop.update(a["event_ids"][1:])
            if a["type"] == "disputed_usage" and choice == "not_billable":
                drop.update(a["event_ids"])
        return [e for e in evs if e["event_id"] not in drop]

    def _snapshot_counts(self, evs: list[dict]) -> list[Decimal]:
        ordered = sorted(evs, key=lambda e: e["timestamp"])
        counts = []
        for i in range(self.days):
            day = self.start + timedelta(days=i)
            current = ZERO
            for e in ordered:
                if _edate(e) <= day:
                    current = D(e["quantity"])
            counts.append(current)
        return counts

    def price(self, choices: dict) -> dict[str, dict]:
        lines: dict[str, dict] = {}
        for r in self.rules:
            t = r["type"]
            if t not in ("flat", "per_unit", "tiered", "included_quota"):
                continue
            sku = r["sku"]
            evs = self._billable(sku, choices)
            qty = None
            if t == "flat":
                amt = money(r["amount"])
                work = f"flat fee under {r['id']}: {_fmt_money(amt)}"
            elif t == "per_unit" and r["measure"] == "snapshot":
                counts = self._snapshot_counts(evs)
                price = D(r["unit_price"])
                if r["proration"] == "daily":
                    unit_days = sum(counts, ZERO)
                    amt = money(unit_days * price / self.days)
                    qty = unit_days / self.days
                    segs = []
                    for c, grp in itertools.groupby(enumerate(counts), key=lambda x: x[1]):
                        g = list(grp)
                        segs.append(f"{_fmt_qty(c)} for {len(g)} day(s)")
                    work = (f"daily proration over {self.days} days ({', '.join(segs)}) = "
                            f"{_fmt_qty(unit_days)} unit-days ÷ {self.days} × {n2s(price)} = {_fmt_money(amt)}")
                else:
                    peak = max(counts) if counts else ZERO
                    qty = peak
                    amt = money(peak * price)
                    work = f"peak count {_fmt_qty(peak)} × {n2s(price)} = {_fmt_money(amt)}"
            else:
                measure = r.get("measure", "sum")
                if measure == "max":
                    qty = max((D(e["quantity"]) for e in evs), default=ZERO)
                else:
                    qty = sum((D(e["quantity"]) for e in evs), ZERO)
                if t == "per_unit":
                    amt = money(qty * D(r["unit_price"]))
                    work = f"{_fmt_qty(qty)} × {n2s(r['unit_price'])} = {_fmt_money(amt)}"
                elif t == "tiered":
                    mode = r.get("tier_mode") or next(
                        (choices[a["id"]] for a in self.ambiguities
                         if a["type"] == "tier_mode" and r["id"] in a["rule_ids"]), "graduated")
                    amt, work = price_tiered(qty, r["tiers"], mode)
                    work = f"{_fmt_qty(qty)} billable units; {work}"
                else:  # included_quota: the billable quantity is the overage
                    over = max(ZERO, qty - D(r["included"]))
                    amt = money(over * D(r["overage_unit_price"]))
                    label = "peak" if measure == "max" else "total"
                    work = (f"{label} {_fmt_qty(qty)} − {_fmt_qty(r['included'])} included = "
                            f"{_fmt_qty(over)} over × {n2s(r['overage_unit_price'])} = {_fmt_money(amt)}")
                    qty = over
            lines[sku] = {"key": sku, "rule_ids": [r["id"]], "amount": amt, "label": _label(r),
                          "quantity": None if qty is None else n2s(money(qty) if qty != qty.to_integral_value() else qty),
                          "workings": work}
        charges_subtotal = sum((ln["amount"] for ln in lines.values()), ZERO)
        for r in self.rules:
            if r["type"] != "discount_percent":
                continue
            base = sum((lines[s]["amount"] for s in r["applies_to"] if s in lines), ZERO)
            amt = -money(base * D(r["percent"]) / 100)
            lines[r["sku"]] = {"key": r["sku"], "rule_ids": [r["id"]], "amount": amt, "quantity": None,
                               "label": _label(r), "workings": f"{n2s(r['percent'])}% of {_fmt_money(base)} "
                                           f"({', '.join(r['applies_to'])}) = {_fmt_money(amt)}"}
        for r in self.rules:
            if r["type"] != "minimum_commit":
                continue
            commit = money(r["amount"])
            gap = commit - charges_subtotal
            if gap > 0:
                lines[r["sku"]] = {"key": r["sku"], "rule_ids": [r["id"]], "amount": gap, "quantity": None,
                                   "label": _label(r),
                                   "workings": f"minimum commitment {_fmt_money(commit)} − charges "
                                               f"{_fmt_money(charges_subtotal)} = true-up {_fmt_money(gap)}"}
            else:
                lines.setdefault(r["sku"], {"key": r["sku"], "rule_ids": [r["id"]], "amount": ZERO, "label": _label(r),
                                            "quantity": None, "workings": f"charges {_fmt_money(charges_subtotal)}"
                                            f" meet the {_fmt_money(commit)} minimum; no true-up"})
        return lines

    # ---------------------------------------------------------------- causes
    def _quantity_cause(self, sku: str, inv_qty: Decimal, billable_qty: Decimal) -> str | None:
        if inv_qty == billable_qty or self._measure(sku) != "sum":
            return None
        dup_qty = ZERO
        dup_ids = []
        seen = set()
        for e in self.raw_by_sku.get(sku, []):
            if e["event_id"] in seen:
                dup_qty += D(e["quantity"])
                dup_ids.append(e["event_id"])
            seen.add(e["event_id"])
        oop = self.out_of_period.get(sku, [])
        oop_qty = sum((D(e["quantity"]) for e in oop), ZERO)
        exc = self.excluded.get(sku, [])
        exc_qty = sum((D(e["quantity"]) for e in exc), ZERO)
        parts = []
        if dup_qty:
            parts.append(f"{_fmt_qty(dup_qty)} from repeated event id(s) {', '.join(dup_ids)}")
        if oop_qty:
            parts.append(f"{_fmt_qty(oop_qty)} from event(s) outside the billing period "
                         f"({', '.join(e['event_id'] for e in oop)})")
        if exc_qty:
            parts.append(f"{_fmt_qty(exc_qty)} from usage the contract excludes "
                         f"({', '.join(e['event_id'] for e in exc[:8])}{'…' if len(exc) > 8 else ''})")
        explained = billable_qty + dup_qty + oop_qty + exc_qty
        msg = f"Invoice bills {_fmt_qty(inv_qty)} {sku} units; the billable metered quantity is {_fmt_qty(billable_qty)}."
        if parts:
            msg += " The difference matches " if explained == inv_qty else " Possible contributors: "
            msg += "; ".join(parts) + "."
        return msg

    # ---------------------------------------------------------------- run
    def run(self) -> dict:
        scenario_choices = self.scenarios()
        priced = [self.price(c) for c in scenario_choices]
        inv_groups: dict[str, dict] = {}
        for ln in self.invoice["lines"]:
            g = inv_groups.setdefault(ln["sku"], {"line_ids": [], "amount": ZERO, "quantity": None,
                                                  "unit_price": ln.get("unit_price")})
            g["line_ids"].append(ln["line_id"])
            g["amount"] += D(ln["amount"])
            if ln.get("quantity") is not None:
                g["quantity"] = (g["quantity"] or ZERO) + D(ln["quantity"])

        lines_sum = sum((D(ln["amount"]) for ln in self.invoice["lines"]), ZERO)
        stated_total = D(self.invoice["stated_total"]) if self.invoice.get("stated_total") else None
        invoiced_total = stated_total if stated_total is not None else lines_sum

        keys = list(inv_groups)
        for p in priced:
            for k, ln in p.items():
                if k not in keys and (ln["amount"] != 0 or k in inv_groups):
                    keys.append(k)

        discrepancies: list[dict] = []
        line_rows: list[dict] = []
        amb_by_id = {a["id"]: a for a in self.ambiguities}
        for key in keys:
            g = inv_groups.get(key)
            inv_amt = g["amount"] if g else ZERO
            line_ids = g["line_ids"] if g else []
            in_contract = any(key in p for p in priced)
            base_line = priced[0].get(key)
            rule_ids = base_line["rule_ids"] if base_line else []
            causes: list[str] = []
            if g:
                for ln in self.invoice["lines"]:
                    if ln["sku"] == key and ln.get("quantity") and ln.get("unit_price"):
                        calc = money(D(ln["quantity"]) * D(ln["unit_price"]))
                        if calc != D(ln["amount"]):
                            causes.append(f"Line {ln['line_id']} does not add up on its own face: "
                                          f"{_fmt_qty(ln['quantity'])} × {n2s(ln['unit_price'])} = "
                                          f"{_fmt_money(calc)}, but the line shows {_fmt_money(ln['amount'])}.")
                rule = self.rule_by_sku.get(key)
                if rule and g.get("unit_price"):
                    contract_price = rule.get("unit_price") or rule.get("overage_unit_price")
                    if contract_price and D(contract_price) != D(g["unit_price"]):
                        causes.append(f"Invoice unit price {n2s(g['unit_price'])} differs from the contract "
                                      f"price {n2s(contract_price)} in {rule['id']}.")
                if base_line and g.get("quantity") is not None and base_line.get("quantity") is not None:
                    qc = self._quantity_cause(key, g["quantity"], D(base_line["quantity"]))
                    if qc:
                        causes.append(qc)
            elif base_line:
                causes.append(f"The contract requires a {key} line ({base_line['workings']}), "
                              "but the invoice has none.")

            event_ids: list[str] = []
            for note in self.notes:
                if note.get("event_ids") and any(
                        e.get("sku") == key for e in self.raw_by_sku.get(key, []) if e["event_id"] in note["event_ids"]):
                    event_ids.extend(note["event_ids"])

            if not in_contract:
                d = {
                    "id": _hid("D", key, "needs_evidence"), "kind": "needs_evidence", "key": key,
                    "line_ids": line_ids, "rule_ids": [], "event_ids": [], "ambiguity_ids": [],
                    "amount": m2s(inv_amt), "direction": "unsupported",
                    "description": (f"{key} is billed at {_fmt_money(inv_amt)} but no contract rule prices it. "
                                    "A contract amendment or order form is needed to confirm it."),
                    "causes": causes,
                }
                discrepancies.append(d)
                line_rows.append({"key": key, "line_ids": line_ids, "invoiced": m2s(inv_amt), "status": "unsupported",
                                  "rule_ids": [], "discrepancy_ids": [d["id"]]})
                continue

            deltas = [inv_amt - (p[key]["amount"] if key in p else ZERO) for p in priced]
            row_disc: list[str] = []
            relevant_ambs = self._relevant(key, scenario_choices, deltas)
            if len(set(deltas)) == 1:
                delta = deltas[0]
                if delta != 0:
                    d = self._calc_disc(key, delta, line_ids, rule_ids, event_ids, causes, base_line, g)
                    discrepancies.append(d)
                    row_disc.append(d["id"])
            else:
                if all(x > 0 for x in deltas):
                    confirmed = min(deltas)
                elif all(x < 0 for x in deltas):
                    confirmed = max(deltas)
                else:
                    confirmed = ZERO
                if confirmed != 0:
                    d = self._calc_disc(key, confirmed, line_ids, rule_ids, event_ids, causes, base_line, g,
                                        partial=True)
                    discrepancies.append(d)
                    row_disc.append(d["id"])
                outcomes, seen = [], set()
                for ch, dl in zip(scenario_choices, deltas):
                    sub = {a: ch[a] for a in relevant_ambs}
                    sig = json.dumps(sub, sort_keys=True)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    outcomes.append({"choices": sub, "delta": m2s(dl - confirmed)})
                amb_rules = sorted({r for a in relevant_ambs for r in amb_by_id[a]["rule_ids"]})
                amb_events = [e for a in relevant_ambs for e in amb_by_id[a]["event_ids"]]
                vals = [D(o["delta"]) for o in outcomes]
                d = {
                    "id": _hid("D", key, "interpretation"), "kind": "interpretation_dependent", "key": key,
                    "line_ids": line_ids, "rule_ids": sorted(set(rule_ids) | set(amb_rules)),
                    "event_ids": amb_events[:30], "ambiguity_ids": relevant_ambs,
                    "amount": "0.00", "range": [m2s(min(vals)), m2s(max(vals))],
                    "direction": "mixed" if min(vals) < 0 < max(vals) else ("overcharge" if max(vals) > 0 else "undercharge"),
                    "description": (f"The {key} charge depends on how {len(relevant_ambs)} contract or data "
                                    f"question(s) are answered; beyond any confirmed error the difference ranges "
                                    f"from {_fmt_money(min(vals))} to {_fmt_money(max(vals))}."),
                    "causes": causes,
                    "outcomes": outcomes,
                }
                discrepancies.append(d)
                row_disc.append(d["id"])
            status = "match"
            if row_disc:
                kinds = {x["kind"] for x in discrepancies if x["id"] in row_disc}
                if "interpretation_dependent" in kinds:
                    status = "interpretation"
                elif not g:
                    status = "missing_on_invoice"
                else:
                    status = "overcharge" if deltas[0] > 0 else "undercharge"
            line_rows.append({"key": key, "line_ids": line_ids, "invoiced": m2s(inv_amt), "status": status,
                              "rule_ids": rule_ids, "discrepancy_ids": row_disc})

        if stated_total is not None and stated_total != lines_sum:
            delta = stated_total - lines_sum
            discrepancies.append({
                "id": _hid("D", "__total__", "calculation_error"), "kind": "calculation_error",
                "key": "__invoice_total__", "line_ids": [ln["line_id"] for ln in self.invoice["lines"]],
                "rule_ids": [], "event_ids": [], "ambiguity_ids": [], "amount": m2s(delta),
                "direction": "overcharge" if delta > 0 else "undercharge",
                "description": (f"The invoice total {_fmt_money(stated_total)} does not equal the sum of its "
                                f"lines {_fmt_money(lines_sum)}."),
                "causes": [],
            })

        credits = self._allocate_prior_credits(discrepancies, inv_groups)
        payments_total = sum((D(p["amount"]) for p in self.payments if p["type"] == "payment"
                              and p["invoice_id"] in ("", self.invoice["invoice_id"])), ZERO)
        refunds_total = sum((D(p["amount"]) for p in self.payments if p["type"] == "refund"
                             and p["invoice_id"] in ("", self.invoice["invoice_id"])), ZERO)
        confirmed = [D(d["amount"]) for d in discrepancies if d["kind"] == "calculation_error"]
        confirmed_net = sum(confirmed, ZERO)

        scenarios_out = []
        for i, (ch, p) in enumerate(zip(scenario_choices, priced)):
            total = sum((ln["amount"] for ln in p.values()), ZERO)
            if stated_total is not None and stated_total != lines_sum:
                pass  # expected total is purely from the contract
            scenarios_out.append({
                "id": f"S{i + 1}", "choices": ch, "total": m2s(total),
                "lines": {k: {"amount": m2s(v["amount"]), "workings": v["workings"], "rule_ids": v["rule_ids"],
                              "label": v.get("label", k),
                              "quantity": v["quantity"]} for k, v in p.items()},
            })

        return {
            "engine_version": ENGINE_VERSION,
            "currency": self.invoice.get("currency") or self.contract.get("currency") or "USD",
            "invoice_id": self.invoice["invoice_id"],
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days},
            "invoice_lines": self.invoice["lines"],
            "line_comparison": line_rows,
            "discrepancies": discrepancies,
            "ambiguities": self.ambiguities,
            "scenarios": scenarios_out,
            "data_quality": self.notes,
            "unsupported_rules": [{"id": r["id"], "text": r["text"]} for r in self.rules if r["type"] == "clause"],
            "prior_credits": credits,
            "totals": {
                "invoiced_total": m2s(invoiced_total),
                "lines_sum": m2s(lines_sum),
                "payments_total": m2s(payments_total),
                "refunds_total": m2s(refunds_total),
                "prior_credits_total": credits["total"],
                "unattributed_prior_credits": credits["unattributed"],
                "balance_due": m2s(invoiced_total - payments_total + refunds_total - D(credits["total"])),
                "confirmed_net_difference": m2s(confirmed_net),
                "confirmed_overcharge": m2s(sum((x for x in confirmed if x > 0), ZERO)),
                "confirmed_undercharge": m2s(sum((x for x in confirmed if x < 0), ZERO)),
                "recalculated_total_confirmed": m2s(invoiced_total - confirmed_net),
                "credit_ceiling": m2s(max(ZERO, invoiced_total - D(credits["total"]))),
            },
            "warnings": ([] if (self.invoice.get("currency") or "USD") == (self.contract.get("currency") or "USD")
                         else [f"Invoice currency {self.invoice.get('currency')} differs from contract currency "
                               f"{self.contract.get('currency')}; amounts are compared without conversion."]),
        }

    def _relevant(self, key: str, choices: list[dict], deltas: list[Decimal]) -> list[str]:
        relevant = []
        for a in self.ambiguities:
            aid = a["id"]
            for i, ci in enumerate(choices):
                hit = False
                for j, cj in enumerate(choices):
                    if ci[aid] != cj[aid] and all(ci[o] == cj[o] for o in ci if o != aid) and deltas[i] != deltas[j]:
                        hit = True
                        break
                if hit:
                    relevant.append(aid)
                    break
        return relevant

    def _calc_disc(self, key, delta, line_ids, rule_ids, event_ids, causes, base_line, g, partial=False) -> dict:
        direction = "overcharge" if delta > 0 else "undercharge"
        if g is None:
            desc = (f"The invoice omits the {key} line required by {', '.join(rule_ids)}; "
                    f"the correct amount is {_fmt_money(-delta)}.")
        else:
            desc = (f"{key} is invoiced at {_fmt_money(g['amount'])}; the contract gives "
                    f"{_fmt_money(g['amount'] - delta)}"
                    + (" under every reading of the open contract questions" if partial else "")
                    + f" ({direction} of {_fmt_money(abs(delta))}).")
        return {
            "id": _hid("D", key, "calculation_error"), "kind": "calculation_error", "key": key,
            "line_ids": line_ids, "rule_ids": rule_ids, "event_ids": sorted(set(event_ids))[:30],
            "ambiguity_ids": [], "amount": m2s(delta), "direction": direction,
            "description": desc, "causes": causes,
            "workings": base_line["workings"] if base_line else "",
            "confirmed_under_all_readings": partial,
        }

    def _allocate_prior_credits(self, discrepancies: list[dict], inv_groups: dict) -> dict:
        line_to_key = {lid: k for k, g in inv_groups.items() for lid in g["line_ids"]}
        allocations: list[dict] = []
        unattributed = ZERO
        total = ZERO
        capacity: dict[str, Decimal] = {}
        for d in discrepancies:
            d["prior_credit_applied"] = "0.00"
            d["prior_credit_ids"] = []
            if d["kind"] == "calculation_error":
                capacity[d["id"]] = max(ZERO, D(d["amount"]))
            elif d["kind"] == "interpretation_dependent":
                capacity[d["id"]] = max(ZERO, D(d["range"][1]))
        for p in self.payments:
            if p["type"] not in CREDIT_TYPES:
                continue
            if p["invoice_id"] and p["invoice_id"] != self.invoice["invoice_id"]:
                continue
            amt = D(p["amount"])
            total += amt
            keys = {line_to_key[lid] for lid in p["applies_to_lines"] if lid in line_to_key}
            remaining = amt
            targets = sorted([d for d in discrepancies if d["key"] in keys and d["id"] in capacity],
                             key=lambda d: d["kind"] != "calculation_error")
            for d in targets:
                take = min(remaining, capacity[d["id"]])
                if take <= 0:
                    continue
                capacity[d["id"]] -= take
                remaining -= take
                d["prior_credit_applied"] = m2s(D(d["prior_credit_applied"]) + take)
                d["prior_credit_ids"].append(p["id"])
                allocations.append({"credit_id": p["id"], "discrepancy_id": d["id"], "amount": m2s(take)})
            if remaining > 0:
                unattributed += remaining
        return {"total": m2s(total), "unattributed": m2s(unattributed), "allocations": allocations}


def calculate(invoice: dict | None, contract: dict | None, usage: list[dict] | None,
              payments: list[dict] | None) -> dict:
    return _Engine(invoice, contract, usage or [], payments or []).run()


# --------------------------------------------------------------------------- selection helpers
def select_scenario(calc: dict, choices: dict | None) -> dict:
    """The scenario matching the given readings. Unspecified readings resolve to the
    highest-total (provider-favourable) scenario, so nothing is credited by default."""
    choices = {k: v for k, v in (choices or {}).items() if v}
    matches = [s for s in calc["scenarios"] if all(s["choices"].get(k) == v for k, v in choices.items())]
    if not matches:
        matches = calc["scenarios"]
    return max(matches, key=lambda s: D(s["total"]))


def compare(calc: dict, choices: dict | None) -> dict:
    """Original vs recalculated invoice, line by line, for one set of readings."""
    scen = select_scenario(calc, choices)
    inv_by_key: dict[str, list[dict]] = defaultdict(list)
    for ln in calc["invoice_lines"]:
        inv_by_key[ln["sku"]].append(ln)
    rows = []
    keys = list(dict.fromkeys(list(inv_by_key) + list(scen["lines"])))
    for key in keys:
        lines = inv_by_key.get(key, [])
        inv_amt = sum((D(ln["amount"]) for ln in lines), ZERO)
        exp = scen["lines"].get(key)
        if exp is None and not lines:
            continue
        if exp is not None and D(exp["amount"]) == 0 and not lines:
            continue
        exp_amt = D(exp["amount"]) if exp else None
        rows.append({
            "key": key,
            "line_ids": [ln["line_id"] for ln in lines],
            "description": (lines[0]["description"] or key) if lines else (exp or {}).get("label", key),
            "on_invoice": bool(lines),
            "invoiced_quantity": lines[0].get("quantity") if len(lines) == 1 else None,
            "invoiced": m2s(inv_amt),
            "recalculated": None if exp_amt is None else m2s(exp_amt),
            "recalculated_quantity": exp.get("quantity") if exp else None,
            "difference": None if exp_amt is None else m2s(inv_amt - exp_amt),
            "workings": exp["workings"] if exp else "No contract rule prices this charge.",
            "rule_ids": exp["rule_ids"] if exp else [],
        })
    inv_total = D(calc["totals"]["invoiced_total"])
    priced_total = D(scen["total"])
    unsupported = sum((D(r["invoiced"]) for r in rows if r["recalculated"] is None), ZERO)
    return {
        "scenario_id": scen["id"],
        "choices": scen["choices"],
        "rows": rows,
        "invoiced_total": m2s(inv_total),
        "recalculated_total": m2s(priced_total + unsupported),
        "unsupported_total": m2s(unsupported),
        "difference": m2s(inv_total - priced_total - unsupported),
        "currency": calc["currency"],
    }
