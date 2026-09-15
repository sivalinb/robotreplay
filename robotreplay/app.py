import asyncio
import hashlib
import json
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import generate_latest
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from robotreplay import __version__
from robotreplay.agent import Investigator
from robotreplay.config import Settings
from robotreplay.evaluations import run_evaluations
from robotreplay.lab import ContextPlan, context_plan, gpu_snapshot
from robotreplay.logs import parse_console
from robotreplay.media import make_demo
from robotreplay.policy import NemoPolicy, input_policy
from robotreplay.provider import Provider
from robotreplay.store import Store, create_user, encode, uid, verify_password
from robotreplay.telemetry import Telemetry, trace_id
from robotreplay.worker import Worker

STATIC = Path(__file__).parent / "static"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class Question(StrictModel):
    clip_ids: list[str] = Field(min_length=1, max_length=2)
    question: str = Field(min_length=1, max_length=1200)
    retrieval_mode: Literal["lexical", "hybrid"] = "lexical"


class Annotation(StrictModel):
    t: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=400)


class ConsoleLog(StrictModel):
    text: str = Field(min_length=1, max_length=65536)
    offset: float = Field(default=0, ge=-180, le=180, allow_inf_nan=False)
    uncertainty: float = Field(default=0.3, ge=0, le=30, allow_inf_nan=False)


class DemoRequest(StrictModel):
    obscured: bool = False


class BodyLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            if int(headers.get(b"content-length", b"0")) > self.limit:
                return await JSONResponse({"detail": "body_too_large"}, 413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "invalid_content_length"}, 400)(
                scope, receive, send
            )
        count = 0

        async def limited_receive():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > self.limit:
                raise HTTPException(413, "body_too_large")
            return message

        return await self.app(scope, limited_receive, send)


