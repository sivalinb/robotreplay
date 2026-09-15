import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from robotreplay.app import create_app
from robotreplay.config import Settings
from robotreplay.store import Store, create_user, uid


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, worker_poll=0.02)


@pytest.fixture
def client(settings):
    store = Store(settings.data_dir)
    create_user(store, "coach", "test-password-robot", "team-a")
    create_user(store, "other", "test-password-other", "team-b")
    with TestClient(create_app(settings)) as connection:
        login(connection)
        yield connection


def login(client, username="coach", password="test-password-robot"):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf"]


def ready_clip(
    store,
    team="team-a",
    text="The visible green marker changes direction in the image.",
    kind="turn_candidate",
    origin="green-marker-v1",
):
    clip_id = uid()
    store.execute(
        """INSERT INTO clips(id,team,title,sha256,permission,source,created,
      expires,status,duration,fps,width,height,coverage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            clip_id,
            team,
            "Test drill",
            uid(),
            "original_generated",
            "test",
            time.time(),
            time.time() + 86400,
            "ready",
            12,
            15,
            640,
            360,
            1,
        ),
    )
    evidence_id = store.add_evidence(clip_id, team, 4.2, 4.2, kind, text, origin, 0.2)
    return clip_id, evidence_id


@pytest.fixture
def model_settings(settings):
    return replace(
        settings,
        provider="nebius",
        model="test-model",
        model_api_key="test-only-token",
        model_budget_usd=1,
        input_usd_per_million=1,
        output_usd_per_million=2,
    )
