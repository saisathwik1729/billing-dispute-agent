"""Deterministic calculation engine: every number here is checked by hand in the README."""
import json
from decimal import Decimal

from app.engine.calculator import calculate, compare, price_tiered, select_scenario
from app.engine.money import m2s, money
from conftest import calc_sample, load_sample

TIERS = [{"up_to": "100000", "unit_price": "0.0100"}, {"up_to": "500000", "unit_price": "0.0080"},
         {"up_to": None, "unit_price": "0.0060"}]


def test_graduated_and_volume_tiers():
    assert price_tiered(Decimal(260000), TIERS, "graduated")[0] == Decimal("2280.00")
    assert price_tiered(Decimal(260000), TIERS, "volume")[0] == Decimal("2080.00")
    assert price_tiered(Decimal(600000), TIERS, "graduated")[0] == Decimal("4800.00")
    assert price_tiered(Decimal(0), TIERS, "graduated")[0] == Decimal("0.00")


def test_money_rounds_half_up_without_floats():
    assert money("2.675") == Decimal("2.68")  # float 2.675 would round to 2.67
    assert m2s("-0.005") == "-0.01"


def test_acme_confirmed_overcharge_and_causes():
    calc = calc_sample("acme-api-overage")
    t = calc["totals"]
    assert t["invoiced_total"] == "2994.00"
    assert t["recalculated_total_confirmed"] == "2582.00"
    assert t["confirmed_overcharge"] == "412.00"
    by_key = {d["key"]: d for d in calc["discrepancies"]}
    api = by_key["API_CALLS"]
    assert api["kind"] == "calculation_error" and api["amount"] == "184.00"
    assert "EV-1012" in api["event_ids"] and "EV-0999" in api["event_ids"]
    assert any("difference matches" in c and "EV-1012" in c and "EV-0999" in c for c in api["causes"])
    disc = by_key["DISCOUNT_API_LOYALTY"]
    assert disc["amount"] == "228.00" and disc["line_ids"] == []
    assert "STORAGE_GB" not in by_key  # peak-based storage charge is correct
    assert calc["ambiguities"] == []


def test_globex_proration_min_commit_and_prior_credit():
    calc = calc_sample("globex-seat-proration")
    by_key = {d["key"]: d for d in calc["discrepancies"]}
    seats = by_key["SEATS"]
    assert seats["amount"] == "200.00"
    assert seats["prior_credit_applied"] == "80.00" and seats["prior_credit_ids"] == ["CR-GLX-0928"]
    trueup = by_key["MIN_COMMIT_TRUEUP"]
    assert trueup["amount"] == "-200.00" and trueup["direction"] == "undercharge"
    assert calc["totals"]["confirmed_net_difference"] == "0.00"
    lines = calc["scenarios"][0]["lines"]
    assert lines["SEATS"]["amount"] == "1000.00"
    assert "20 for 15 day(s), 30 for 15 day(s)" in lines["SEATS"]["workings"]


def test_initech_interpretation_is_separated_from_errors():
    calc = calc_sample("initech-maintenance-tiers")
    assert len(calc["ambiguities"]) == 3 and len(calc["scenarios"]) == 8
    assert {a["type"] for a in calc["ambiguities"]} == {"tier_mode", "disputed_usage", "possible_duplicate"}
    (d,) = calc["discrepancies"]
    assert d["kind"] == "interpretation_dependent"
    assert d["range"] == ["-100.00", "390.00"] and d["direction"] == "mixed"
    assert calc["totals"]["confirmed_net_difference"] == "0.00"
    amb = {a["type"]: a["id"] for a in calc["ambiguities"]}
    favourable = {amb["tier_mode"]: "volume", amb["disputed_usage"]: "not_billable",
                  amb["possible_duplicate"]: "exclude_repeats"}
    cmp = compare(calc, favourable)
    assert cmp["recalculated_total"] == "2560.00" and cmp["difference"] == "390.00"
    assert compare(calc, {amb["tier_mode"]: "volume"})["difference"] == "150.00"
    # Unanswered questions resolve to the provider-favourable (highest) total.
    assert select_scenario(calc, {})["total"] == "3050.00"


def test_confirmed_part_when_every_reading_overcharges():
    s = load_sample("initech-maintenance-tiers")
    inv = json.loads(json.dumps(s["invoice"]))
    inv["lines"][0]["amount"] = "3000.00"
    inv["stated_total"] = "3250.00"
    calc = calculate(inv, s["contract"], s["usage"]["events"], [])
    kinds = {d["kind"]: d for d in calc["discrepancies"]}
    # Highest reading is 2800.00, so 200.00 is owed under every reading.
    assert kinds["calculation_error"]["amount"] == "200.00"
    assert kinds["calculation_error"]["confirmed_under_all_readings"] is True
    assert kinds["interpretation_dependent"]["range"] == ["0.00", "490.00"]


def test_invoice_total_mismatch_and_unsupported_charge():
    s = load_sample("acme-api-overage")
    inv = json.loads(json.dumps(s["invoice"]))
    inv["lines"].append({"line_id": "L9", "sku": "WHITE_GLOVE", "description": "Onboarding", "quantity": None,
                         "unit_price": None, "amount": "150.00"})
    inv["stated_total"] = "3200.00"
    calc = calculate(inv, s["contract"], s["usage"]["events"], [])
    by_key = {d["key"]: d for d in calc["discrepancies"]}
    assert by_key["WHITE_GLOVE"]["kind"] == "needs_evidence"
    assert by_key["__invoice_total__"]["amount"] == "56.00"  # 3200.00 - 3144.00


def test_engine_is_deterministic():
    a, b = calc_sample("initech-maintenance-tiers"), calc_sample("initech-maintenance-tiers")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_snapshot_without_proration_bills_peak():
    contract = {"contract_id": "C", "currency": "USD", "rules": [
        {"id": "R1", "type": "per_unit", "sku": "SEATS", "unit_price": "10", "measure": "snapshot",
         "proration": "none", "text": ""}]}
    invoice = {"invoice_id": "I", "currency": "USD", "period": {"start": "2026-09-01", "end": "2026-09-30"},
               "lines": [{"line_id": "L1", "sku": "SEATS", "description": "", "quantity": "5", "unit_price": "10",
                          "amount": "50.00"}], "stated_total": None}
    usage = [{"event_id": "a", "timestamp": "2026-09-01T00:00:00", "sku": "SEATS", "quantity": "5", "tags": []},
             {"event_id": "b", "timestamp": "2026-09-20T00:00:00", "sku": "SEATS", "quantity": "7", "tags": []}]
    calc = calculate(invoice, contract, usage, [])
    assert calc["discrepancies"][0]["amount"] == "-20.00"
