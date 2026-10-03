import pytest
from decimal import Decimal

from app.agent.grounding import allowed_numbers, build_reference_index, ground, unverified_numbers
from app.engine.resolution import compute_option
from conftest import calc_sample, load_sample


def _ids(calc):
    return {d["key"]: d["id"] for d in calc["discrepancies"]}


def test_option_deducts_prior_credit_and_prior_adjustments():
    calc = calc_sample("globex-seat-proration")
    ids = _ids(calc)
    seats = compute_option(calc, [ids["SEATS"]], {})
    assert seats["amount"] == "120.00" and seats["kind"] == "credit"
    again = compute_option(calc, [ids["SEATS"]], {}, {ids["SEATS"]: Decimal("120.00")}, Decimal("120.00"))
    assert again["amount"] == "0.00"
    net = compute_option(calc, [ids["SEATS"], ids["MIN_COMMIT_TRUEUP"]], {})
    assert net["amount"] == "-80.00" and net["kind"] == "debit"


def test_option_interpretation_choices_and_conservative_default():
    calc = calc_sample("initech-maintenance-tiers")
    d = calc["discrepancies"][0]["id"]
    amb = {a["type"]: a["id"] for a in calc["ambiguities"]}
    assert compute_option(calc, [d], {})["amount"] == "-100.00"
    vol = compute_option(calc, [d], {amb["tier_mode"]: "volume"})
    assert vol["amount"] == "150.00" and vol["items"][0]["note"].startswith("No reading chosen")
    bad = compute_option(calc, [d, "D-NOPE"], {amb["tier_mode"]: "flat-rate", "A-NOPE": "x"})
    assert bad["unknown_discrepancies"] == ["D-NOPE"] and len(bad["warnings"]) == 3


def test_credit_is_capped_at_invoice_net_of_credits():
    calc = calc_sample("acme-api-overage")
    ids = list(_ids(calc).values())
    capped = compute_option(calc, ids, {}, {}, Decimal("2900.00"))
    assert capped["amount"] == "94.00" and capped["capped"]


def _bundle(key):
    s = load_sample(key)
    return {"invoice": s["invoice"], "contract": s["contract"], "usage": s["usage"]["events"],
            "payments": s.get("payments", {}).get("entries", []), "payments_available": "payments" in s,
            "notes": []}


def test_grounding_strips_fake_citations_recategorizes_and_flags_numbers():
    calc = calc_sample("acme-api-overage")
    bundle = _bundle("acme-api-overage")
    index = build_reference_index(bundle, calc)
    allowed = allowed_numbers(calc, bundle["invoice"], bundle["contract"])
    ids = _ids(calc)
    output = {
        "case_summary": "Acme was overcharged by $412.00 and also $999.99.",
        "findings": [
            {"title": "Duplicate event", "category": "contract_interpretation", "confidence": "high",
             "explanation": "EV-1012 was billed twice, an overcharge of $184.00 on line L2.",
             "discrepancy_ids": [ids["API_CALLS"]], "citations": ["EV-1012", "INV-LINE-7"]},
            {"title": "Storage is wrong", "category": "calculation_error", "confidence": "low",
             "explanation": "Storage should cost $12.34.", "discrepancy_ids": [], "citations": ["R3"]},
            {"title": "Made up", "category": "data_quality", "explanation": "Nothing to see.",
             "citations": ["X-1"]},
        ],
        "resolution_options": [{"title": "Credit", "action": "credit", "rationale": "Fix it.", "amount": "5000.00",
                                "discrepancy_ids": [ids["API_CALLS"], ids["DISCOUNT_API_LOYALTY"]]}],
        "missing_evidence": [{"evidence_kind": "usage", "reason": "Raw gateway logs", "blocking": False},
                             {"evidence_kind": "spaceship", "reason": "x"}],
    }
    g = ground(output, index, calc, allowed, "llm")
    f1, f2, f3 = g["findings"]
    assert f1["category"] == "calculation_error" and f1["invalid_citations"] == ["INV-LINE-7"]
    assert {"L2", "R2"} <= {c["ref"] for c in f1["citations"] if c["origin"] == "engine"}
    assert f1["unverified_numbers"] == []  # 184.00 is the engine's own figure
    assert f2["category"] == "needs_evidence" and f2["unverified_numbers"] == ["$12.34"]
    assert f3["grounded"] is False
    assert g["resolution_options"][0]["computation"]["amount"] == "412.00"  # the model's 5000.00 is ignored
    assert [m["evidence_kind"] for m in g["missing_evidence"]] == ["usage"]
    assert any("999.99" in n for n in g["report"]["notes"])


def test_unverified_number_regex_ignores_ids_and_dates():
    allowed = {Decimal("412.00")}
    assert unverified_numbers("Invoice INV-2026-08 on 2026-09-01 owes $412.00 for EV-1012", allowed) == []
    assert unverified_numbers("credit 1,234.56 now", allowed) == ["1,234.56"]


def test_provider_wiring_without_network():
    from app.agent.llm import AnthropicClient, GEMINI_OPENAI_BASE_URL, LLMError, OpenAICompatibleClient, get_client
    from app.config import Settings

    g = get_client(Settings(llm_provider="gemini", llm_api_key="k", llm_model=""))
    assert isinstance(g, OpenAICompatibleClient) and g.provider == "gemini"
    assert g.model == "gemini-3.8-flash" and g.url == GEMINI_OPENAI_BASE_URL + "/chat/completions"
    assert isinstance(get_client(Settings(llm_provider="anthropic", llm_api_key="k")), AnthropicClient)
    o = get_client(Settings(llm_provider="openai", llm_api_key="k", llm_base_url="https://api.groq.com/openai/v1",
                            llm_model="llama-3.3-70b-versatile"))
    assert o.provider == "openai" and o.url == "https://api.groq.com/openai/v1/chat/completions"
    with pytest.raises(LLMError):
        get_client(Settings(llm_provider="gemini", llm_api_key=""))
