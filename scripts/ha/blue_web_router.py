"""Loopback Nginx routing decisions; never proxies bodies or changes crawler work."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from blue_web_ownership import Ownership
from blue_web_routes import artifact_key, policy
from blue_seller_refresh_policy import POLICY_VERSION, choose_refresh, observe_refresh


# Match Nginx's BLUE-only reverse SSH listeners. Probe the same transport that
# serves users; a healthy Tailnet peer must not mask a broken web tunnel.
NODES = {"main": "http://127.0.0.1:18505", "laptop": "http://127.0.0.1:18506"}
COOKIE = "takealot_blue_web_route"


def distributable(method: str, uri: str) -> bool:
    return not policy(method, uri)[0]


class Router:
    def __init__(self, secret: str, release: str, *, clock=time.monotonic, wall=time.time, state_path=None, initial_owner=None):
        self.secret, self.release, self.clock, self.wall = secret, release, clock, wall
        self.lock = threading.Lock()
        self.samples: dict[str, dict] = {}
        self.probe_failures = dict.fromkeys(NODES, 0)
        self.decisions = {"main": 0, "laptop": 0, "unavailable": 0}
        self.ownership = Ownership(state_path, wall=wall, initial_owner=initial_owner)

    def update(self, node: str, payload: dict, latency: float) -> None:
        if (payload.get("node") != node or not re.fullmatch(r"[a-f0-9]{64}", payload.get("frontend_sha256", ""))
                or not 0 < payload.get("available_bytes", 0) <= payload.get("total_bytes", 0)
                or not 0 < payload.get("available_commit_bytes", 0)):
            raise ValueError("Unverified BLUE capacity sample")
        with self.lock:
            previous = self.samples.get(node, {})
            if (payload.get('boot') == previous.get('boot') and
                    payload.get('inventory_at', 0) < previous.get('inventory_at', 0)):
                payload = {**payload, **{key: previous[key] for key in
                           ('mutation', 'workflows', 'files', 'inventory_at') if key in previous}}
            if payload.get('mutation') != previous.get('mutation'):
                self.ownership.changed()
            if node == "laptop":
                self.release = payload["frontend_sha256"]
            self.samples[node] = {**payload, "received": self.clock(), "latency": latency}
            self.probe_failures[node] = 0
            observe_refresh(self.ownership, node, payload)

    def probe_failed(self, node: str) -> None:
        with self.lock:
            self.probe_failures[node] = min(self.probe_failures[node] + 1, 2)

    def _sign(self, text: str) -> str:
        return hmac.new(self.secret.encode(), text.encode(), hashlib.sha256).hexdigest()[:32]

    def cookie_node(self, header: str) -> tuple[str, float] | None:
        try:
            cookies = SimpleCookie(header)
            value = cookies[COOKIE].value
            node, issued, signature = value.split(".")
            age = self.wall() - int(issued)
            if (node in NODES and 0 <= age <= 43200
                    and hmac.compare_digest(signature, self._sign(node + "." + issued))):
                return node, age
        except (CookieError, KeyError, ValueError, TypeError):
            pass
        return None

    def choose(self, method: str, uri: str, cookie: str = "") -> tuple[str | None, str]:
        with self.lock:
            live = {node: sample for node, sample in self.samples.items()
                    if self.clock() - sample["received"] < 12 and sample["frontend_sha256"] == self.release}
            if live:
                def score(name):
                    s = live[name]
                    available = min(s["available_bytes"], s["available_commit_bytes"])
                    headroom = max(0.01, available / s["total_bytes"])
                    return headroom / (1 + max(0, s.get("active_requests", 0)) * .08) / (1 + s["latency"] * .15)
                node = max(live, key=score)
                best = node
                pinned = self.cookie_node(cookie)
                if pinned and pinned[0] in live:
                    old, age = pinned
                    # Hold a session for at least a minute; small changes do not bounce it.
                    if age < 60 or score(node) < score(old) * 1.35:
                        node = old
                family, new = policy(method, uri)
                if (family == 'legacy' and method == 'POST' and uri.split('?')[0] == '/api/competitors/targets'
                        and not any(s.get('workflows', {}).get('legacy', {}).get('busy') for s in self.samples.values())):
                    family = ''  # Add-to-DB is shared; only enqueue-to-active-local-batch needs its owner.
                if family:
                    key = artifact_key(uri) if method in {'GET', 'HEAD'} else None
                    holders = [(s.get('files', {}).get(key, 0), name) for name, s in self.samples.items()
                               if key and s.get('files', {}).get(key)]
                    if holders:
                        # Newest known artifact wins; never silently return an
                        # older peer copy when its actual latest owner is offline.
                        node = max(holders)[1]
                        node = node if node in live else None
                        if any(s.get('workflows', {}).get(family, {}).get('busy') for s in self.samples.values()):
                            node = None  # Do not download a file while its producer may still be writing it.
                    elif family == 'refresh':
                        node = choose_refresh(
                            self.ownership, new, live, self.samples,
                            main_unavailable=self.probe_failures['main'] >= 2,
                        )
                    else:
                        node = self.ownership.choose(family, new, live, self.samples, best)
                if uri.split('?')[0] in {'/api/competitors/distributed/start-full', '/api/competitors/distributed/resume'}:
                    if any(s.get('workflows', {}).get('legacy', {}).get('busy') for s in self.samples.values()):
                        node = None
                new_cookie = ""
                if node and not family and (not pinned or pinned[0] != node or pinned[1] >= 3600):
                    value = f"{node}.{int(self.wall())}"
                    new_cookie = f"{COOKIE}={value}.{self._sign(value)}; Path=/; Max-Age=43200; Secure; HttpOnly; SameSite=Lax"
            else:
                node, new_cookie = None, ""
            self.decisions[node or "unavailable"] += 1
            return node, new_cookie

    def status(self) -> dict:
        with self.lock:
            return {"samples": {name: {**sample, "age_seconds": round(self.clock() - sample["received"], 2)}
                                for name, sample in self.samples.items()}, "decisions": dict(self.decisions),
                    "routing_version": 2, "owners": dict(self.ownership.owners), "revision": self.ownership.revision,
                    "seller_refresh_policy": POLICY_VERSION, "probe_failures": dict(self.probe_failures)}


def monitor(router: Router, node: str) -> None:
    opener = build_opener(ProxyHandler({}))
    while True:
        started = time.monotonic()
        try:
            request = Request(NODES[node] + "/__blue/capacity", headers={"X-Blue-Routing-Key": router.secret})
            with opener.open(request, timeout=3) as response:
                payload = json.loads(response.read(2 * 1024 * 1024))
            router.update(node, payload, time.monotonic() - started)
        except Exception:
            # Last good sample expires by local monotonic time, independent of node clocks.
            router.probe_failed(node)
        time.sleep(5)


def handler_for(router: Router, *, snapshots_only=False):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/snapshot' or not hmac.compare_digest(self.headers.get('X-Blue-Routing-Key', ''), router.secret):
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError('Invalid snapshot size')
                payload = json.loads(self.rfile.read(size))
                if payload.get('node') not in NODES:
                    raise ValueError('Invalid node')
                # A completion push updates metadata immediately, but cannot
                # resurrect liveness or replace measured network latency.
                with router.lock:
                    prior = router.samples.get(payload['node'])
                    if (prior and prior.get('boot') == payload.get('boot') and
                            payload.get('inventory_at', 0) > prior.get('inventory_at', 0)):
                        changed = prior.get('mutation') != payload.get('mutation')
                        for key in ('mutation', 'workflows', 'files', 'inventory_at'):
                            prior[key] = payload[key]
                        if changed:
                            router.ownership.changed()
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
            except (ValueError, KeyError, TypeError):
                self.send_error(400)

        def do_GET(self):
            if snapshots_only:
                self.send_error(404)
                return
            if self.path == "/status":
                body = json.dumps(router.status()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/route":
                node, cookie = router.choose(self.headers.get("X-Original-Method", ""),
                    self.headers.get("X-Original-URI", ""), self.headers.get("Cookie", ""))
                body = b""
                self.send_response(204 if node else 503)
                if node:
                    self.send_header("X-Blue-Node", node)
                    self.send_header('X-Blue-Revision', str(router.ownership.revision))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
            else:
                body = b""
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # Never log cookies, keys, user URLs or query strings.
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not re.fullmatch(r"[a-f0-9]{64}", config["secret"] + ""):
        raise ValueError("Invalid routing credential")
    router = Router(config["secret"], config["frontend_sha256"],
                    state_path=Path(config.get('state_path', '/var/lib/takealot-blue-routing/ownership.json')),
                    initial_owner='laptop')  # Adopt the former public endpoint's existing local tasks.
    for node in NODES:
        threading.Thread(target=monitor, args=(router, node), daemon=True).start()
    class DecisionServer(HTTPServer):
        request_queue_size = 128
    snapshot_server = DecisionServer(('100.72.100.10', 18504), handler_for(router, snapshots_only=True))
    threading.Thread(target=snapshot_server.serve_forever, daemon=True).start()
    DecisionServer(("127.0.0.1", 18504), handler_for(router)).serve_forever()


if __name__ == "__main__":
    main()
