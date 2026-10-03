import pytest

from app.evidence.parser import EvidenceValidationError, parse_evidence


def test_invalid_json_reports_position():
    with pytest.raises(EvidenceValidationError) as e:
        parse_evidence("invoice", '{"invoice_id": "X",, }')
    assert "line 1" in e.value.errors[0]


def test_invoice_field_errors_are_specific():
    with pytest.raises(EvidenceValidationError) as e:
        parse_evidence("invoice", '{"invoice_id":"X","period":{"start":"2026-09-31","end":"2026-09-01"},'
                                  '"lines":[{"line_id":"L1","sku":"A","amount":"abc"}]}')
    joined = " ".join(e.value.errors)
    assert "period.start" in joined and "lines[0].amount" in joined


def test_money_never_becomes_float():
    r = parse_evidence("invoice", '{"invoice_id":"X","period":{"start":"2026-09-01","end":"2026-09-30"},'
                                  '"lines":[{"line_id":"L1","sku":"A","quantity":3,"unit_price":0.1,"amount":0.3}]}')
    assert r.data["lines"][0]["amount"] == "0.30" and r.data["lines"][0]["unit_price"] == "0.1"


def test_usage_bad_rows_are_skipped_with_warnings():
    csv_text = "event_id,timestamp,sku,quantity,tags\nE1,2026-09-01T00:00:00Z,API,10,\nE2,not-a-date,API,5,\nE3,2026-09-02T00:00:00Z,API,-4,\n"
    r = parse_evidence("usage", csv_text)
    assert [e["event_id"] for e in r.data["events"]] == ["E1"]
    assert len(r.warnings) == 2


def test_contract_rule_validation():
    with pytest.raises(EvidenceValidationError) as e:
        parse_evidence("contract", '{"rules":[{"id":"R1","type":"per_unit","sku":"S","unit_price":"1",'
                                   '"proration":"daily"},{"id":"R2","type":"magic"}]}')
    joined = " ".join(e.value.errors)
    assert "daily proration requires measure 'snapshot'" in joined and "'magic'" in joined


def test_tiers_must_end_unbounded():
    with pytest.raises(EvidenceValidationError):
        parse_evidence("contract", '{"rules":[{"id":"R1","type":"tiered","sku":"S","tiers":[{"up_to":10,"unit_price":"1"}]}]}')
