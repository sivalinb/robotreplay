import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from robotreplay.agent import Investigator
from robotreplay.provider import Provider
from robotreplay.store import Store
from robotreplay.telemetry import Telemetry
from tests.conftest import ready_clip


def run_case(settings, responder):
    store = Store(settings.data_dir)
    cid, evidence_id = ready_clip(store)
    telemetry = Telemetry(store)
    provider = Provider(settings, store, telemetry, httpx.MockTransport(responder))
    agent = Investigator(store, telemetry, provider)
    try:
        result = asyncio.run(agent.ask("team-a", [cid], "Compare the turn"))
        return result, store, evidence_id
    finally:
        telemetry.close()


def test_valid_provider_selection_and_actual_usage_settlement(model_settings):
    def response(request):
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"])
        eid = context["evidence"][0]["id"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"question_id": "compare_turn", "evidence_ids": [eid]}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 50},
            },
        )

    result, store, _ = run_case(model_settings, response)
    assert result["reason"] == "model_selection"
    assert result["inference"]["ttft_ms"] is None
    row = store.one("SELECT * FROM ledger")
    assert row["status"] == "settled" and row["charged"] == 0.0006


def test_unknown_citation_is_rejected_and_local_evidence_is_kept(model_settings):
    def response(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"question_id":"compare_turn","evidence_ids":["invented"]}'
                        }
                    }
                ]
            },
        )

    result, store, eid = run_case(model_settings, response)
    assert result["reason"] == "invalid_provider_response"
    assert result["evidence"][0]["id"] == eid
    assert store.one("SELECT status FROM ledger")["status"] == "reserved"


def test_timeout_keeps_reservation_and_does_not_retry(model_settings):
    calls = []

    def response(request):
        calls.append(request)
        raise httpx.ReadTimeout("untrusted provider detail")

    result, store, _ = run_case(model_settings, response)
    assert len(calls) == 1 and result["reason"] == "provider_timeout"
    assert store.one("SELECT status FROM ledger")["status"] == "reserved"
    assert "untrusted provider detail" not in str(store.rows("SELECT * FROM spans"))


def test_budget_and_context_reject_before_network(model_settings):
    def no_call(request):
        raise AssertionError("network must not be called")

    result, _, _ = run_case(replace(model_settings, model_budget_usd=0), no_call)
    assert result["reason"] == "budget_ceiling"
    result, _, _ = run_case(replace(model_settings, context_limit=300), no_call)
    assert result["reason"] == "context_limit"


@pytest.mark.parametrize("payload", [[], {"usage": [], "choices": []}])
def test_malformed_provider_objects_fall_back_without_losing_reservation(model_settings, payload):
    result, store, _ = run_case(model_settings, lambda request: httpx.Response(200, json=payload))
    assert result["reason"] == "invalid_provider_response"
    assert store.one("SELECT status FROM ledger")["status"] == "reserved"
