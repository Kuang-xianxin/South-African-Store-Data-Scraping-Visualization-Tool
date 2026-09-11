"""Windows Seller-only hook for BLUE and same-account GREEN consumers.

Opt-in via an exact local marker. Importing this module does not reconfigure
the ERP database, proxy, working directory, environment or scheduled tasks.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading

from blue_seller_client import AdmissionError, SellerRun


ROOT = Path("D:/TakealotBlue")
MARKER = ROOT / "state/seller-api-enabled.json"
_process_run = None
_process_lock = threading.Lock()


def current_sid() -> str:
    value = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    identifiers = re.findall(rb"S-1-[0-9-]+", value)
    if len(identifiers) != 1:
        raise AdmissionError("Seller service identity unavailable")
    return identifiers[0].decode("ascii")


def seller_ssh_command(remote):
    from blue_arbiter_observer import ssh_command

    # GREEN user tasks and BLUE SYSTEM services get different restricted keys.
    # Never widen access to the database observer's SYSTEM-only private key.
    key = ROOT / "secrets/seller-api" / current_sid() / "id_ed25519"
    if not key.is_file():
        raise AdmissionError("Seller-only key is not installed for this service identity")
    command = ssh_command(remote)
    command[command.index("-i") + 1] = str(key)
    return command


class SSH:
    def __init__(self, command):
        self.command = command
        self.process = None
        self.lines = None
        self.lock = threading.Lock()

    def _read(self, stream, lines):
        try:
            while line := stream.readline(131073):
                if len(line) > 131072 or not line.endswith(b"\n"):
                    break
                lines.put(line)
        finally:
            lines.put(None)

    def _receive(self):
        try:
            raw = self.lines.get(timeout=25)
            if raw is None:
                raise AdmissionError("Seller arbitration connection closed")
            return json.loads(raw)
        except (queue.Empty, ValueError, TypeError):
            raise AdmissionError("Seller arbitration response unavailable") from None

    def __call__(self, message):
        with self.lock:
            try:
                if self.process is None:
                    self.process = subprocess.Popen(
                        self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.lines = queue.Queue()
                    threading.Thread(target=self._read, args=(self.process.stdout, self.lines), daemon=True).start()
                    greeting = self._receive()
                    if (greeting.get("cluster") != "takealot-blue-3307-v1" or
                            greeting.get("protocol") != 1 or greeting.get("status") != "hello"):
                        raise AdmissionError("Seller arbitration handshake mismatch")
                self.process.stdin.write((json.dumps(message) + "\n").encode())
                self.process.stdin.flush()
                return self._receive()
            except BaseException:
                self.close()
                # No replay: the remote may have committed admission before disconnect.
                raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None


class Journal:
    def __init__(self, path: Path, protect, unprotect):
        self.path, self.protect, self.unprotect = path, protect, unprotect
        # This is a receipt journal, not a fallback business database.
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, secret BLOB NOT NULL, http_status INTEGER, ack INTEGER NOT NULL DEFAULT 0)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, entry):
        protected = self.protect(json.dumps(entry).encode())
        with self._connect() as db:
            db.execute("INSERT INTO attempts(id,secret) VALUES (?,?)", (entry["request_id"], protected))

    def finish(self, request_id, http_status):
        with self._connect() as db:
            updated = db.execute("UPDATE attempts SET http_status=? WHERE id=? AND http_status IS NULL AND ack=0", (http_status, request_id))
            if updated.rowcount != 1:
                raise AdmissionError("Seller completion journal mismatch")

    def completed(self):
        with self._connect() as db:
            rows = db.execute("SELECT secret,http_status FROM attempts WHERE http_status IS NOT NULL AND ack=0 ORDER BY rowid").fetchall()
        return [{**json.loads(self.unprotect(blob)), "http_status": code} for blob, code in rows]

    def ack(self, request_id):
        with self._connect() as db:
            db.execute("UPDATE attempts SET ack=1 WHERE id=? AND http_status IS NOT NULL", (request_id,))


def _new_run():
    import blue_node

    cfg = blue_node.config()  # Node/hostname only; never opens or changes MySQL.
    marker = json.loads(MARKER.read_text(encoding="utf-8"))
    if marker != {"version": 1, "mode": "seller-request-authority", "node": cfg["node"]}:
        raise AdmissionError("Seller admission marker invalid")
    remote = json.loads((ROOT / "arbiter.json").read_text(encoding="utf-8-sig"))
    if (remote.get("cluster") != "takealot-blue-3307-v1" or remote.get("node") != cfg["node"] or
            remote.get("host") not in {"119.91.117.232", "100.72.100.10"}):
        raise AdmissionError("Seller arbitration identity invalid")
    journal = Journal(ROOT / "state/seller-api/receipts.sqlite3", blue_node.protect_secret,
                      lambda blob: blue_node.protect_secret(blob, decrypt=True))
    return SellerRun(SSH(seller_ssh_command(remote)), journal)


def _cli_process() -> bool:
    return (Path(sys.argv[0]).name.casefold() == "cli.py" or
            "takealot_ops.cli" in getattr(sys, "orig_argv", []))


def install(client_type, *, run_factory=None, process_scope=None) -> None:
    """Patch the existing class object, including modules that imported it earlier."""
    if getattr(client_type, "_seller_authority_installed", False):
        return
    factory = run_factory or _new_run
    long_lived = _cli_process() if process_scope is None else process_scope
    original_init, original_send, original_close = client_type.__init__, client_type._send_get, client_type.close

    def initialize(self, *args, **kwargs):
        global _process_run
        original_init(self, *args, **kwargs)
        try:
            if long_lived:
                with _process_lock:
                    if _process_run is None:
                        _process_run = factory()
                        atexit.register(_process_run.close)
                    self._seller_run = _process_run
            else:
                self._seller_run = factory()
        except BaseException:
            original_close(self)
            raise

    def send(self, path, params):
        return self._seller_run.perform(lambda: original_send(self, path, params))

    def close(self):
        try:
            original_close(self)
        finally:
            if not long_lived:
                self._seller_run.close()

    client_type.__init__, client_type._send_get, client_type.close = initialize, send, close
    client_type._seller_authority_installed = True


def install_if_enabled(client_type) -> None:
    # Intended for both ERP environments on these two hosts. No marker means the
    # old behavior remains until the coordinated installation is complete.
    if os.name == "nt" and MARKER.exists():
        install(client_type)
