"""End-to-end reviewer workflow through the HTTP API (background runs execute inline)."""
import json
import threading

import pytest

from app.agent import orchestrator
from app.agent.llm import MockClient


def _sample_case(client, key="acme-api-overage"):
    r = client.post(f"/api/samples/{key}/cases")
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _analyze(client, case_id, simulate=None):
    r = client.post(f"/api/cases/{case_id}/analyze", json={"simulate_failures": simulate or []})
    assert r.status_code == 202, r.text
    run = client.get(f"/api/runs/{r.json()['id']}").json()
    assert run["status"] in ("completed", "completed_with_warnings"), run
    return run, client.get(f"/api/cases/{case_id}").json()


def _accept_all(client, detail):
    for f in detail["latest_analysis"]["findings"]:
        if "ungrounded" not in f["flags"]:
            r = client.patch(f"/api/findings/{f['id']}", json={"action": "accept"})
            assert r.status_code == 200, r.text


def _credit_option(detail):
    return next(o for o in detail["latest_analysis"]["options"] if o["action"] == "credit")


def test_auth_is_required(client):
    anon = client.__class__(client.app)
    assert anon.get("/api/cases").status_code == 401
    assert anon.post("/api/auth/login", json={"username": "reviewer", "password": "nope"}).status_code == 401
    assert anon.get("/api/health").json()["llm"]["provider"] == "mock"


