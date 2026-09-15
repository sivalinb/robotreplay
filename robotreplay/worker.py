import asyncio
import json
import os
import shutil
import sys
import time

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from robotreplay.store import uid


class Worker:
    def __init__(self, store, telemetry, settings):
        self.store, self.telemetry, self.settings = store, telemetry, settings
        self.stopping = False

    def recover(self):
        # One uvicorn process owns this worker. Durable jobs survive clean or abrupt restarts.
        self.store.execute("UPDATE jobs SET status='queued',started=NULL WHERE status='running'")
        self.store.execute("UPDATE clips SET status='queued' WHERE status='processing'")

    def claim(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.*, c.team FROM jobs j JOIN clips c ON c.id=j.clip_id
              WHERE j.status='queued' ORDER BY j.queued LIMIT 1""").fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running',started=?,attempts=attempts+1 WHERE id=?",
                (time.time(), row["id"]),
            )
            db.execute("UPDATE clips SET status='processing' WHERE id=?", (row["clip_id"],))
            return dict(row)

    async def process(self, job):
        directory = self.store.media_dir(job["clip_id"])
        context = TraceContextTextMapPropagator().extract({"traceparent": job["traceparent"]})
        code = None
        with self.telemetry.span(
            "media.analyze",
            job["team"],
            context=context,
            **{
                "rr.algorithm": "green-marker-v1",
                "rr.queue_seconds": max(0, time.time() - job["queued"]),
            },
        ):
            # Do not forward model credentials, session configuration, or the full parent env.
            env = {
                k: os.environ[k]
                for k in ("PATH", "SYSTEMROOT", "TMPDIR", "LANG")
                if k in os.environ
            }
            package_parent = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = package_parent
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "robotreplay.media",
                str(directory / "source.bin"),
                str(directory),
                "--max-duration",
                str(self.settings.max_duration),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), self.settings.worker_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if os.name == "posix":
                    import signal

                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
                code = "worker_deadline"
            if code is None and process.returncode:
                code = "analysis_failed"
                if (directory / "error.json").exists():
                    code = json.loads((directory / "error.json").read_text())["error_code"]
            if code is None:
                result = json.loads((directory / "analysis.json").read_text())
                self.persist(job, result)
            else:
                self.store.execute(
                    "UPDATE clips SET status='failed',error_code=? WHERE id=?",
                    (code, job["clip_id"]),
                )
        self.store.execute(
            "UPDATE jobs SET status=?,finished=? WHERE id=?",
            ("failed" if code else "complete", time.time(), job["id"]),
        )
        self.telemetry.jobs.labels("failed" if code else "complete").inc()
        self.store.audit(job["team"], "analysis", code or "complete")

    def persist(self, job, result):
        m = result["metadata"]
        with self.store.connect() as db:
            db.execute("DELETE FROM samples WHERE clip_id=?", (job["clip_id"],))
            db.execute(
                "DELETE FROM evidence WHERE clip_id=? AND origin='green-marker-v1'",
                (job["clip_id"],),
            )
            db.executemany(
                "INSERT INTO samples VALUES(?,?,?,?,?,?)",
                [
                    (job["clip_id"], s["t"], s["x"], s["y"], int(s["visible"]), s["motion"])
                    for s in result["samples"]
                ],
            )
            for e in result["evidence"]:
                db.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        job["clip_id"],
                        job["team"],
                        e["t"],
                        e["end_t"],
                        e["kind"],
                        e["text"],
                        "green-marker-v1",
                        0.2,
                        time.time(),
                    ),
                )
            db.execute(
                """UPDATE clips SET status='ready',duration=?,fps=?,width=?,height=?,
              coverage=?,error_code=NULL WHERE id=?""",
                (m["duration"], m["fps"], m["width"], m["height"], m["coverage"], job["clip_id"]),
            )

    async def run(self):
        self.recover()
        while not self.stopping:
            job = self.claim()
            if job:
                try:
                    await self.process(job)
                except Exception:
                    self.store.execute(
                        "UPDATE jobs SET status='failed',finished=? WHERE id=?",
                        (time.time(), job["id"]),
                    )
                    self.store.execute(
                        "UPDATE clips SET status='failed',error_code='worker_failed' WHERE id=?",
                        (job["clip_id"],),
                    )
                    self.store.audit(job["team"], "analysis", "worker_failed")
            else:
                self.expire()
                await asyncio.sleep(self.settings.worker_poll)

    def expire(self):
        for clip in self.store.rows(
            "SELECT id FROM clips WHERE expires<? AND status!='processing'", (time.time(),)
        ):
            # DB removal first: failed disk cleanup cannot leave the media readable through API.
            self.store.execute("DELETE FROM clips WHERE id=?", (clip["id"],))
            shutil.rmtree(self.store.media_dir(clip["id"]), ignore_errors=True)
