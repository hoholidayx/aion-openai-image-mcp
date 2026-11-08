"""Durable, profile-scoped image jobs and a single bounded worker."""

import asyncio
import base64
from contextlib import closing, contextmanager
import fcntl
from hashlib import sha256
import json
import logging
from pathlib import Path
import sqlite3
import time

import httpx

from .adapters import ADAPTERS
from .adapters.base import ImageRequest, ProviderError
from .config import Config, Profile
from .images import MAX_REQUEST_BODY, png_dimensions

LOG = logging.getLogger(__name__)
MAX_QUEUED = 32
RESULT_RETENTION = 7 * 24 * 60 * 60
PROVIDER_TIMEOUT = 180
UNCERTAIN = "submission_uncertain"


class JobError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Jobs:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db = data_dir / "jobs.sqlite3"

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.db, timeout=5, isolation_level="IMMEDIATE")) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    @contextmanager
    def instance(self):
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.data_dir / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("The data directory already has a running image MCP") from exc
            self._initialize()
            yield

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    profile TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request TEXT,
                    result BLOB,
                    width INTEGER,
                    height INTEGER,
                    digest TEXT,
                    error_code TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (profile, task_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, updated_at)")
            db.execute(
                "UPDATE jobs SET state=?, error_code=?, request=NULL, updated_at=? WHERE state='submitting'",
                (UNCERTAIN, UNCERTAIN, int(time.time())),
            )

    def submit(self, profile: Profile, request: dict) -> dict:
        task_id = request.pop("request_id")
        canonical = json.dumps(request, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        encoded = canonical.encode()
        if len(encoded) > MAX_REQUEST_BODY:
            raise JobError("invalid_request", "Image request exceeds the size limit")
        digest = sha256(encoded).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT task_id, request_hash, state, error_code FROM jobs WHERE profile=? AND task_id=?",
                (profile.name, task_id),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise JobError("request_conflict", "Request ID already belongs to different inputs")
                return self._status(existing)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending','submitting')").fetchone()[0] >= MAX_QUEUED:
                raise JobError("queue_full", "Image job queue is full")
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL, NULL, ?)",
                (profile.name, task_id, digest, profile.fingerprint(), canonical, int(time.time())),
            )
        return {"task_id": task_id, "state": "pending"}

    @staticmethod
    def _status(row: sqlite3.Row) -> dict:
        result = {"task_id": row["task_id"], "state": row["state"]}
        if row["error_code"]:
            result["error_code"] = row["error_code"]
        return result

    def status(self, profile: str, task_id: str) -> dict:
        with self._connection() as db:
            row = db.execute(
                "SELECT task_id, state, error_code FROM jobs WHERE profile=? AND task_id=?", (profile, task_id),
            ).fetchone()
        if row is None:
            raise JobError("task_not_found", "Image task not found")
        return self._status(row)

    def result(self, profile: str, task_id: str) -> tuple[dict, bytes]:
        with self._connection() as db:
            row = db.execute(
                "SELECT state, result, width, height, digest FROM jobs WHERE profile=? AND task_id=?", (profile, task_id),
            ).fetchone()
        if row is None:
            raise JobError("task_not_found", "Image task not found")
        if row["state"] == "expired":
            raise JobError("result_expired", "Image result expired")
        if row["state"] != "completed":
            raise JobError("result_not_ready", "Image result is not available")
        return {"task_id": task_id, "width": row["width"], "height": row["height"], "sha256": row["digest"]}, row["result"]

    def claim(self) -> sqlite3.Row | None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT profile, task_id, profile_hash, request FROM jobs WHERE state='pending' ORDER BY updated_at, rowid LIMIT 1",
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET state='submitting', updated_at=? WHERE profile=? AND task_id=?",
                    (int(time.time()), row["profile"], row["task_id"]),
                )
        return row

    def finish(self, row: sqlite3.Row, state: str, *, error: str | None = None,
               image: bytes | None = None, dimensions: tuple[int, int] | None = None) -> None:
        width, height = dimensions or (None, None)
        digest = sha256(image).hexdigest() if image is not None else None
        with self._connection() as db:
            db.execute("""
                UPDATE jobs SET state=?, error_code=?, result=?, width=?, height=?, digest=?, request=NULL, updated_at=?
                WHERE profile=? AND task_id=? AND state='submitting'
            """, (state, error, image, width, height, digest, int(time.time()), row["profile"], row["task_id"]))

    def expire(self) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE jobs SET state='expired', result=NULL WHERE state='completed' AND updated_at<?",
                (int(time.time()) - RESULT_RETENTION,),
            )

    async def run(self, config: Config, wake: asyncio.Event) -> None:
        next_expiry = 0.0
        while True:
            if time.monotonic() >= next_expiry:
                self.expire()
                next_expiry = time.monotonic() + 60
            wake.clear()
            row = self.claim()
            if row is None:
                try:
                    await asyncio.wait_for(wake.wait(), 2)
                except TimeoutError:
                    pass
                continue
            await self._run_job(config, row)

    async def _run_job(self, config: Config, row: sqlite3.Row) -> None:
        profile = config.profiles.get(row["profile"])
        if profile is None or profile.fingerprint() != row["profile_hash"]:
            self.finish(row, "failed", error="backend_unavailable")
            return
        raw = json.loads(row["request"])
        request = ImageRequest(raw["prompt"], raw["size"], raw["preset"], tuple(
            base64.b64decode(image) for image in raw["images"]
        ))
        try:
            async with asyncio.timeout(PROVIDER_TIMEOUT), httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=httpx.Timeout(PROVIDER_TIMEOUT, connect=10),
                headers={"Accept-Encoding": "identity"},
            ) as client:
                image = await ADAPTERS[profile.adapter].generate(profile, request, client)
        except ProviderError as exc:
            self._failure(row, exc.code)
            return
        except Exception:
            # A supplier SDK can raise unexpected exceptions containing request data.
            self._failure(row, UNCERTAIN)
            return
        try:
            dimensions = await asyncio.to_thread(png_dimensions, image)
        except ValueError:
            self._failure(row, "output_invalid")
            return
        self.finish(row, "completed", image=image, dimensions=dimensions)
        LOG.info("Image task completed: %s/%s", row["profile"], row["task_id"])

    def _failure(self, row: sqlite3.Row, code: str) -> None:
        self.finish(row, UNCERTAIN if code == UNCERTAIN else "failed", error=code)
        LOG.warning("Image task failed: %s/%s (%s)", row["profile"], row["task_id"], code)
