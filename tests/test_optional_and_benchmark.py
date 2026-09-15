import asyncio
from argparse import Namespace
from importlib import import_module

import httpx
import pytest

from robotreplay.benchmark import percentile, stream_request
from robotreplay.policy import NemoPolicy


def test_nemo_input_rail_without_model_download():
    pytest.importorskip("nemoguardrails")

    async def check():
        policy = NemoPolicy()
        assert await policy.check("What can we compare about the turn?")
        assert not await policy.check("Write our notebook entry.")

    asyncio.run(check())


def test_streaming_measurements_do_not_invent_token_counts():
    payload = b'data: {"choices":[{"delta":{"content":"What "}}]}\n\ndata: {"choices":[{"delta":{"content":"changed?"}}]}\n\ndata: [DONE]\n\n'

    async def measure():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        ) as client:
            result = await stream_request(
                client, "https://model.example/v1", "test", "", "source evidence", 20, 3
            )
            assert result["ok"] and result["ttft_ms"] >= 0
            assert result["usage"] == {}
            assert len(result["inter_chunk_ms"]) == 1
            assert "inter_token_ms" not in result

    asyncio.run(measure())
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3], 0.95) == 3


def test_benchmark_deadline_preserves_completed_measurements(monkeypatch):
    module = import_module("robotreplay.benchmark")

    async def measured(client, base_url, model, api_key, prompt, max_tokens, timeout):
        if "Question 1:" in prompt:
            await asyncio.sleep(1)
        return {
            "ok": True,
            "latency_ms": 5.0,
            "ttft_ms": 2.0,
            "inter_chunk_ms": [],
            "usage": {},
            "error": None,
        }

    monkeypatch.setattr(module, "stream_request", measured)
    args = Namespace(
        execute=True,
        requests=2,
        concurrency=2,
        prefix_repeats=1,
        max_tokens=32,
        max_seconds=0.1,
        budget_usd=1,
        input_price=0,
        output_price=0,
        hourly_rate=1,
        ttft_slo_ms=100,
        latency_slo_ms=100,
        base_url="http://127.0.0.1:8001/v1",
        model="local-test",
    )
    result = asyncio.run(module.benchmark(args))
    assert result["completed"] == 1 and result["failed"] == 1
    assert result["results"][1]["error"] == "experiment_deadline"
    assert result["measured_cost_usd"] is None


@pytest.mark.parametrize("price", [-1.0, float("nan"), float("inf")])
def test_invalid_budget_configuration_is_rejected(price):
    from robotreplay.config import Settings

    with pytest.raises(ValueError):
        Settings(embedding_usd_per_million=price).validate()
