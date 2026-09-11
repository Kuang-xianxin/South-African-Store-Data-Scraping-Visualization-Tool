"""Fail-closed client for authenticated BLUE Seller admission.

The wire transport and journal are injected: tests exercise actual HTTP sends
without Windows/SSH, while deployment supplies pinned SSH and protected storage.
"""
from __future__ import annotations

import threading
import uuid

from blue_seller_authority import CLUSTER


class AdmissionError(RuntimeError):
    """Not an ApiTransportError: the HTTP retry loop must not bypass admission."""


class SellerRun:
    def __init__(self, rpc, journal, *, heartbeat_seconds=5):
        self.rpc, self.journal = rpc, journal
        self.run_id = uuid.uuid4().hex
        self.generation = None
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.failure = None
        self.closed = False
        self.heartbeat_seconds = heartbeat_seconds

    def _call(self, action: str, **fields) -> dict:
        with self.lock:
            result = self.rpc({"cluster": CLUSTER, "action": "seller." + action,
                               "run_id": self.run_id, "generation": self.generation, **fields})
            if result.get("cluster") != CLUSTER or result.get("protocol") != "seller-v1":
                raise AdmissionError("Seller arbitration protocol mismatch")
            return result

    def _require(self, reply: dict, statuses: set[str]) -> dict:
        if (reply.get("cluster") != CLUSTER or reply.get("protocol") != "seller-v1" or
                reply.get("status") not in statuses):
            # Only controlled reason codes; never include RPC payloads or exception text.
            raise AdmissionError("Seller execution permission unavailable")
        return reply

    def start(self) -> None:
        with self.lock:
            if self.closed or self.failure:
                raise AdmissionError("Seller run is closed or its authority was lost")
            if self.generation is not None:
                return
            # Recover a completed response whose COMMIT acknowledgement was lost.
            # Uncertain HTTP attempts are intentionally not converted into completions.
            for entry in self.journal.completed():
                result = self.rpc({"cluster": CLUSTER, "action": "seller.complete", **entry})
                self._require(result, {"completed", "already-completed"})
                self.journal.ack(entry["request_id"])
            reply = self._require(self._call("begin_run"), {"run-granted"})
            self.generation = reply["generation"]
            if self.heartbeat_seconds is not None:
                self.thread = threading.Thread(target=self._heartbeat, name="blue-seller-authority", daemon=True)
                self.thread.start()

    def _heartbeat(self) -> None:
        while not self.stop.wait(self.heartbeat_seconds):
            try:
                self._require(self._call("renew"), {"renewed"})
            except Exception:
                self.failure = True
                return

    def perform(self, send):
        with self.request_lock:
            self.start()
            if self.failure or self.closed:
                raise AdmissionError("Seller authority was lost; no HTTP request sent")
            request_id, token = uuid.uuid4().hex, uuid.uuid4().hex
            entry = {"run_id": self.run_id, "generation": self.generation,
                     "request_id": request_id, "token": token}
            self.journal.prepare(entry)  # Durable before remote admission.
            try:
                reply = self._require(self._call("begin_request", request_id=request_id, token=token),
                                      {"request-granted"})
                if reply.get("request_permitted") is not True:
                    raise AdmissionError("Seller request was not admitted")
            except BaseException:
                self.failure = True
                raise
            try:
                response = send()
                # A buffered complete HTTP response is the release evidence.
                response.read()
            except BaseException:
                self.failure = True
                try:
                    self._call("uncertain", request_id=request_id, token=token)
                except Exception:
                    pass  # Server admission remains sticky even without this update.
                raise AdmissionError("Seller HTTP outcome is unconfirmed; further calls stopped") from None
            self.journal.finish(request_id, response.status_code)
            try:
                self._require(self._call("complete", request_id=request_id, token=token,
                                         http_status=response.status_code), {"completed", "already-completed"})
                self.journal.ack(request_id)
            except BaseException:
                self.failure = True
                # Never return this as a transport failure and retry the HTTP call.
                raise AdmissionError("Seller response received; arbitration acknowledgement pending") from None
            return response

    def close(self) -> None:
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.request_lock:
            if self.closed:
                return
            self.closed = True
            if self.generation is not None:
                try:
                    self._require(self._call("end_run"), {"run-ended"})
                except Exception:
                    pass  # No implicit release of unconfirmed requests.
        close = getattr(self.rpc, "close", None)
        if close:
            close()
