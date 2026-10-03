"""Parse and validate uploaded evidence into canonical dictionaries.

Structural problems (unparseable JSON, missing invoice lines, an unknown rule type)
reject the upload with field-level errors. Row-level problems in usage events or
payment history keep the valid rows and return warnings, so one bad meter row does
not block an investigation.

Canonical output stores every number as a string; the engine re-reads them as
Decimal. Floats never appear.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..engine.money import D, MoneyError, m2s, n2s

EVIDENCE_KINDS = ("invoice", "contract", "usage", "payments", "dispute", "note")
SINGLE_DOC_KINDS = {"invoice", "contract"}  # a new upload of these supersedes the active one
RULE_TYPES = (
    "flat", "per_unit", "tiered", "included_quota", "discount_percent",
    "minimum_commit", "usage_exclusion", "clause",
)
PAYMENT_TYPES = ("payment", "credit", "adjustment", "refund")
MAX_TEXT_BYTES = 2_000_000


class EvidenceValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ParseResult:
    data: dict
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers
def _load_json(text: str):
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise EvidenceValidationError([f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"])


def _looks_like_json(text: str) -> bool:
    s = text.lstrip()
    return s.startswith("{") or s.startswith("[")


def _date(value, path: str, errors: list[str]) -> str | None:
    if value is None:
        errors.append(f"{path}: required date (YYYY-MM-DD)")
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        errors.append(f"{path}: '{value}' is not a valid date (YYYY-MM-DD)")
        return None


def _timestamp(value, path: str, errors: list[str]) -> str | None:
    if value in (None, ""):
        errors.append(f"{path}: required timestamp (ISO 8601)")
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).isoformat()
    except ValueError:
        errors.append(f"{path}: '{value}' is not an ISO 8601 timestamp")
        return None


def _num(value, path: str, errors: list[str], *, money: bool = False, allow_negative: bool = True,
         required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            errors.append(f"{path}: required number")
        return None
    if isinstance(value, bool):
        errors.append(f"{path}: expected a number, got a boolean")
        return None
    try:
        d = D(value)
    except MoneyError:
        errors.append(f"{path}: '{value}' is not a number")
        return None
    if not allow_negative and d < 0:
        errors.append(f"{path}: must not be negative")
        return None
    return m2s(d) if money else n2s(d)


def _str(value, path: str, errors: list[str], required: bool = True) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            errors.append(f"{path}: required text")
        return None
    return str(value).strip()


def _str_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _csv_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise EvidenceValidationError(["CSV has no header row"])
    return [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()} for row in reader]


# ---------------------------------------------------------------- invoice
def parse_invoice(text: str) -> ParseResult:
    obj = _load_json(text)
    if not isinstance(obj, dict):
        raise EvidenceValidationError(["Invoice must be a JSON object with invoice_id, period and lines"])
    errors: list[str] = []
    warnings: list[str] = []
    invoice_id = _str(obj.get("invoice_id"), "invoice_id", errors)
    period = obj.get("period") or {}
    start = _date(period.get("start"), "period.start", errors)
    end = _date(period.get("end"), "period.end", errors)
    if start and end and start > end:
        errors.append("period: start is after end")
    lines_in = obj.get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        errors.append("lines: at least one invoice line is required")
        lines_in = []
    lines, seen = [], set()
    for i, ln in enumerate(lines_in):
        p = f"lines[{i}]"
        if not isinstance(ln, dict):
            errors.append(f"{p}: must be an object")
            continue
        line_id = _str(ln.get("line_id"), f"{p}.line_id", errors)
        if line_id in seen:
            errors.append(f"{p}.line_id: duplicate line id '{line_id}'")
        seen.add(line_id)
        lines.append({
            "line_id": line_id,
            "sku": _str(ln.get("sku"), f"{p}.sku", errors),
            "description": str(ln.get("description") or ""),
            "quantity": _num(ln.get("quantity"), f"{p}.quantity", errors, required=False),
            "unit_price": _num(ln.get("unit_price"), f"{p}.unit_price", errors, required=False),
            "amount": _num(ln.get("amount"), f"{p}.amount", errors, money=True),
        })
    if obj.get("tax") not in (None, "", 0, "0"):
        warnings.append("Invoice includes tax; tax is outside the scope of this tool and is not recalculated.")
    if errors:
        raise EvidenceValidationError(errors)
    return ParseResult({
        "invoice_id": invoice_id,
        "customer": str(obj.get("customer") or ""),
        "currency": str(obj.get("currency") or "USD").upper(),
        "issued_at": str(obj.get("issued_at") or ""),
        "period": {"start": start, "end": end},
        "lines": lines,
        "stated_total": _num(obj.get("total"), "total", [], money=True, required=False),
        "tax": _num(obj.get("tax"), "tax", [], money=True, required=False),
    }, warnings)


# ---------------------------------------------------------------- contract
def _parse_rule(r: dict, p: str, errors: list[str]) -> dict:
    rid = _str(r.get("id"), f"{p}.id", errors)
    rtype = r.get("type")
    if rtype not in RULE_TYPES:
        errors.append(f"{p}.type: '{rtype}' is not one of {', '.join(RULE_TYPES)}")
        return {"id": rid, "type": rtype}
    rule = {"id": rid, "type": rtype, "text": str(r.get("text") or "")}
    if r.get("label"):
        rule["label"] = str(r["label"])[:80]
    if rtype != "minimum_commit" and rtype != "clause":
        rule["sku"] = _str(r.get("sku"), f"{p}.sku", errors)
    if rtype == "flat":
        rule["amount"] = _num(r.get("amount"), f"{p}.amount", errors, money=True, allow_negative=False)
    elif rtype == "per_unit":
        rule["unit_price"] = _num(r.get("unit_price"), f"{p}.unit_price", errors, allow_negative=False)
        rule["measure"] = r.get("measure", "sum")
        rule["proration"] = r.get("proration", "none")
        if rule["measure"] not in ("sum", "max", "snapshot"):
            errors.append(f"{p}.measure: must be sum, max or snapshot")
        if rule["proration"] not in ("none", "daily"):
            errors.append(f"{p}.proration: must be none or daily")
        if rule["proration"] == "daily" and rule["measure"] != "snapshot":
            errors.append(f"{p}.proration: daily proration requires measure 'snapshot'")
    elif rtype == "tiered":
        tiers_in = r.get("tiers")
        tiers = []
        if not isinstance(tiers_in, list) or not tiers_in:
            errors.append(f"{p}.tiers: at least one tier is required")
            tiers_in = []
        prev = Decimal(0)
        for j, t in enumerate(tiers_in):
            up_to = t.get("up_to") if isinstance(t, dict) else None
            up = None if up_to is None else _num(up_to, f"{p}.tiers[{j}].up_to", errors, allow_negative=False)
            if up is not None and D(up) <= prev:
                errors.append(f"{p}.tiers[{j}].up_to: tiers must increase")
            if up is None and j != len(tiers_in) - 1:
                errors.append(f"{p}.tiers[{j}].up_to: only the last tier may be unbounded")
            if up is not None:
                prev = D(up)
            tiers.append({"up_to": up, "unit_price": _num((t or {}).get("unit_price"),
                          f"{p}.tiers[{j}].unit_price", errors, allow_negative=False)})
        if tiers and tiers[-1]["up_to"] is not None:
            errors.append(f"{p}.tiers: the last tier must have up_to null (unbounded)")
        rule["tiers"] = tiers
        mode = r.get("tier_mode")
        if mode not in (None, "graduated", "volume"):
            errors.append(f"{p}.tier_mode: must be graduated, volume, or omitted")
        rule["tier_mode"] = mode
        rule["measure"] = r.get("measure", "sum")
    elif rtype == "included_quota":
        rule["included"] = _num(r.get("included"), f"{p}.included", errors, allow_negative=False)
        rule["overage_unit_price"] = _num(r.get("overage_unit_price"), f"{p}.overage_unit_price", errors,
                                          allow_negative=False)
        rule["measure"] = r.get("measure", "sum")
        if rule["measure"] not in ("sum", "max"):
            errors.append(f"{p}.measure: must be sum or max")
    elif rtype == "discount_percent":
        rule["percent"] = _num(r.get("percent"), f"{p}.percent", errors, allow_negative=False)
        if rule["percent"] is not None and D(rule["percent"]) > 100:
            errors.append(f"{p}.percent: must be between 0 and 100")
        rule["applies_to"] = _str_list(r.get("applies_to"))
        if not rule["applies_to"]:
            errors.append(f"{p}.applies_to: list the SKUs the discount applies to")
    elif rtype == "minimum_commit":
        rule["amount"] = _num(r.get("amount"), f"{p}.amount", errors, money=True, allow_negative=False)
        rule["sku"] = str(r.get("sku") or "MIN_COMMIT_TRUEUP")
    elif rtype == "usage_exclusion":
        rule["exclude_tags"] = _str_list(r.get("exclude_tags"))
        rule["disputed_tags"] = _str_list(r.get("disputed_tags"))
        if not rule["exclude_tags"] and not rule["disputed_tags"]:
            errors.append(f"{p}: give exclude_tags or disputed_tags")
    elif rtype == "clause":
        if not rule["text"]:
            errors.append(f"{p}.text: a clause needs its contract wording")
    return rule


def parse_contract(text: str) -> ParseResult:
    obj = _load_json(text)
    if not isinstance(obj, dict):
        raise EvidenceValidationError(["Contract must be a JSON object with contract_id and rules"])
    errors: list[str] = []
    rules_in = obj.get("rules")
    if not isinstance(rules_in, list) or not rules_in:
        errors.append("rules: at least one pricing rule is required")
        rules_in = []
    rules, seen = [], set()
    for i, r in enumerate(rules_in):
        if not isinstance(r, dict):
            errors.append(f"rules[{i}]: must be an object")
            continue
        rule = _parse_rule(r, f"rules[{i}]", errors)
        if rule.get("id") in seen:
            errors.append(f"rules[{i}].id: duplicate rule id '{rule.get('id')}'")
        seen.add(rule.get("id"))
        rules.append(rule)
    priced = [r.get("sku") for r in rules if r.get("type") in ("flat", "per_unit", "tiered", "included_quota")]
    dupes = {s for s in priced if priced.count(s) > 1}
    if dupes:
        errors.append(f"rules: more than one pricing rule for SKU {', '.join(sorted(dupes))}")
    if errors:
        raise EvidenceValidationError(errors)
    return ParseResult({
        "contract_id": _str(obj.get("contract_id"), "contract_id", []) or "CONTRACT",
        "customer": str(obj.get("customer") or ""),
        "currency": str(obj.get("currency") or "USD").upper(),
        "rules": rules,
    })


# ---------------------------------------------------------------- usage
def parse_usage(text: str) -> ParseResult:
    if _looks_like_json(text):
        obj = _load_json(text)
        rows = obj.get("events") if isinstance(obj, dict) else obj
        if not isinstance(rows, list):
            raise EvidenceValidationError(["Usage must be a JSON list of events or an object with an 'events' list"])
    else:
        rows = _csv_rows(text)
    events, warnings = [], []
    for i, row in enumerate(rows):
        errs: list[str] = []
        p = f"events[{i}]"
        if not isinstance(row, dict):
            warnings.append(f"{p}: skipped, not an object")
            continue
        ev = {
            "event_id": _str(row.get("event_id"), f"{p}.event_id", errs),
            "timestamp": _timestamp(row.get("timestamp"), f"{p}.timestamp", errs),
            "sku": _str(row.get("sku"), f"{p}.sku", errs),
            "quantity": _num(row.get("quantity"), f"{p}.quantity", errs, allow_negative=False),
            "tags": _str_list(row.get("tags")),
        }
        if errs:
            warnings.append(f"{p} skipped: " + "; ".join(errs))
            continue
        events.append(ev)
    if not events:
        raise EvidenceValidationError(["No valid usage events found"] + warnings[:10])
    return ParseResult({"events": events}, warnings)


# ---------------------------------------------------------------- payments
def parse_payments(text: str) -> ParseResult:
    if _looks_like_json(text):
        obj = _load_json(text)
        rows = obj.get("entries") if isinstance(obj, dict) else obj
        if not isinstance(rows, list):
            raise EvidenceValidationError(["Payments must be a JSON list or an object with an 'entries' list"])
    else:
        rows = _csv_rows(text)
    entries, warnings, seen = [], [], set()
    for i, row in enumerate(rows):
        errs: list[str] = []
        p = f"entries[{i}]"
        if not isinstance(row, dict):
            warnings.append(f"{p}: skipped, not an object")
            continue
        etype = str(row.get("type") or "").strip().lower()
        if etype not in PAYMENT_TYPES:
            errs.append(f"{p}.type: '{row.get('type')}' is not one of {', '.join(PAYMENT_TYPES)}")
        entry = {
            "id": _str(row.get("id"), f"{p}.id", errs),
            "type": etype,
            "date": _date(row.get("date"), f"{p}.date", errs),
            "amount": _num(row.get("amount"), f"{p}.amount", errs, money=True, allow_negative=False),
            "invoice_id": str(row.get("invoice_id") or "").strip(),
            "applies_to_lines": _str_list(row.get("applies_to_lines")),
            "reason": str(row.get("reason") or ""),
        }
        if entry["id"] in seen:
            errs.append(f"{p}.id: duplicate entry id '{entry['id']}'")
        if errs:
            warnings.append(f"{p} skipped: " + "; ".join(errs))
            continue
        seen.add(entry["id"])
        entries.append(entry)
    if not entries and rows:
        raise EvidenceValidationError(["No valid payment or adjustment entries found"] + warnings[:10])
    return ParseResult({"entries": entries}, warnings)


def parse_text(text: str) -> ParseResult:
    body = text.strip()
    if _looks_like_json(body):
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                body = str(obj.get("description") or obj.get("text") or body)
        except json.JSONDecodeError:
            pass
    if not body:
        raise EvidenceValidationError(["Text is empty"])
    return ParseResult({"text": body})


PARSERS = {
    "invoice": parse_invoice,
    "contract": parse_contract,
    "usage": parse_usage,
    "payments": parse_payments,
    "dispute": parse_text,
    "note": parse_text,
}


def parse_evidence(kind: str, text: str) -> ParseResult:
    if kind not in PARSERS:
        raise EvidenceValidationError([f"kind: must be one of {', '.join(EVIDENCE_KINDS)}"])
    if not text or not text.strip():
        raise EvidenceValidationError(["content: file is empty"])
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise EvidenceValidationError(["content: file is larger than 2 MB"])
    return PARSERS[kind](text)