def test_full_workflow_and_duplicate_credit_prevention(client):
    cid = _sample_case(client)
    run, detail = _analyze(client, cid)
    assert [s["name"] for s in run["steps"]] == ["collect_evidence", "check_missing_evidence", "recalculate_invoice",
                                                 "ai_analysis", "ground_and_validate", "price_resolution_options",
                                                 "save_analysis"]
    a = detail["latest_analysis"]
    assert a["source"] == "llm" and a["findings"] and not a["stale"]
    assert all(f["citations"] for f in a["findings"])
    opt = _credit_option(detail)
    assert opt["live"]["amount"] == "412.00"

    # Human approval gate: findings must be accepted first.
    r = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Approved per review"},
                    headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "findings_not_accepted"
    _accept_all(client, detail)
    r = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "x"},
                    headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 422  # note too short
    r = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Approved per review"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_key_required"

    r1 = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Approved per review"},
                     headers={"Idempotency-Key": "k-1"})
    assert r1.status_code == 201 and r1.json()["amount"] == "412.00" and r1.json()["replayed"] is False
    r2 = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Approved per review"},
                     headers={"Idempotency-Key": "k-1"})
    assert r2.json()["id"] == r1.json()["id"] and r2.json()["replayed"] is True
    r3 = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Clicked again"},
                     headers={"Idempotency-Key": "k-2"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "already_adjusted"

    cmp = client.get(f"/api/cases/{cid}/comparison").json()
    assert cmp["recalculated_total"] == "2582.00" and cmp["net_after_credits"] == "2582.00"
    assert cmp["remaining_difference"] == "0.00"

    detail = client.get(f"/api/cases/{cid}").json()
    assert _credit_option(detail)["live"]["amount"] == "0.00"
    r = client.post(f"/api/cases/{cid}/resolve", json={"summary": "Credited duplicate usage and missing discount."})
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    types = [e["type"] for e in client.get(f"/api/cases/{cid}/events").json()]
    for t in ("case_created", "evidence_added", "analysis_started", "analysis_completed", "finding_accept",
              "adjustment_approved", "status_changed"):
        assert t in types


def test_new_evidence_reopens_case_and_marks_conclusions_stale(client):
    cid = _sample_case(client, "initech-maintenance-tiers")
    _, detail = _analyze(client, cid)
    assert any(m["evidence_kind"] == "payments" for m in detail["latest_analysis"]["missing_evidence"])
    # Reviewer requests the missing payment history.
    r = client.post(f"/api/cases/{cid}/info-requests", json={"evidence_kind": "payments",
                                                             "message": "Please export the payment ledger"})
    assert r.status_code == 201
    assert client.get(f"/api/cases/{cid}").json()["case"]["status"] == "awaiting_info"
    for f in detail["latest_analysis"]["findings"]:
        client.patch(f"/api/findings/{f['id']}", json={"action": "reject", "note": "Not needed"})
    client.post(f"/api/info-requests/{r.json()['id']}/cancel")
    assert client.post(f"/api/cases/{cid}/resolve", json={"summary": "Closed pending customer reply"}).status_code == 200

    r = client.post(f"/api/cases/{cid}/sample-followups/0")
    assert r.status_code == 201
    detail = client.get(f"/api/cases/{cid}").json()
    assert detail["case"]["status"] == "reopened"
    a = detail["latest_analysis"]
    assert a["stale"] and a["changed_kinds"] == ["payments"]
    stale = [f for f in a["findings"] if f["stale_reasons"]]
    fresh = [f for f in a["findings"] if not f["stale_reasons"]]
    assert stale, "findings built on the calculation depend on payments"
    assert all("payments" in f["stale_reasons"] for f in stale)
    r = client.patch(f"/api/findings/{stale[0]['id']}", json={"action": "accept"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_finding"
    events = [e["type"] for e in client.get(f"/api/cases/{cid}/events").json()]
    assert "case_reopened" in events and "analysis_stale" in events

    _, detail = _analyze(client, cid)
    assert not detail["latest_analysis"]["stale"] and detail["case"]["status"] == "in_review"
    assert len(detail["analyses"]) == 2 and detail["analyses"][1]["stale"]
    del fresh


def test_stale_analysis_blocks_credit_approval(client):
    cid = _sample_case(client)
    _, detail = _analyze(client, cid)
    _accept_all(client, detail)
    opt = _credit_option(detail)
    note = "Customer sent a corrected gateway log summary."
    r = client.post(f"/api/cases/{cid}/evidence", json={"kind": "note", "filename": "call.txt", "content": note})
    assert r.status_code == 201
    r = client.post(f"/api/cases/{cid}/adjustments", json={"option_id": opt["id"], "note": "Approve"},
                    headers={"Idempotency-Key": "stale-1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_analysis"


def test_partial_tool_failures_degrade_gracefully(client):
    cid = _sample_case(client, "globex-seat-proration")
    run, detail = _analyze(client, cid, ["llm", "payments_lookup"])
    assert run["status"] == "completed_with_warnings"
    steps = {s["name"]: s for s in run["steps"]}
    assert steps["collect_evidence"]["status"] == "partial"
    assert steps["ai_analysis"]["status"] == "failed" and "simulated" in steps["ai_analysis"]["error"]
    assert steps["fallback_analysis"]["status"] == "ok"
    a = detail["latest_analysis"]
    assert a["source"] == "fallback"
    payments_gap = [m for m in a["missing_evidence"] if m["evidence_kind"] == "payments"]
    assert payments_gap and "ledger failure" in payments_gap[0]["reason"]
    # Without the ledger the $80 prior credit is invisible, so the seat option shows the full 200.00 —
    # the UI and missing-evidence warning make that risk explicit.
    seat_opt = next(o for o in a["options"] if o["action"] == "credit")
    assert seat_opt["live"]["amount"] == "200.00"
    _accept_all(client, detail)
    body = {"option_id": seat_opt["id"], "note": "Seat proration error confirmed"}
    r = client.post(f"/api/cases/{cid}/adjustments", json=body, headers={"Idempotency-Key": "pf-1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "payments_unverified"
    r = client.post(f"/api/cases/{cid}/adjustments", json={**body, "acknowledge_unverified_payments": True,
                                                            "amount": "50.00"}, headers={"Idempotency-Key": "pf-1"})
    assert r.status_code == 201 and r.json()["amount"] == "50.00" and r.json()["computed_amount"] == "200.00"

    # A partial (overridden) credit leaves the remainder available, never more.
    detail = client.get(f"/api/cases/{cid}").json()
    seat_opt = next(o for o in detail["latest_analysis"]["options"] if o["id"] == seat_opt["id"])
    assert seat_opt["live"]["amount"] == "150.00"

    run, detail = _analyze(client, cid, ["calculator"])
    assert {s["name"]: s["status"] for s in run["steps"]}["recalculate_invoice"] == "failed"
    a = detail["latest_analysis"]
    assert a["calculation_id"] is None
    assert all(o["action"] in ("request_information", "no_adjustment") for o in a["options"])


def test_prior_credit_from_ledger_prevents_double_credit(client):
    cid = _sample_case(client, "globex-seat-proration")
    _, detail = _analyze(client, cid)
    seats = next(d for d in detail["calculation"]["discrepancies"] if d["key"] == "SEATS")
    opt = next(o for o in detail["latest_analysis"]["options"] if seats["id"] in o["discrepancy_ids"]
               and o["action"] == "credit")
    assert opt["live"]["amount"] == "120.00"


def test_model_bad_json_is_repaired_and_hallucinations_are_caught(client, monkeypatch):
    replies = iter(["Sure! Here is my analysis: {not json", None])

    def handler(system, messages):
        nxt = next(replies)
        if nxt is not None:
            return nxt
        ctx = json.loads(messages[0]["content"].split("<case_context>")[1].split("</case_context>")[0])
        d = {x["key"]: x["id"] for x in ctx["calculation"]["discrepancies"]}
        return json.dumps({
            "case_summary": "The API line is overcharged.", "claim_assessment": "Partly valid.",
            "findings": [
                {"title": "API overcharge", "category": "calculation_error", "confidence": "high",
                 "explanation": "Duplicate EV-1012 inflated line L2.", "discrepancy_ids": [d["API_CALLS"]],
                 "citations": ["L2", "EV-1012", "EV-9999"]},
                {"title": "Invented", "category": "calculation_error", "confidence": "high",
                 "explanation": "Customer owes $77.77 more.", "citations": ["NOPE"]}],
            "missing_evidence": [],
            "resolution_options": [{"title": "Credit API only", "action": "credit", "rationale": "Clear error.",
                                    "discrepancy_ids": [d["API_CALLS"]], "citations": ["L2"]}]})

    monkeypatch.setattr(orchestrator, "get_client", lambda: MockClient(handler))
    cid = _sample_case(client)
    run, detail = _analyze(client, cid)
    ai = next(s for s in run["steps"] if s["name"] == "ai_analysis")
    assert ai["status"] == "partial" and "repaired" in ai["error"]
    f1, f2 = detail["latest_analysis"]["findings"]
    assert f1["invalid_citations"] == ["EV-9999"]
    assert "ungrounded" in f2["flags"] and f2["unverified_numbers"] == ["$77.77"]
    r = client.patch(f"/api/findings/{f2['id']}", json={"action": "accept"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ungrounded"
    assert detail["latest_analysis"]["options"][0]["live"]["amount"] == "184.00"
    raw = client.get(f"/api/analyses/{detail['latest_analysis']['id']}/raw").text
    assert "API overcharge" in raw


def test_finding_edit_preserves_original(client):
    cid = _sample_case(client)
    _, detail = _analyze(client, cid)
    f = detail["latest_analysis"]["findings"][0]
    r = client.patch(f"/api/findings/{f['id']}", json={"action": "edit", "note": "Clarified wording",
                                                       "explanation": "Reviewer wording."})
    edited = next(x for x in r.json()["findings"] if x["id"] == f["id"])
    assert edited["status"] == "edited" and edited["original"]["explanation"] == f["explanation"]
    assert client.patch(f"/api/findings/{f['id']}", json={"action": "reject"}).status_code == 422


def test_evidence_validation_and_duplicates(client):
    r = client.post("/api/cases", json={"title": "", "customer_name": ""})
    assert r.status_code == 422 and len(r.json()["error"]["details"]) == 2
    cid = client.post("/api/cases", json={"title": "Manual", "customer_name": "Hooli"}).json()["id"]
    r = client.post(f"/api/cases/{cid}/evidence", json={"kind": "invoice", "content": '{"invoice_id": 1'})
    assert r.status_code == 422 and r.json()["error"]["code"] == "evidence_invalid"
    assert client.post(f"/api/cases/{cid}/analyze", json={}).status_code == 422
    body = {"kind": "note", "filename": "a.txt", "content": "Customer called."}
    assert client.post(f"/api/cases/{cid}/evidence", json=body).status_code == 201
    assert client.post(f"/api/cases/{cid}/evidence", json=body).status_code == 409
    run, detail = _analyze(client, cid)
    assert detail["latest_analysis"]["calculation_id"] is None
    kinds = {m["evidence_kind"] for m in detail["latest_analysis"]["missing_evidence"] if m["blocking"]}
    assert {"invoice", "contract"} <= kinds


def test_concurrent_approvals_create_one_adjustment(client, fresh_db):
    from app.services import adjustments as svc
    cid = _sample_case(client)
    _, detail = _analyze(client, cid)
    _accept_all(client, detail)
    opt = _credit_option(detail)
    results, errors = [], []

    def worker(i):
        db = fresh_db()
        try:
            adj, _ = svc.approve(db, cid, opt["id"], f"race-{i}", "Concurrent approval", "reviewer")
            db.commit()
            results.append(adj.id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors.append(getattr(exc, "code", type(exc).__name__))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1, (results, errors)
    assert len(client.get(f"/api/cases/{cid}").json()["adjustments"]) == 1


def test_one_run_at_a_time(client, fresh_db):
    cid = _sample_case(client)
    db = fresh_db()
    orchestrator.start_run(db, cid, "reviewer")
    db.commit()
    db.close()
    r = client.post(f"/api/cases/{cid}/analyze", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_in_progress"


@pytest.mark.parametrize("bad", [["meteor"]])
def test_unknown_failure_simulation_rejected(client, bad):
    cid = _sample_case(client)
    assert client.post(f"/api/cases/{cid}/analyze", json={"simulate_failures": bad}).status_code == 422
