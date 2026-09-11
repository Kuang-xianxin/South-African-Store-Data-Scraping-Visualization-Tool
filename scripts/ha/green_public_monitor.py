"""Read-only cloud-to-main latency checks; no login, collection, or repair actions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

ORIGIN = "https://119.91.117.232"
STATE_ROOT = Path("/var/lib/takealot-green-monitor")
PERFORMANCE_LOG = Path("/var/log/nginx/takealot-green-perf.jsonl")
RADAR_PATHS = {"/api/competitors", "/api/competitors/own-store"}


def probe(name: str, path: str, slow_seconds: float) -> tuple[dict, bytes]:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]*", path) or ".." in path:
        raise ValueError("Probe path is outside the fixed ERP origin")
    with tempfile.TemporaryDirectory(prefix="green-monitor-") as directory:
        output = Path(directory) / "body"
        command = [
            "curl", "--noproxy", "*", "--silent", "--show-error",
            "--connect-timeout", "3", "--max-time", "10", "--max-filesize", "2097152",
            "--resolve", "119.91.117.232:443:10.1.0.12", "--output", str(output),
            "--write-out", "%{http_code} %{time_total} %{content_type}", ORIGIN + path,
        ]
        start = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=12)
            parts = result.stdout.split(maxsplit=2)
            status = int(parts[0]) if parts else 0
            seconds = float(parts[1]) if len(parts) > 1 else time.monotonic() - start
            mime = parts[2].strip() if len(parts) > 2 else ""
            error = "" if result.returncode == 0 else f"curl_exit_{result.returncode}"
        except subprocess.TimeoutExpired:
            status, seconds, mime, error = 0, time.monotonic() - start, "", "probe_timeout"
        body = output.read_bytes() if output.exists() else b""
    return {
        "name": name, "path": path, "status": status, "seconds": round(seconds, 3),
        "bytes": len(body), "mime": mime, "error": error, "slow_seconds": slow_seconds,
    }, body


def recent_requests(now: float) -> list[dict]:
    if not PERFORMANCE_LOG.exists():
        return []
    with PERFORMANCE_LOG.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 2_000_000))
        lines = stream.read().splitlines()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            age = now - float(row["timestamp"])
            if 0 <= age <= 90:
                rows.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def classify(probes: list[dict], requests: list[dict]) -> tuple[str, list[str]]:
    reasons = []
    down = False
    for row in probes:
        if row["error"] or row["status"] != 200:
            down = True
            reasons.append(f"{row['name']}:{row['error'] or row['status']}")
        elif row["seconds"] > row["slow_seconds"]:
            reasons.append(f"{row['name']}:slow:{row['seconds']}s")
    for row in requests:
        if row.get("path") not in RADAR_PATHS:
            continue
        if int(row.get("status", 0)) >= 500:
            reasons.append(f"{row['path']}:HTTP{row['status']}")
        elif float(row.get("seconds", 0)) > 8:
            reasons.append(f"{row['path']}:slow:{row['seconds']}s")
    if any("100.70.103.11:8501" in str(row.get("upstream", "")) for row in requests):
        reasons.append("using_tailscale_backup")
    return ("down" if down else "degraded" if reasons else "healthy"), sorted(set(reasons))


def advance(previous: dict, condition: str, reasons: list[str], now: float) -> dict:
    candidate_count = previous.get("candidate_count", 0) + 1 if previous.get("candidate") == condition else 1
    state = previous.get("state", "healthy")
    sequence = previous.get("sequence", 0)
    events = list(previous.get("events", []))
    # Require two samples for both an alarm and a recovery; suppress single spikes.
    if candidate_count >= 2 and condition != state:
        state = condition
        sequence += 1
        events.append({"id": sequence, "time": now, "state": state, "reasons": reasons})
    return {
        "observed_at": now, "state": state, "sample_condition": condition,
        "candidate": condition, "candidate_count": min(candidate_count, 1000),
        "sequence": sequence, "events": events[-60:], "reasons": reasons,
    }


def collect() -> tuple[list[dict], list[dict]]:
    probes = []
    home, html = probe("homepage", "/", 3)
    probes.append(home)
    health, health_body = probe("health", "/api/health", 3)
    try:
        health_data = json.loads(health_body)
        if health_data.get("status") != "ok" or health_data.get("application") != "takealot-erp":
            health["error"] = "wrong_health_payload"
    except (ValueError, AttributeError):
        health["error"] = health["error"] or "invalid_health_payload"
    probes.append(health)
    main_match = re.search(rb'<script[^>]+src="(/assets/[A-Za-z0-9_-]+\.js)"', html)
    if main_match:
        main, main_body = probe("main_js", main_match[1].decode(), 5)
        if "javascript" not in main["mime"]:
            main["error"] = main["error"] or "wrong_javascript_mime"
        probes.append(main)
        radar_match = re.search(rb'(?:assets/)?(CompetitorsPage-[A-Za-z0-9_-]+\.js)', main_body)
        if radar_match:
            radar, _ = probe("radar_js", "/assets/" + radar_match[1].decode(), 5)
            if "javascript" not in radar["mime"]:
                radar["error"] = radar["error"] or "wrong_javascript_mime"
            probes.append(radar)
        else:
            probes.append({"name": "radar_js", "status": 0, "error": "asset_not_found", "seconds": 0, "slow_seconds": 5})
    else:
        probes.append({"name": "main_js", "status": 0, "error": "asset_not_found", "seconds": 0, "slow_seconds": 5})
    return probes, recent_requests(time.time())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Print current measurements without changing monitor state")
    args = parser.parse_args()
    probes, requests = collect()
    condition, reasons = classify(probes, requests)
    if args.once:
        print(json.dumps({"condition": condition, "reasons": reasons, "probes": probes}))
        return
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATE_ROOT / "status.json"
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    current = advance(previous, condition, reasons, time.time())
    current["probes"] = probes
    current["scope"] = "Tencent HTTPS entry through reverse tunnel to production main; excludes client ISP latency"
    radar_requests = [row for row in requests if row.get("path") in RADAR_PATHS]
    current["radar_request_count_90s"] = len(radar_requests)
    current["radar_max_seconds_90s"] = max((float(row["seconds"]) for row in radar_requests), default=None)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=2) + "\n")
    temporary.replace(path)
    if current["sequence"] != previous.get("sequence", 0):
        print(json.dumps(current["events"][-1]), flush=True)


if __name__ == "__main__":
    main()
