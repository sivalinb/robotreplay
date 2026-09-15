import time

from tests.conftest import ready_clip


def await_clip(client, cid):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        response = client.get("/api/clips/" + cid)
        state = response.json()["clip"]
        if state["status"] in {"ready", "failed"}:
            return response.json()
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def test_real_video_to_evidence_to_question_and_trace(client):
    queued = client.post("/api/demo", json={"obscured": False})
    assert queued.status_code == 202
    cid = queued.json()["id"]
    detail = await_clip(client, cid)
    assert detail["clip"]["status"] == "ready", detail["clip"]
    assert detail["clip"]["duration"] == 12
    assert detail["clip"]["coverage"] > 0.95
    assert any(e["kind"] == "turn_candidate" and 3.8 < e["t"] < 4.8 for e in detail["evidence"])
    assert len(detail["samples"]) >= 50
    media = client.get(f"/api/clips/{cid}/video", headers={"Range": "bytes=0-99"})
    assert media.status_code == 206 and len(media.content) == 100
    question = client.post(
        "/api/questions", json={"clip_ids": [cid], "question": "Compare the turn"}
    )
    assert question.status_code == 200
    result = question.json()
    assert result["status"] == "ready"
    assert result["inference"]["provider"] == "local"
    assert result["evidence"] and result["tool_calls"] == [
        "get_trial_evidence",
        "get_approved_concept",
    ]
    trace = client.get("/api/traces/" + result["trace_id"]).json()
    assert {"investigate", "tools.get_trial_evidence", "policy.output"} <= {
        s["name"] for s in trace
    }
    assert client.post("/api/demo", json={"obscured": False}).json()["id"] == cid


def test_occlusion_never_becomes_a_stationary_robot_claim(client):
    queued = client.post("/api/demo", json={"obscured": True})
    detail = await_clip(client, queued.json()["id"])
    assert detail["clip"]["status"] == "ready"
    assert 0.7 < detail["clip"]["coverage"] < 0.9
    gaps = [e for e in detail["evidence"] if e["kind"] == "visibility_gap"]
    assert any(4.8 <= e["t"] <= 5.3 and 7 <= e["end_t"] <= 7.5 for e in gaps)
    assert not any(e["kind"] == "stop_candidate" and 5 <= e["t"] <= 7 for e in detail["evidence"])


def test_rights_gate_prevents_admission_and_bad_video_fails_closed(client):
    result = client.post(
        "/api/clips",
        files={"file": ("broken.mp4", b"not video", "video/mp4")},
        data={"title": "Bad", "permission": "", "source": "test"},
    )
    assert result.status_code == 422
    assert not client.get("/api/clips").json()
    result = client.post(
        "/api/clips",
        files={"file": ("broken.mp4", b"not video", "video/mp4")},
        data={"title": "Bad", "permission": "own_recording", "source": "test"},
    )
    assert result.status_code == 202
    detail = await_clip(client, result.json()["id"])
    assert detail["clip"]["status"] == "failed"
    assert client.get(f"/api/clips/{result.json()['id']}/video").status_code == 409


def test_console_time_alignment_and_no_implicit_robot_api(client):
    cid, _ = ready_clip(client.app.state.store)
    result = client.post(
        f"/api/clips/{cid}/logs",
        json={"text": "RR|4000|turn command start", "offset": 0.2, "uncertainty": 0.3},
    )
    assert result.status_code == 201
    evidence = client.get("/api/clips/" + cid).json()["evidence"]
    log = next(e for e in evidence if e["origin"] == "vex_console")
    assert log["t"] == 4.2 and log["uncertainty"] == 0.3
    assert client.post(f"/api/clips/{cid}/logs", json={"text": "RR|40000|late"}).status_code == 422


def test_telemetry_contains_no_question_or_source_text(client):
    cid, _ = ready_clip(client.app.state.store, text="PERSONAL_NOTE_4321", origin="mentor")
    result = client.post(
        "/api/questions", json={"clip_ids": [cid], "question": "PERSONAL_NOTE_4321"}
    )
    assert result.status_code == 200
    serialized = str(client.app.state.store.rows("SELECT * FROM spans"))
    assert "PERSONAL_NOTE_4321" not in serialized
    assert "test-password" not in serialized


def test_empty_visible_evidence_abstains(client):
    cid, _ = ready_clip(
        client.app.state.store, kind="visibility_gap", text="No unique green marker is visible."
    )
    result = client.post(
        "/api/questions", json={"clip_ids": [cid], "question": "Why did it stall?"}
    )
    assert result.json()["status"] == "abstain"
    assert result.json()["evidence"] == []


def test_evaluations_are_saved_and_scoped(client):
    report = client.post("/api/evaluations").json()
    assert report["passed"] == 12
    assert client.get("/api/evaluations").json()[0]["id"] == report["id"]


def test_context_and_kv_memory_estimates(client):
    value = {"input_tokens": 8192, "output_tokens": 256, "context_limit": 8192}
    response = client.post("/api/lab/context", json=value).json()
    assert response["admission"] == "hold"
    half = client.post("/api/lab/context", json={**value, "dtype": "fp8"}).json()
    assert half["kv_gib"] * 2 == response["kv_gib"]
    assert "Analytical estimate" in response["assumptions"]
