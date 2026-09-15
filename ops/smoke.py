"""Exercise the disposable CI Compose stack with generated media only."""

import json
import time
from pathlib import Path

import httpx


def eventually(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError, AssertionError):
            pass
        time.sleep(1)
    raise AssertionError("Infrastructure check did not become ready before its deadline")


def main():
    checks = {}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5, trust_env=False) as app:
        eventually(lambda: app.get("/healthz").is_success)
        login = app.post(
            "/api/login",
            json={
                "username": "ci-mentor",
                "password": "ci-only-generated-media",
            },
        )
        login.raise_for_status()
        app.headers["x-csrf-token"] = login.json()["csrf"]
        admitted = app.post("/api/demo", json={"obscured": True})
        admitted.raise_for_status()
        cid = admitted.json()["id"]

        def ready():
            clip = app.get("/api/clips/" + cid).json()
            return clip if clip["clip"]["status"] == "ready" else None

        clip = eventually(ready)
        assert 0.7 < clip["clip"]["coverage"] < 0.9
        assert any(e["kind"] == "visibility_gap" for e in clip["evidence"])
        video = app.get("/api/clips/" + cid + "/video", headers={"Range": "bytes=0-99"})
        assert video.status_code == 206 and len(video.content) == 100
        checks["container_media_pipeline"] = "pass"
        answer = app.post(
            "/api/questions",
            json={
                "clip_ids": [cid],
                "question": "Compare the visible green marker",
            },
        ).json()
        assert answer["status"] == "ready" and answer["evidence"]
        trace_id = answer["trace_id"]
        checks["container_investigation"] = "pass"
    with httpx.Client(timeout=5, trust_env=False) as client:

        def scrapes():
            body = client.get("http://127.0.0.1:9090/api/v1/targets").json()
            targets = {t["labels"]["job"]: t["health"] for t in body["data"]["activeTargets"]}
            return targets.get("robotreplay") == "up" and targets.get("otelcol") == "up"

        eventually(scrapes)
        checks["authenticated_metrics_and_collector_scrape"] = "pass"
        grafana = "http://127.0.0.1:3000"
        eventually(lambda: client.get(grafana + "/api/health").json().get("database") == "ok")
        # These are disposable loopback CI credentials, never deployed credentials.
        client.auth = ("admin", "local-lab-change-me")
        eventually(lambda: client.get(grafana + "/api/dashboards/uid/robotreplay").is_success)
        checks["grafana_provisioning"] = "pass"

        def exported_trace():
            response = client.get(
                grafana + "/api/datasources/proxy/uid/tempo/api/traces/" + trace_id
            )
            return response.is_success and bool(response.json().get("batches"))

        eventually(exported_trace)
        checks["otlp_trace_to_tempo"] = "pass"

        def exported_logs():
            response = client.get(
                grafana + "/api/datasources/proxy/uid/loki/loki/api/v1/query_range",
                params={"query": '{service_name="robotreplay"}', "limit": 5},
            )
            return response.is_success and bool(response.json()["data"]["result"])

        eventually(exported_logs)
        checks["sanitized_logs_to_loki"] = "pass"
    target = Path("artifacts/infrastructure-smoke.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks))


if __name__ == "__main__":
    main()