def create_app(settings=None, provider_transport=None):
    settings = settings or Settings.from_env()
    settings.validate()
    store = Store(settings.data_dir)
    telemetry = Telemetry(store, settings.otlp_endpoint)
    provider = Provider(settings, store, telemetry, provider_transport)
    nemo = NemoPolicy() if settings.nemo_enabled else None
    investigator = Investigator(store, telemetry, provider, nemo)
    worker = Worker(store, telemetry, settings)
    metrics_path = store.root / "metrics-token"
    if not metrics_path.exists():
        metrics_path.write_text(secrets.token_urlsafe(32))
        metrics_path.chmod(0o600)
    metrics_token = metrics_path.read_text().strip()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker.run())
        yield
        worker.stopping = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        telemetry.close()

    app = FastAPI(
        title="RobotReplay",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.telemetry = store, telemetry
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(BodyLimit, limit=settings.max_upload_bytes + 1024 * 1024)

    @app.middleware("http")
    async def visibility(request, call_next):
        started = time.perf_counter()
        # Origin validation supplements session CSRF tokens, including login CSRF.
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "origin_denied"}, status_code=403)
        with telemetry.span("http.request") as span:
            response = await call_next(request)
            route = request.scope.get("route")
            name = route.path if route else "unmatched"
            span.set_attribute("rr.route", name)
            span.set_attribute("rr.team", getattr(request.state, "team", "system"))
            span.set_attribute("http.response.status_code", response.status_code)
            response.headers["X-Trace-ID"] = trace_id()
        telemetry.requests.labels(
            name,
            request.method
            if request.method in {"GET", "POST", "DELETE", "PUT", "PATCH", "HEAD"}
            else "OTHER",
            str(response.status_code),
        ).inc()
        telemetry.latency.labels(name).observe(time.perf_counter() - started)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; media-src 'self' blob:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    def user(request: Request):
        token = request.cookies.get("rr_session", "")
        session = store.one(
            """SELECT u.id,u.username,u.team,u.role,s.csrf
          FROM sessions s JOIN users u ON u.id=s.user_id
          WHERE s.token_hash=? AND s.expires>?""",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        )
        if not session:
            raise HTTPException(401, "sign_in_required")
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "csrf_denied")
        request.state.team = session["team"]
        return session

    def owned(clip_id, identity):
        clip = store.clip(clip_id, identity["team"])
        if not clip:
            raise HTTPException(404, "clip_not_found")
        return clip

    def admission(path, title, identity, permission, source, carrier):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = store.one(
            "SELECT * FROM clips WHERE team=? AND sha256=?", (identity["team"], digest)
        )
        if existing:
            path.unlink(missing_ok=True)
            return existing
        clip_id = uid()
        directory = store.media_dir(clip_id)
        directory.mkdir(mode=0o700)
        shutil.move(path, directory / "source.bin")
        now = time.time()
        try:
            with store.connect() as db:
                db.execute(
                    """INSERT INTO clips(id,team,title,sha256,permission,source,created,
                  expires,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        clip_id,
                        identity["team"],
                        title,
                        digest,
                        permission,
                        source,
                        now,
                        now + settings.retention_days * 86400,
                        "queued",
                    ),
                )
                db.execute(
                    "INSERT INTO jobs(id,clip_id,status,queued,traceparent) VALUES(?,?,?,?,?)",
                    (uid(), clip_id, "queued", now, carrier.get("traceparent", "")),
                )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        store.audit(identity["team"], "ingest", "permission_recorded")
        return store.clip(clip_id, identity["team"])

    attempts = {}

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        now = time.monotonic()
        peer = request.client.host if request.client else "unknown"
        for key in list(attempts):
            if now - attempts[key][0] >= 60:
                del attempts[key]
        count = attempts.get(peer, (now, 0))
        if count[1] >= 8 or len(attempts) > 1000:
            raise HTTPException(429, "login_rate_limit")
        attempts[peer] = (count[0], count[1] + 1)
        account = store.one("SELECT * FROM users WHERE username=?", (body.username,))
        if not account or not verify_password(body.password, account["password"]):
            raise HTTPException(401, "invalid_credentials")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        store.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        store.execute(
            "INSERT INTO sessions VALUES(?,?,?,?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                account["id"],
                csrf,
                time.time() + 8 * 3600,
            ),
        )
        request.state.team = account["team"]
        response = JSONResponse({"username": account["username"], "csrf": csrf})
        response.set_cookie(
            "rr_session",
            token,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request, identity=Depends(user)):
        token = request.cookies.get("rr_session", "")
        store.execute(
            "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
        )
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie("rr_session")
        return response

    @app.get("/api/me")
    def me(identity=Depends(user)):
        return {
            "username": identity["username"],
            "csrf": identity["csrf"],
            "demo": settings.demo,
            "provider": settings.provider,
            "nemo_enabled": settings.nemo_enabled,
        }

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/clips")
    def clips(identity=Depends(user)):
        return store.rows(
            "SELECT * FROM clips WHERE team=? ORDER BY created DESC LIMIT 100", (identity["team"],)
        )

    @app.post("/api/clips", status_code=202)
    async def upload(
        file: UploadFile = File(...),
        title: str = Form(...),
        permission: str = Form(...),
        source: str = Form("Own recording"),
        identity=Depends(user),
    ):
        if settings.demo:
            raise HTTPException(403, "demo_accepts_generated_media_only")
        if permission not in {"own_recording", "written_permission", "compatible_license"}:
            raise HTTPException(422, "permission_required")
        if not 1 <= len(title.strip()) <= 100 or not 1 <= len(source) <= 500:
            raise HTTPException(422, "invalid_provenance")
        temporary = store.root / (uid() + ".upload")
        try:
            size = 0
            with temporary.open("xb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "file_too_large")
                    stream.write(chunk)
            if not size:
                raise HTTPException(422, "empty_file")
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            with telemetry.span("ingest.persist", identity["team"]):
                return admission(temporary, title.strip(), identity, permission, source, carrier)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    @app.post("/api/demo", status_code=202)
    async def demo(body: DemoRequest, identity=Depends(user)):
        path = store.root / (uid() + ".mp4")
        try:
            with telemetry.span("demo.generate_video", identity["team"]):
                await asyncio.to_thread(make_demo, path, body.obscured)
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            return admission(
                path,
                "Occluded practice" if body.obscured else "Clear practice",
                identity,
                "original_generated",
                "RobotReplay procedural video; no third-party footage",
                carrier,
            )
        finally:
            path.unlink(missing_ok=True)

    @app.get("/api/clips/{clip_id}")
    def clip_detail(clip_id: str, identity=Depends(user)):
        clip = owned(clip_id, identity)
        return {
            "clip": clip,
            "evidence": store.evidence(clip_id, identity["team"]),
            "samples": store.rows("SELECT * FROM samples WHERE clip_id=? ORDER BY t", (clip_id,)),
            "jobs": store.rows(
                "SELECT status,queued,started,finished,attempts FROM jobs WHERE clip_id=?",
                (clip_id,),
            ),
        }

    @app.get("/api/clips/{clip_id}/video")
    def video(clip_id: str, identity=Depends(user)):
        clip = owned(clip_id, identity)
        if clip["status"] != "ready":
            raise HTTPException(409, "video_not_ready")
        path = store.media_dir(clip_id) / "playback.mp4"
        if not path.exists():
            raise HTTPException(404, "video_missing")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/clips/{clip_id}/annotations", status_code=201)
    def annotate(clip_id: str, body: Annotation, identity=Depends(user)):
        clip = owned(clip_id, identity)
        if clip["status"] != "ready" or body.t > clip["duration"]:
            raise HTTPException(422, "timestamp_out_of_range")
        evidence_id = store.add_evidence(
            clip_id, identity["team"], body.t, body.t, "human_observation", body.text, "mentor"
        )
        store.audit(identity["team"], "annotation", "human_observation_added")
        return {"id": evidence_id}

    @app.post("/api/clips/{clip_id}/logs", status_code=201)
    def console(clip_id: str, body: ConsoleLog, identity=Depends(user)):
        clip = owned(clip_id, identity)
        if clip["status"] != "ready":
            raise HTTPException(409, "video_not_ready")
        try:
            rows = parse_console(body.text, clip["duration"], body.offset, body.uncertainty)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        ids = [
            store.add_evidence(
                clip_id,
                identity["team"],
                r["t"],
                r["t"],
                "console_print",
                r["text"],
                "vex_console",
                r["uncertainty"],
            )
            for r in rows
        ]
        return {
            "imported": len(ids),
            "evidence_ids": ids,
            "note": "Clock alignment is approximate; coincident events do not prove causation.",
        }

    @app.delete("/api/clips/{clip_id}")
    def delete(clip_id: str, identity=Depends(user)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            clip = db.execute(
                "SELECT * FROM clips WHERE id=? AND team=?", (clip_id, identity["team"])
            ).fetchone()
            if not clip:
                raise HTTPException(404, "clip_not_found")
            if clip["status"] == "processing":
                raise HTTPException(409, "wait_for_analysis_before_deletion")
            db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        path = store.media_dir(clip_id)
        shutil.rmtree(path, ignore_errors=True)
        store.audit(identity["team"], "retention", "clip_deleted")
        return {
            "status": "deleted",
            "indexes_removed": True,
            "media_removed": not path.exists(),
            "backup_note": "External filesystem backups have their own expiry policy.",
        }

    @app.post("/api/questions")
    async def ask(body: Question, identity=Depends(user)):
        for clip_id in body.clip_ids:
            clip = owned(clip_id, identity)
            if clip["status"] != "ready":
                raise HTTPException(409, "analysis_not_ready")
        with telemetry.span("investigate", identity["team"]):
            result = await investigator.ask(
                identity["team"], body.clip_ids, body.question, body.retrieval_mode
            )
            result["trace_id"] = trace_id()
            return result

    @app.get("/api/dashboard")
    def dashboard(identity=Depends(user)):
        team = identity["team"]
        clips = store.rows(
            "SELECT status,COUNT(*) AS count FROM clips WHERE team=? GROUP BY status", (team,)
        )
        jobs = store.rows(
            """SELECT j.status,COUNT(*) AS count FROM jobs j JOIN clips c ON c.id=j.clip_id
                            WHERE c.team=? GROUP BY j.status""",
            (team,),
        )
        traces = store.rows(
            """SELECT trace_id,MIN(started) AS started,COUNT(*) AS spans,
          MAX(started+duration_ms/1000)-MIN(started) AS elapsed FROM spans
          WHERE team=? GROUP BY trace_id ORDER BY started DESC LIMIT 15""",
            (team,),
        )
        ledger = store.one(
            """SELECT COALESCE(SUM(charged),0) AS settled_usd,
          COALESCE(SUM(CASE WHEN status!='settled' THEN reserved ELSE 0 END),0) AS reserved_usd,
          COUNT(*) AS attempts FROM ledger WHERE team=?""",
            (team,),
        )
        return {
            "clips": clips,
            "jobs": jobs,
            "traces": traces,
            "audit": store.rows(
                "SELECT action,reason,created FROM audit WHERE team=? ORDER BY id DESC LIMIT 20",
                (team,),
            ),
            "ledger": ledger,
            "budget_usd": settings.model_budget_usd,
            "gpu": {
                "status": "unavailable",
                "reason": "GPU metrics require a connected self-hosted exporter.",
            },
            "telemetry": {
                "local": "sqlite",
                "otlp": "configured" if settings.otlp_endpoint else "disabled",
                "collector_health": "not_verified",
            },
            "provider": settings.provider,
            "inference": store.rows(
                "SELECT created,provider,operation,outcome,duration_ms,input_tokens,output_tokens,"
                "cached_input_tokens,reasoning_tokens,cost_usd,cost_basis FROM inference_events "
                "WHERE team=? ORDER BY id DESC LIMIT 20",
                (team,),
            ),
        }

    @app.get("/api/traces/{trace}")
    def trace_detail(trace: str, identity=Depends(user)):
        rows = store.rows(
            "SELECT * FROM spans WHERE team=? AND trace_id=? ORDER BY started",
            (identity["team"], trace),
        )
        for row in rows:
            row["attrs"] = json.loads(row["attrs"])
        return rows

    @app.post("/api/evaluations")
    def evaluate(identity=Depends(user)):
        with telemetry.span("eval.policy_suite", identity["team"]):
            report = run_evaluations()
            report["id"] = uid()
            store.execute(
                "INSERT INTO eval_runs VALUES(?,?,?,?)",
                (report["id"], identity["team"], time.time(), encode(report)),
            )
        return report

    @app.get("/api/evaluations")
    def reports(identity=Depends(user)):
        return [
            json.loads(r["report"])
            for r in store.rows(
                "SELECT report FROM eval_runs WHERE team=? ORDER BY created DESC LIMIT 10",
                (identity["team"],),
            )
        ]

    @app.post("/api/policy/check")
    def policy_check(body: Annotation, identity=Depends(user)):
        decision = input_policy(body.text)
        telemetry.policy.labels(decision.reason).inc()
        return {"allowed": decision.allowed, "reason": decision.reason, "message": decision.message}

    @app.get("/metrics")
    def metrics(request: Request):
        expected = "Bearer " + metrics_token
        if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "metrics_token_required")
        return Response(generate_latest(telemetry.registry), media_type="text/plain; version=0.0.4")

    @app.get("/api/openapi.json")
    def schema(identity=Depends(user)):
        return app.openapi()

    @app.post("/api/lab/context")
    def plan(body: ContextPlan, identity=Depends(user)):
        try:
            return context_plan(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/lab/gpu")
    def gpu(identity=Depends(user)):
        return gpu_snapshot()

    @app.get("/api/lab/provider-results")
    def provider_results(identity=Depends(user)):
        # Reviewed synthetic release artifact only. This endpoint never starts an API experiment.
        artifact = Path(__file__).parent / "data" / "provider-results.json"
        if not artifact.exists():
            return {"kind": "published_snapshot", "reports": []}
        return json.loads(artifact.read_text())

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def setup_demo(settings):
    store = Store(settings.data_dir)
    if not store.one("SELECT id FROM users WHERE username='demo'"):
        create_user(store, "demo", "robotreplay-demo", team="demo-team")
    return store
