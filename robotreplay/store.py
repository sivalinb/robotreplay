import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def uid():
    return uuid.uuid4().hex


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, team TEXT NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'mentor'
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
 csrf TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips(
 id TEXT PRIMARY KEY, team TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
 permission TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
 status TEXT NOT NULL, duration REAL, fps REAL, width INTEGER, height INTEGER,
 coverage REAL, error_code TEXT, UNIQUE(team,sha256)
);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE,
 status TEXT NOT NULL, queued REAL NOT NULL, started REAL, finished REAL,
 attempts INTEGER NOT NULL DEFAULT 0, traceparent TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS samples(
 clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE, t REAL NOT NULL,
 x REAL, y REAL, visible INTEGER NOT NULL, motion REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE,
 team TEXT NOT NULL, t REAL NOT NULL, end_t REAL NOT NULL, kind TEXT NOT NULL,
 text TEXT NOT NULL, origin TEXT NOT NULL, uncertainty REAL NOT NULL DEFAULT 0,
 created REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
 id UNINDEXED, clip_id UNINDEXED, team UNINDEXED, text
);
CREATE TRIGGER IF NOT EXISTS evidence_add AFTER INSERT ON evidence BEGIN
 INSERT INTO evidence_fts(id,clip_id,team,text) VALUES(new.id,new.clip_id,new.team,new.text);
END;
CREATE TRIGGER IF NOT EXISTS evidence_remove AFTER DELETE ON evidence BEGIN
 DELETE FROM evidence_fts WHERE id=old.id;
END;
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, team TEXT NOT NULL, action TEXT NOT NULL,
 reason TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spans(
 id INTEGER PRIMARY KEY, team TEXT NOT NULL, trace_id TEXT NOT NULL,
 span_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
 started REAL NOT NULL, duration_ms REAL NOT NULL, attrs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS span_team ON spans(team,started);
CREATE TABLE IF NOT EXISTS ledger(
 id TEXT PRIMARY KEY, team TEXT NOT NULL, reserved REAL NOT NULL,
 charged REAL NOT NULL DEFAULT 0, status TEXT NOT NULL, created REAL NOT NULL,
 input_tokens INTEGER, output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS eval_runs(
 id TEXT PRIMARY KEY, team TEXT NOT NULL, created REAL NOT NULL, report TEXT NOT NULL
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "clips").mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "robotreplay.sqlite"
        self.log_lock = threading.Lock()
        with self.connect() as db:
            db.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, parameters=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, parameters)]

    def one(self, sql, parameters=()):
        rows = self.rows(sql, parameters)
        return rows[0] if rows else None

    def execute(self, sql, parameters=()):
        with self.connect() as db:
            db.execute(sql, parameters)

    def clip(self, clip_id, team):
        return self.one("SELECT * FROM clips WHERE id=? AND team=?", (clip_id, team))

    def media_dir(self, clip_id):
        # IDs are application-generated, never user-selected filenames or remote URLs.
        if len(clip_id) != 32 or any(c not in "0123456789abcdef" for c in clip_id):
            raise ValueError("Invalid clip identifier")
        return self.root / "clips" / clip_id

    def audit(self, team, action, reason):
        self.execute(
            "INSERT INTO audit(team,action,reason,created) VALUES(?,?,?,?)",
            (team, action, reason, time.time()),
        )
        # Explicitly exclude team identities and content from the infrastructure log stream.
        event = (
            encode(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "service": "robotreplay",
                    "action": action,
                    "reason": reason,
                }
            )
            + "\n"
        )
        try:
            with self.log_lock:
                path = self.root / "events.jsonl"
                if path.exists() and path.stat().st_size > 5_000_000:
                    path.replace(self.root / "events.jsonl.1")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, event.encode())
                finally:
                    os.close(fd)
        except OSError:
            # SQLite audit remains authoritative; collector freshness must detect a missing file stream.
            pass

    def reserve(self, team, estimate, ceiling):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("""SELECT COALESCE(SUM(
              CASE WHEN status='settled' THEN charged ELSE reserved END),0) FROM ledger
              """).fetchone()[0]
            if estimate <= 0 or used + estimate > ceiling:
                return None
            request_id = uid()
            db.execute(
                "INSERT INTO ledger(id,team,reserved,status,created) VALUES(?,?,?,?,?)",
                (request_id, team, estimate, "reserved", time.time()),
            )
            return request_id

    def settle(self, request_id, charged, input_tokens, output_tokens):
        self.execute(
            """UPDATE ledger SET charged=?,status='settled',
          input_tokens=?,output_tokens=? WHERE id=?""",
            (charged, input_tokens, output_tokens, request_id),
        )

    def evidence(self, clip_id, team):
        return self.rows(
            "SELECT * FROM evidence WHERE clip_id=? AND team=? ORDER BY t", (clip_id, team)
        )

    def add_evidence(self, clip_id, team, t, end_t, kind, text, origin, uncertainty=0):
        evidence_id = uid()
        self.execute(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (evidence_id, clip_id, team, t, end_t, kind, text, origin, uncertainty, time.time()),
        )
        return evidence_id


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + result.hex()


def verify_password(password, encoded):
    salt, _ = encoded.split(":")
    return secrets.compare_digest(hash_password(password, bytes.fromhex(salt)), encoded)


def create_user(store, username, password, team=None):
    if not (3 <= len(username) <= 80 and 12 <= len(password) <= 256):
        raise ValueError("Username needs 3–80 characters; password needs 12–256")
    identity = uid()
    store.execute(
        "INSERT INTO users VALUES(?,?,?,?,?)",
        (identity, username, team or uid(), hash_password(password), "mentor"),
    )
    return identity


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)
