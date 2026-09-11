"""Prepared radar responses with bounded, permission-scoped background refresh."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import io
import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context, copy_context
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import Response


def _brotli_body(body: bytes) -> bytes | None:
    try:
        brotli = importlib.import_module("brotli")
    except ImportError:
        return None
    return bytes(brotli.compress(body, quality=5, mode=brotli.MODE_TEXT))


def radar_code_fingerprint(root: Path) -> str:
    """Never reuse a persisted projection across a backend code change."""
    root = Path(os.environ.get("TAKEALOT_RELEASE_SOURCE_ROOT", str(root)))
    digest = hashlib.sha256(b"radar-page-response-v1")
    for path in sorted((root / "src" / "takealot_ops").rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class PreparedRadarPage:
    body: bytes
    compressed: bytes
    etag: str
    version: str
    generated_at: float
    brotli_body: bytes | None = None

    def response(self, request: Request, *, refreshing: bool) -> Response:
        headers = {
            "Cache-Control": "private, no-cache",
            "Vary": "Accept-Encoding, Cookie, X-Store-Code",
            "ETag": "W/" + self.etag,
            "X-ERP-Refreshing": "1" if refreshing else "0",
            "X-ERP-Generated-At": str(self.generated_at),
        }
        candidates = {part.strip().removeprefix("W/") for part in request.headers.get("if-none-match", "").split(",")}
        if self.etag in candidates or "*" in candidates:
            return Response(status_code=304, headers=headers)
        accepted = request.headers.get("accept-encoding", "").lower()
        qualities: dict[str, float] = {}
        for item in accepted.split(","):
            coding, *parameters = (part.strip() for part in item.split(";"))
            if coding not in {"gzip", "br"}:
                continue
            quality = next((part[2:] for part in parameters if part.startswith("q=")), "1")
            try:
                qualities[coding] = float(quality)
            except ValueError:
                qualities[coding] = 0
        content = self.body
        if self.brotli_body is not None and qualities.get("br", 0) > 0 and qualities.get("br", 0) >= qualities.get("gzip", 0):
            headers["Content-Encoding"] = "br"
            content = self.brotli_body
        elif qualities.get("gzip", 0) > 0:
            headers["Content-Encoding"] = "gzip"
            content = self.compressed
        return Response(
            content,
            media_type="application/json", headers=headers,
        )


@dataclass
class _ReadJob:
    version: str
    loader: Callable[[], dict[str, Any]]
    context: Context
    future: Future[PreparedRadarPage]
    started: bool = False


class RadarPageCache:
    """Keep complete results visible while bounded workers prepare newer data.

    Authorization and date are part of a hard boundary supplied by the caller.
    Only data revisions may reuse the previous result, for at most 15 minutes.
    The ordinary/default read always waits for a current projection.
    """

    def __init__(
        self, *, directory: Path | None = None, namespace: str = "v1",
        max_entries: int = 6, fresh_seconds: float = 180,
        preview_seconds: float = 900, clock: Callable[[], float] = time.time,
        max_bytes: int = 32 * 1024 * 1024, max_workers: int = 2,
    ) -> None:
        self._directory = directory
        self._namespace = namespace
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_workers = max_workers
        self._fresh_seconds = fresh_seconds
        self._preview_seconds = preview_seconds
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[str, PreparedRadarPage] = OrderedDict()
        self._inflight: dict[str, _ReadJob] = {}
        self._pending: dict[str, _ReadJob] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._closing = False

    def get_or_load(
        self, key: Hashable, *, version: str, boundary: str,
        loader: Callable[[], dict[str, Any]], prefer_cached: bool = False,
    ) -> tuple[PreparedRadarPage, bool]:
        digest = hashlib.sha256(repr((self._namespace, boundary, key)).encode()).hexdigest()
        with self._lock:
            if self._closing:
                raise RuntimeError("Radar cache is closing")
            entry = self._entries.get(digest)
            if entry is None:
                entry = self._read(digest)
                if entry is not None:
                    self._remember(digest, entry)
            age = max(0.0, self._clock() - entry.generated_at) if entry else float("inf")
            if entry and entry.version == version and age < self._fresh_seconds:
                self._entries.move_to_end(digest)
                return entry, False
            job = self._inflight.get(digest)
            if job is None:
                if self._executor is None:
                    # The independent partitions must not queue behind each other.
                    self._executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="radar-read")
                job = _ReadJob(version, loader, copy_context(), Future())
                self._inflight[digest] = job
                self._executor.submit(self._run_job, digest, job)
            elif job.version != version:
                if job.started:
                    # One latest follow-up, never a queue for every crawl revision.
                    follow_up = self._pending.get(digest)
                    if follow_up is None:
                        follow_up = _ReadJob(version, loader, copy_context(), Future())
                        self._pending[digest] = follow_up
                    job = follow_up
                if job.version != version:
                    job.version, job.loader, job.context = version, loader, copy_context()
            if prefer_cached and entry and age < self._preview_seconds:
                return entry, True
        # A waiting reader joins the existing refresh, including its error.
        return job.future.result(), False

    def _run_job(self, digest: str, job: _ReadJob) -> None:
        with self._lock:
            job.started = True
        try:
            entry = job.context.run(self._load, digest, job.version, job.loader)
        except BaseException as error:
            job.future.set_exception(error)
        else:
            job.future.set_result(entry)
        finally:
            with self._lock:
                self._inflight.pop(digest, None)
                follow_up = self._pending.pop(digest, None)
                if follow_up is not None and self._executor is not None:
                    self._inflight[digest] = follow_up
                    self._executor.submit(self._run_job, digest, follow_up)

    def _load(
        self, digest: str, version: str, loader: Callable[[], dict[str, Any]],
    ) -> PreparedRadarPage:
        body = json.dumps(
            loader(), ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            default=jsonable_encoder,
        ).encode()
        entry = PreparedRadarPage(
            body=body, compressed=gzip.compress(body, compresslevel=5, mtime=0),
            etag='"' + hashlib.sha256(body).hexdigest() + '"',
            version=version, generated_at=self._clock(),
            brotli_body=_brotli_body(body),
        )
        self._write(digest, entry)
        with self._lock:
            self._remember(digest, entry)
        return entry

    def _remember(self, digest: str, entry: PreparedRadarPage) -> None:
        self._entries[digest] = entry
        self._entries.move_to_end(digest)
        while self._entries and (
            len(self._entries) > self._max_entries
            or sum(len(e.body) + len(e.compressed) + len(e.brotli_body or b"")
                   for e in self._entries.values()) > self._max_bytes
        ):
            self._entries.popitem(last=False)

    def _read(self, digest: str) -> PreparedRadarPage | None:
        if self._directory is None:
            return None
        path = self._directory / f"{digest}.radar-cache"
        try:
            if path.stat().st_size > 8_000_000:
                return None
            with path.open("rb") as handle:
                metadata = json.loads(handle.readline(4096))
                compressed = handle.read()
            if metadata["key"] != digest or metadata["format"] != 1:
                return None
            generated_at = float(metadata["generated_at"])
            if not 0 <= self._clock() - generated_at < self._preview_seconds:
                return None
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
                body = archive.read(64_000_001)
            if len(body) > 64_000_000:
                return None
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            if etag != metadata["etag"]:
                return None
            return PreparedRadarPage(body, compressed, etag, str(metadata["version"]), generated_at, _brotli_body(body))
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            return None

    def _write(self, digest: str, entry: PreparedRadarPage) -> None:
        if self._directory is None:
            return
        temporary: Path | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            metadata = json.dumps({
                "format": 1, "key": digest, "version": entry.version,
                "generated_at": entry.generated_at, "etag": entry.etag,
            }).encode()
            temporary = self._directory / f"{digest}-{uuid4().hex}.tmp"
            temporary.write_bytes(metadata + b"\n" + entry.compressed)
            os.replace(temporary, self._directory / f"{digest}.radar-cache")
            paths = sorted(self._directory.glob("*.radar-cache"), key=lambda p: p.stat().st_mtime)
            for path in paths[:-self._max_entries * 2]:
                path.unlink(missing_ok=True)
        except OSError:
            # A cache filesystem failure must not discard a successful live read.
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            executor, self._executor = self._executor, None
            for job in self._pending.values():
                job.future.cancel()
            self._pending.clear()
        if executor is not None:
            executor.shutdown(wait=True)
        with self._lock:
            self._closing = False
