"""Run the laptop's isolated read-only blue ERP on TCP 8502."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from takealot_ops.settings import DashboardSettings  # noqa: E402


EXPECTED_COMPUTER = "LAPTOP-2T5MN8EU"
EXPECTED_MYSQL_HOST = "LAPTOP-2T5MN8EU"
BLUE_WEB_PORT = 8502
DEPLOYMENT_LABEL = "blue-laptop"
SESSION_COOKIE_NAME = "takealot_blue_session"
TENCENT_TAILSCALE_IP = "100.72.100.10"
RETIREMENT_MARKER = Path("D:/TakealotHA/blue-green/legacy-blue-retired.json")


class BluePreflightError(RuntimeError):
    """Raised when the laptop blue environment is not safely isolated."""


def _database_role(database_url: str) -> dict[str, object]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT @@hostname, @@server_id, @@global.read_only, "
                    "@@global.super_read_only, @@global.log_bin, "
                    "@@global.log_replica_updates"
                )
            ).one()
    except Exception as exc:
        raise BluePreflightError("Unable to verify the laptop MySQL role.") from exc
    finally:
        engine.dispose()
    return {
        "hostname": str(row[0]),
        "server_id": int(row[1]),
        "read_only": int(row[2]),
        "super_read_only": int(row[3]),
        "log_bin": int(row[4]),
        "log_replica_updates": int(row[5]),
    }


def _assert_database_url(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "mysql":
        raise BluePreflightError("The blue environment requires MySQL.")
    if (url.host or "").casefold() not in {"127.0.0.1", "localhost"}:
        raise BluePreflightError("The blue database must use laptop loopback MySQL.")
    if url.port not in {None, 3306}:
        raise BluePreflightError("The blue database must use local TCP 3306.")
    if (url.database or "").casefold() != "takealot_ops":
        raise BluePreflightError("The blue database must be takealot_ops.")


def _assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise BluePreflightError(f"TCP port {port} is already in use.")


def _prepare_blue_environment() -> dict[str, object]:
    if RETIREMENT_MARKER.exists():
        raise BluePreflightError("Legacy blue has been retired; use the new blue stage on 8503.")
    computer = os.environ.get("COMPUTERNAME", "")
    if computer.casefold() != EXPECTED_COMPUTER.casefold():
        raise BluePreflightError(
            f"Expected computer {EXPECTED_COMPUTER}, got {computer or '<empty>'}."
        )

    os.chdir(PROJECT_ROOT)
    settings = DashboardSettings.from_env(PROJECT_ROOT)
    _assert_database_url(settings.database_url)
    role = _database_role(settings.database_url)
    expected_role = {
        "hostname": EXPECTED_MYSQL_HOST,
        "server_id": 2,
        "read_only": 1,
        "super_read_only": 1,
        "log_bin": 1,
        "log_replica_updates": 1,
    }
    if role != expected_role:
        raise BluePreflightError(
            "Laptop MySQL is not the expected read-only replica: "
            + json.dumps(role, ensure_ascii=True, sort_keys=True)
        )
    _assert_port_free(BLUE_WEB_PORT)

    os.environ["TAKEALOT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    os.environ["TAKEALOT_WEB_ONLY"] = "1"
    os.environ["TAKEALOT_READ_ONLY_TEST_MODE"] = "1"
    os.environ["TAKEALOT_DEPLOYMENT_LABEL"] = DEPLOYMENT_LABEL
    os.environ["TAKEALOT_SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    os.environ["TAKEALOT_COMPETITOR_BROWSER_PROXY"] = ""
    return {
        "status": "ready",
        "computer": computer,
        "deployment": DEPLOYMENT_LABEL,
        "web_port": BLUE_WEB_PORT,
        "web_only": True,
        "read_only_test_mode": True,
        "session_cookie": SESSION_COOKIE_NAME,
        "database": role,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    preflight = _prepare_blue_environment()
    print(json.dumps(preflight, ensure_ascii=True, sort_keys=True), flush=True)
    if args.preflight_only:
        return 0

    import uvicorn

    uvicorn.run(
        "takealot_ops.erp.web:app",
        host="0.0.0.0",
        port=BLUE_WEB_PORT,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=TENCENT_TAILSCALE_IP,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
