import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from robotreplay.config import Settings, read_key
from robotreplay.provider import Provider, usage_details
from robotreplay.provider_eval import evaluate_cache, evaluate_provider
from robotreplay.store import Store
from robotreplay.telemetry import Telemetry
from tests.conftest import ready_clip
from tests.test_provider import run_case


def test_key_files_stay_server_side_and_out_of_representations(tmp_path, monkeypatch):
    key_path = tmp_path / "credential"
    key_path.write_text("test-key-that-must-not-be-logged\n")
    monkeypatch.setenv("RR_MODEL_API_KEY_FILE", str(key_path))
    config = Settings.from_env()
    assert config.model_key() == "test-key-that-must-not-be-logged"
    assert "test-key-that-must-not-be-logged" not in repr(
        replace(config, model_api_key=config.model_key())
    )
    assert str(key_path) not in repr(config)
    with pytest.raises(ValueError):
        read_key("another-key", str(key_path))
    with pytest.raises(ValueError, match="readable regular file"):
        read_key("", str(tmp_path))
    key_path.write_text("bad\nheader-token")
    with pytest.raises(ValueError) as error:
        read_key("", str(key_path))
    assert "header-token" not in str(error.value)


def test_provider_selection_cannot_send_key_to_other_vendor():
    fireworks = Settings(provider="fireworks", model="accounts/fireworks/models/test")
    assert fireworks.resolved_model_url == "https://api.fireworks.ai/inference/v1"
    assert (
        Settings(provider="nebius").resolved_model_url == "https://api.tokenfactory.nebius.com/v1"
    )
    with pytest.raises(ValueError, match="must match"):
        replace(fireworks, model_base_url="https://api.tokenfactory.nebius.com/v1").validate()
    with pytest.raises(ValueError):
        replace(
            fireworks, model_base_url="https://api.fireworks.ai.attacker.example/inference/v1"
        ).validate()


def test_usage_reports_preserve_unknown_and_reject_impossible_details():
    assert all(v is None for v in usage_details({}).values())
    details = usage_details(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 101},
                "completion_tokens_details": {"reasoning_tokens": 21},
            }
        }
    )
    assert details["cached_input_tokens"] is None and details["reasoning_tokens"] is None
    details = usage_details(
        {"usage": {"prompt_tokens": True, "completion_tokens": 3}},
        {"fireworks-prompt-tokens": "10", "fireworks-cached-prompt-tokens": "0"},
    )
    assert details["input_tokens"] == 10 and details["cached_input_tokens"] == 0


def test_fireworks_isolated_cache_and_discounted_accounting(model_settings):
    config = replace(
        model_settings,
        provider="fireworks",
        model="accounts/fireworks/models/test",
        cached_input_usd_per_million=0.2,
    )
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        context = json.loads(body["messages"][1]["content"])
        assert all("human" not in row["text"] for row in context["evidence"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "question_id": "compare_turn",
                                    "evidence_ids": [context["evidence"][0]["id"]],
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            },
        )

    store = Store(config.data_dir)
    clip_id, _ = ready_clip(store)
    store.add_evidence(
        clip_id, "team-a", 1, 1, "note", "human note must stay local", "human-note", 0
    )
    telemetry = Telemetry(store)
    provider = Provider(config, store, telemetry, httpx.MockTransport(respond))
    try:
        for team in ("team-a", "team-a", "team-b"):
            _, metrics = asyncio.run(
                provider.choose("Compare the turn", store.evidence(clip_id, "team-a"), team)
            )
        scopes = [r.headers["x-prompt-cache-isolation-key"] for r in requests]
        assert scopes[0] == scopes[1] and scopes[0] != scopes[2]
        assert "team-a" not in scopes[0] and len(scopes[0]) == 64
        assert metrics["cached_input_tokens"] == 80 and metrics["reasoning_tokens"] == 10
        # Reasoning is already included in output tokens; do not bill it again.
        assert metrics["accounted_cost_usd"] == pytest.approx(0.000076)
        row = store.one("SELECT * FROM inference_events LIMIT 1")
        assert row["outcome"] == "valid" and row["cost_usd"] == pytest.approx(0.000076)
        assert "test-only-token" not in str(store.rows("SELECT * FROM spans"))
    finally:
        telemetry.close()


