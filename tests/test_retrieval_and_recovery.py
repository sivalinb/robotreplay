import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from robotreplay.agent import Investigator
from robotreplay.provider import Provider
from robotreplay.store import Store, uid
from robotreplay.telemetry import Telemetry
from robotreplay.worker import Worker
from tests.conftest import ready_clip


@pytest.mark.parametrize("malformed", [False, True])
def test_hybrid_protocol_scopes_exports_and_retains_failed_reservations(settings, malformed):
    settings = replace(
        settings,
        embedding_model="test-embedding",
        embedding_base_url="https://embed.example/v1",
        embedding_usd_per_million=1,
        model_budget_usd=1,
    )
    store = Store(settings.data_dir)
    cid, eid = ready_clip(store)
    ready_clip(store, team="team-b", text="Private other-team observation.")
    store.add_evidence(cid, "team-a", 5, 5, "human_observation", "A private mentor note.", "human")
    calls = []

    def respond(request):
        inputs = json.loads(request.content)["input"]
        calls.append(inputs)
        assert str(request.url).endswith("/embeddings")
        assert len(inputs) == 2 and "[email removed]" in inputs[0]
        assert all("private" not in text.lower() for text in inputs)
        if malformed:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0, 0]}]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 1, "embedding": [0.8, 0.2]},
                ],
                "usage": {"prompt_tokens": 40},
            },
        )

    telemetry = Telemetry(store)
    try:
        provider = Provider(settings, store, telemetry, httpx.MockTransport(respond))
        result = asyncio.run(
            Investigator(store, telemetry, provider).ask(
                "team-a",
                [cid],
                "Compare the visible green marker for coach@example.com",
                "hybrid",
            )
        )
        assert len(calls) == 1
        assert eid in {e["id"] for e in result["evidence"]}
        row = store.one("SELECT * FROM ledger")
        if malformed:
            assert result["strategy"] == "fts5_fallback:embedding_unavailable"
            assert row["status"] == "reserved"
        else:
            assert result["strategy"] == "hybrid_rrf"
            assert row["status"] == "settled" and row["charged"] == 0.00004
        assert "coach@example.com" not in str(store.rows("SELECT * FROM spans"))
    finally:
        telemetry.close()


def test_worker_restart_recovers_and_claims_once(settings):
    store = Store(settings.data_dir)
    cid, _ = ready_clip(store)
    store.execute("UPDATE clips SET status='processing' WHERE id=?", (cid,))
    job_id = uid()
    store.execute(
        "INSERT INTO jobs(id,clip_id,status,queued,started,attempts) VALUES(?,?,?,?,?,?)",
        (job_id, cid, "running", time.time() - 5, time.time() - 4, 1),
    )
    telemetry = Telemetry(store)
    try:
        worker = Worker(store, telemetry, settings)
        worker.recover()
        assert store.clip(cid, "team-a")["status"] == "queued"
        assert worker.claim()["id"] == job_id
        assert worker.claim() is None
        assert store.one("SELECT attempts FROM jobs")["attempts"] == 2
    finally:
        telemetry.close()
