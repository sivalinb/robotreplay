from concurrent.futures import ThreadPoolExecutor

import pytest

from robotreplay.evaluations import run_evaluations
from robotreplay.policy import ModelSelection
from robotreplay.store import Store
from tests.conftest import login, ready_clip


def test_policy_suite_is_not_a_mocked_result():
    result = run_evaluations()
    assert result["total"] == 12
    assert result["passed"] == result["total"]


def test_sessions_csrf_origin_and_host(client):
    client.cookies.clear()
    assert client.get("/api/clips").status_code == 401
    login(client)
    token = client.headers.pop("x-csrf-token")
    assert client.post("/api/evaluations").status_code == 403
    client.headers["x-csrf-token"] = token
    assert (
        client.post("/api/evaluations", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.get("/api/clips", headers={"Host": "evil.example"}).status_code == 400


def test_passwords_and_sessions_are_not_stored_in_plaintext(client):
    store = client.app.state.store
    assert (
        "test-password-robot"
        not in store.one("SELECT password FROM users WHERE username='coach'")["password"]
    )
    raw = client.cookies.get("rr_session")
    assert raw not in str(store.rows("SELECT * FROM sessions"))
    cookie = client.post(
        "/api/login", json={"username": "coach", "password": "test-password-robot"}
    ).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_cross_team_all_read_and_write_paths(client):
    cid, _ = ready_clip(client.app.state.store, team="team-b")
    for path in (f"/api/clips/{cid}", f"/api/clips/{cid}/video"):
        assert client.get(path).status_code == 404
    assert (
        client.post(
            "/api/questions", json={"clip_ids": [cid], "question": "Compare turns"}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/clips/{cid}/annotations", json={"t": 1, "text": "note"}).status_code
        == 404
    )
    assert client.delete(f"/api/clips/{cid}").status_code == 404
    assert client.get("/api/clips").json() == []


def test_annotation_is_plain_data_and_not_an_instruction(client):
    cid, _ = ready_clip(client.app.state.store)
    payload = "<script>alert('x')</script>"
    assert (
        client.post(f"/api/clips/{cid}/annotations", json={"t": 1, "text": payload}).status_code
        == 201
    )
    assert (
        client.post(f"/api/clips/{cid}/annotations", json={"t": 999, "text": "late"}).status_code
        == 422
    )
    answer = client.post("/api/questions", json={"clip_ids": [cid], "question": "script"}).json()
    assert all(e["text"] != payload for e in answer["evidence"])
    assert "script-src 'self'" in client.get("/").headers["content-security-policy"]


def test_delete_removes_evidence_search_index_and_media(client):
    store = client.app.state.store
    cid, _ = ready_clip(store)
    directory = store.media_dir(cid)
    directory.mkdir()
    (directory / "source.bin").write_bytes(b"private-media")
    result = client.delete(f"/api/clips/{cid}")
    assert result.status_code == 200 and result.json()["media_removed"]
    assert not directory.exists()
    assert not store.rows("SELECT * FROM evidence WHERE clip_id=?", (cid,))
    assert not store.rows("SELECT * FROM evidence_fts WHERE clip_id=?", (cid,))
    assert client.get(f"/api/clips/{cid}").status_code == 404


def test_atomic_budget_reservations_cannot_oversubscribe(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.reserve("a", 0.3, 1), range(10)))
    assert sum(r is not None for r in results) == 3


@pytest.mark.parametrize(
    "value",
    [
        {"question_id": "compare_turn", "evidence_ids": ["missing"]},
        {"question_id": "write_notebook", "evidence_ids": ["known"]},
        {"question_id": "compare_turn", "evidence_ids": ["known"], "extra": "<script>"},
    ],
)
def test_model_output_cannot_expand_authority(value):
    with pytest.raises(ValueError):
        ModelSelection.model_validate(value).verified({"known"})


def test_metrics_require_a_dedicated_token_and_exclude_ids(client):
    assert client.get("/metrics").status_code == 401
    secret = (client.app.state.store.root / "metrics-token").read_text()
    result = client.get("/metrics", headers={"Authorization": "Bearer " + secret})
    assert result.status_code == 200
    assert "rr_requests_total" in result.text
    assert "team-a" not in result.text and "rr_session" not in result.text
