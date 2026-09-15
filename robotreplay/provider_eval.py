"""A small reproducible application evaluation. Defaults to a plan, not paid traffic."""

import hashlib
import json
import time
from pathlib import Path

from robotreplay.agent import Investigator
from robotreplay.benchmark import percentile
from robotreplay.policy import PROMPTS
from robotreplay.provider import ModelUnavailable, Provider
from robotreplay.store import Store, uid
from robotreplay.telemetry import Telemetry

DATASET = Path(__file__).parent / "data" / "provider-cases.json"


async def evaluate_provider(settings, execute=False, repeats=1, transport=None):
    settings.validate()
    if not 1 <= repeats <= 3:
        raise ValueError("Use 1–3 repetitions of the versioned evaluation suite")
    dataset_bytes = DATASET.read_bytes()
    dataset = json.loads(dataset_bytes)
    report = {
        "schema": "robotreplay-provider-eval-v1",
        "suite": dataset["suite"],
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "provider": settings.provider,
        "model": settings.model if settings.provider != "local" else "teaching-templates-v1",
        "repeats": repeats,
        "created": time.time(),
        "total": len(dataset["cases"]) * repeats,
        "max_external_selection_calls": 3 * repeats if settings.provider != "local" else 0,
        "configured_output_limit": settings.output_tokens,
        "configured_timeout_seconds": settings.model_timeout,
        "configured_input_price": settings.input_usd_per_million,
        "configured_output_price": settings.output_usd_per_million,
        "configured_cached_input_price": settings.cached_input_usd_per_million,
        "response_format": "json_schema" if settings.provider == "fireworks" else "json_object",
        "dataset": "Synthetic teaching, visibility, and policy cases. No team recordings or child records.",
        "limitations": "A tiny application suite, not a general model ranking or safety certification. "
        "Policy-blocked inputs never reach a model. JSON selection does not measure open-ended coaching. "
        "Fallbacks are reported separately and never count as successful model calls. "
        "Non-streaming latency includes network time; TTFT, prefill, decode and GPU use are not measured.",
    }
    if not execute:
        return {
            **report,
            "status": "plan_only",
            "instructions": "Set model, server-side key, prices and budget, then add --execute. "
            "All runs using the same RR_DATA_DIR share one budget ledger.",
        }
    if settings.provider != "local" and (not settings.model or settings.model_budget_usd <= 0):
        raise ValueError(
            "External evaluations require an explicit model and positive shared budget"
        )
    store = Store(settings.data_dir)
    telemetry = Telemetry(store)
    provider = Provider(settings, store, telemetry, transport)
    agent = Investigator(store, telemetry, provider)
    team = "synthetic-lab-" + uid()
    cases, started = [], time.perf_counter()
    try:
        for repetition in range(repeats):
            for case in dataset["cases"]:
                clip_id = uid()
                now = time.time()
                store.execute(
                    """INSERT INTO clips(id,team,title,sha256,permission,source,created,expires,status)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        clip_id,
                        team,
                        "Synthetic evaluation",
                        uid(),
                        "original_generated",
                        "provider-evaluation",
                        now,
                        now + 3600,
                        "ready",
                    ),
                )
                evidence_id = store.add_evidence(
                    clip_id,
                    team,
                    4.2,
                    4.2,
                    case["kind"],
                    case["text"],
                    case.get("origin", "green-marker-v1"),
                    0.2,
                )
                try:
                    with telemetry.span("eval.provider_case", team, **{"rr.case": case["id"]}):
                        result = await agent.ask(team, [clip_id], case["question"])
                    is_ready = case["status"] == "ready"
                    selected_id = next(
                        (key for key, value in PROMPTS.items() if value == result.get("question")),
                        None,
                    )
                    task_correct = result["status"] == case["status"] and (
                        selected_id == case["question_id"]
                        if is_ready
                        else result["reason"] == case["reason"]
                    )
                    cited = result.get("evidence", [])
                    citation_valid = bool(cited) and all(e["id"] == evidence_id for e in cited)
                    if is_ready:
                        task_correct = task_correct and citation_valid
                    model_success = result["reason"] == "model_selection"
                    fallback = is_ready and settings.provider != "local" and not model_success
                    cases.append(
                        {
                            "id": case["id"],
                            "repetition": repetition + 1,
                            "status": result["status"],
                            "reason": result["reason"],
                            "expected_question_id": case.get("question_id"),
                            "selected_question_id": selected_id,
                            "task_correct": task_correct,
                            "model_success": model_success,
                            "fallback": fallback,
                            "citation_valid": citation_valid if is_ready else None,
                            "passed": task_correct and not fallback,
                            "inference": result.get("inference"),
                        }
                    )
                finally:
                    store.execute("DELETE FROM clips WHERE id=? AND team=?", (clip_id, team))
        ledger = store.one(
            """SELECT COUNT(*) AS attempts,COALESCE(SUM(charged),0) AS settled_usd,
            COALESCE(SUM(CASE WHEN status='settled' THEN 0 ELSE reserved END),0) AS reserved_usd
            FROM ledger WHERE team=?""",
            (team,),
        )
        measurements = store.rows(
            "SELECT provider,operation,outcome,duration_ms,input_tokens,output_tokens,"
            "cached_input_tokens,reasoning_tokens,cost_usd,cost_basis,http_status,finish_reason "
            "FROM inference_events WHERE team=? ORDER BY id",
            (team,),
        )
        latencies = [row["duration_ms"] for row in measurements if row["outcome"] == "valid"]
        passed = sum(case["passed"] for case in cases)
        return {
            **report,
            "status": "measured",
            "passed": passed,
            "cases": cases,
            "model_successes": sum(case["model_success"] for case in cases),
            "fallbacks": sum(case["fallback"] for case in cases),
            "release_gate": "pass" if passed == len(cases) else "hold",
            "elapsed_seconds": time.perf_counter() - started,
            "model_completion_p50_ms": percentile(latencies, 0.5),
            "model_completion_p95_ms": percentile(latencies, 0.95),
            "ttft_ms": None,
            "ledger": ledger,
            "measurements": measurements,
            "accounted_plus_reserved_usd": ledger["settled_usd"] + ledger["reserved_usd"],
        }
    finally:
        telemetry.close()


async def evaluate_cache(settings, execute=False, pairs=3, transport=None):
    """Exact-request repeats in fresh Fireworks isolation scopes; no inferred cache hits."""
    settings.validate()
    if settings.provider != "fireworks" or not 1 <= pairs <= 3:
        raise ValueError("Cache isolation experiment requires Fireworks and 1–3 pairs")
    report = {
        "schema": "robotreplay-cache-eval-v1",
        "provider": settings.provider,
        "model": settings.model,
        "created": time.time(),
        "pairs": pairs,
        "max_external_calls": pairs * 2,
        "design": "Each pair uses a fresh cache isolation key, then repeats the exact request with "
        "the same key. The initial request is an isolated baseline, not a claimed zero-cache result.",
        "limitations": "A small paired observation. Provider scheduling, shared system prefixes and "
        "network noise can affect results. Cache counts must be provider-reported. Response wall "
        "time is not TTFT or an isolated prefill measurement. No GPU occupancy is measured.",
    }
    if not execute:
        return {**report, "status": "plan_only"}
    if not settings.model or settings.model_budget_usd <= 0:
        raise ValueError("Cache experiment requires a model and positive shared budget")
    store = Store(settings.data_dir)
    telemetry = Telemetry(store)
    provider = Provider(settings, store, telemetry, transport)
    evidence = [
        {
            "id": "synthetic-turn-" + str(i),
            "t": 4.2 + i,
            "kind": "turn_candidate",
            "text": "The visible green marker changes direction in the generated practice drill. "
            "The image observation alone does not establish a mechanical cause.",
            "origin": "green-marker-v1",
        }
        for i in range(6)
    ]
    measurements, teams = [], []
    try:
        for pair in range(pairs):
            team = "synthetic-cache-" + uid()
            teams.append(team)
            for phase in ("isolated", "repeat"):
                try:
                    selection, inference = await provider.choose(
                        "What can we compare about this turn?", evidence, team
                    )
                    outcome = (
                        "valid"
                        if selection.question_id == "compare_turn"
                        else "wrong_teaching_question"
                    )
                except ModelUnavailable as error:
                    outcome, inference = error.reason, {}
                measurements.append(
                    {"pair": pair + 1, "phase": phase, "outcome": outcome, "inference": inference}
                )
        marks = ",".join("?" for _ in teams)
        ledger = store.one(
            "SELECT COALESCE(SUM(charged),0) AS settled_usd,"
            "COALESCE(SUM(CASE WHEN status='settled' THEN 0 ELSE reserved END),0) AS reserved_usd "
            f"FROM ledger WHERE team IN ({marks})",
            tuple(teams),
        )
        return {
            **report,
            "status": "measured",
            "measurements": measurements,
            "ledger": ledger,
            "accounted_plus_reserved_usd": ledger["settled_usd"] + ledger["reserved_usd"],
        }
    finally:
        telemetry.close()