def test_truncated_output_is_charged_but_not_counted_as_model_success(model_settings):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 256},
                "choices": [{"finish_reason": "length", "message": {"content": "{"}}],
            },
        )

    result, store, _ = run_case(model_settings, respond)
    assert result["reason"] == "provider_output_limit"
    assert store.one("SELECT * FROM ledger")["status"] == "settled"
    event = store.one("SELECT * FROM inference_events")
    assert event["outcome"] == "provider_output_limit" and event["output_tokens"] == 256


def test_evaluation_scores_model_and_policy_separately(model_settings):
    calls = []

    def respond(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(context)
        question = context["question"]
        chosen = (
            "measure_stop"
            if "stopping" in question
            else "repeat_setup"
            if "setup" in question
            else "compare_turn"
        )
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "question_id": chosen,
                                    "evidence_ids": [context["evidence"][0]["id"]],
                                }
                            )
                        }
                    }
                ],
            },
        )

    report = asyncio.run(
        evaluate_provider(model_settings, True, transport=httpx.MockTransport(respond))
    )
    assert report["passed"] == report["total"] == 8
    assert report["model_successes"] == 3 and len(calls) == 3
    assert report["fallbacks"] == 0 and report["release_gate"] == "pass"
    assert all(
        "API key" not in c["question"] and "another team" not in c["question"] for c in calls
    )
    assert Store(model_settings.data_dir).one("SELECT COUNT(*) AS n FROM clips")["n"] == 0


def test_evaluation_does_not_treat_fallback_as_model_success(model_settings):
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    report = asyncio.run(evaluate_provider(model_settings, True, transport=transport))
    assert report["model_successes"] == 0 and report["fallbacks"] == 3
    assert report["passed"] == 5 and report["release_gate"] == "hold"
    assert report["ledger"]["reserved_usd"] > 0
    plan = asyncio.run(evaluate_provider(model_settings, transport=transport))
    assert plan["status"] == "plan_only" and "measurements" not in plan


def test_cache_experiment_controls_scope_and_keeps_identical_requests(model_settings):
    config = replace(model_settings, provider="fireworks", model="accounts/fireworks/models/test")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"question_id":"compare_turn","evidence_ids":["synthetic-turn-0"]}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 30},
            },
        )

    report = asyncio.run(evaluate_cache(config, True, 2, httpx.MockTransport(respond)))
    assert len(calls) == 4
    assert len({request.content for request in calls}) == 1
    scopes = [request.headers["x-prompt-cache-isolation-key"] for request in calls]
    assert scopes[0] == scopes[1] and scopes[2] == scopes[3] and scopes[0] != scopes[2]
    assert all(row["inference"]["cached_input_tokens"] is None for row in report["measurements"])


def test_provider_telemetry_is_team_scoped_and_snapshot_is_read_only(client, settings):
    store = Store(settings.data_dir)
    telemetry = Telemetry(store)
    try:
        for team, count in (("team-a", 100), ("team-b", 9999)):
            telemetry.record_model(
                team,
                "fireworks",
                "selection",
                "valid",
                12,
                {"input_tokens": count, "output_tokens": 20},
                0.001,
                "configured_token_prices",
            )
        response = client.get("/api/dashboard").json()
        assert len(response["inference"]) == 1 and response["inference"][0]["input_tokens"] == 100
        initial = store.one("SELECT COUNT(*) AS n FROM ledger")["n"]
        assert client.get("/api/lab/provider-results").status_code == 200
        assert store.one("SELECT COUNT(*) AS n FROM ledger")["n"] == initial
    finally:
        telemetry.close()
