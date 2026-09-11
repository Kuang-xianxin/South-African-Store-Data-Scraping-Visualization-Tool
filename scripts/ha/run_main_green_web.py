"""Run a temporary web-only ERP on the main computer against the laptop primary."""

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
from sqlalchemy.engine import URL, make_url  # noqa: E402

from takealot_ops.settings import DashboardSettings  # noqa: E402


EXPECTED_COMPUTER = "DESKTOP-NTRMANG"
EXPECTED_MAIN_HOST = "DESKTOP-NTRMANG"
EXPECTED_LAPTOP_HOST = "LAPTOP-2T5MN8EU"
LOCAL_MYSQL_PORT = 3306
LAPTOP_TUNNEL_PORT = 13307
GREEN_WEB_PORT = 8501
DEPLOYMENT_LABEL = "green-main"


class GreenPreflightError(RuntimeError):
    """Raised when the green instance cannot be started safely."""


def _role_state(database_url: str) -> dict[str, object]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT CURRENT_USER(), @@hostname, @@server_id, "
                    "@@global.read_only, @@global.super_read_only, "
                    "@@global.log_bin, @@global.log_replica_updates"
                )
            ).one()
    except Exception as exc:
        raise GreenPreflightError("Database role preflight could not connect.") from exc
    finally:
        engine.dispose()
    return {
        "current_user": str(row[0]),
        "hostname": str(row[1]),
        "server_id": int(row[2]),
        "read_only": int(row[3]),
        "super_read_only": int(row[4]),
        "log_bin": int(row[5]),
        "log_replica_updates": int(row[6]),
    }


def _green_database_url(local_database_url: str) -> str:
    url: URL = make_url(local_database_url)
    if url.get_backend_name() != "mysql":
        raise GreenPreflightError("The green bridge requires the configured MySQL database.")
    if url.host not in {"127.0.0.1", "localhost"}:
        raise GreenPreflightError("The configured main database is not loopback-only.")
    if url.port not in {None, LOCAL_MYSQL_PORT}:
        raise GreenPreflightError("The configured main database is not on port 3306.")
    green_url = url.set(host="127.0.0.1", port=LAPTOP_TUNNEL_PORT)
    return green_url.render_as_string(hide_password=False)


def _assert_role(
    role: dict[str, object],
    *,
    expected_host: str,
    expected_server_id: int,
    expected_read_only: int,
) -> None:
    expected = {
        "hostname": expected_host,
        "server_id": expected_server_id,
        "read_only": expected_read_only,
        "super_read_only": expected_read_only,
        "log_bin": 1,
        "log_replica_updates": 1,
    }
    observed = {key: role[key] for key in expected}
    if observed != expected:
        raise GreenPreflightError(
            "Database role safety check failed: "
            + json.dumps(observed, ensure_ascii=True, sort_keys=True)
        )


def _assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise GreenPreflightError(f"TCP port {port} is already in use.")


def _prepare_green_environment() -> dict[str, object]:
    computer = os.environ.get("COMPUTERNAME", "")
    if computer.casefold() != EXPECTED_COMPUTER.casefold():
        raise GreenPreflightError(
            f"Expected computer {EXPECTED_COMPUTER}, got {computer or '<empty>'}."
        )

    os.chdir(PROJECT_ROOT)
    local_settings = DashboardSettings.from_env(PROJECT_ROOT)
    if local_settings.dashboard_port != GREEN_WEB_PORT:
        raise GreenPreflightError(
            f"Expected dashboard port {GREEN_WEB_PORT}, got {local_settings.dashboard_port}."
        )
    local_role = _role_state(local_settings.database_url)
    _assert_role(
        local_role,
        expected_host=EXPECTED_MAIN_HOST,
        expected_server_id=1,
        expected_read_only=1,
    )

    green_database_url = _green_database_url(local_settings.database_url)
    primary_role = _role_state(green_database_url)
    _assert_role(
        primary_role,
        expected_host=EXPECTED_LAPTOP_HOST,
        expected_server_id=2,
        expected_read_only=0,
    )
    _assert_port_free(GREEN_WEB_PORT)

    os.environ["TAKEALOT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    os.environ["TAKEALOT_DATABASE_URL"] = green_database_url
    os.environ["TAKEALOT_BACKUP_DATABASE_URL"] = ""
    os.environ["TAKEALOT_WEB_ONLY"] = "1"
    os.environ["TAKEALOT_DEPLOYMENT_LABEL"] = DEPLOYMENT_LABEL
    return {
        "status": "ready",
        "computer": computer,
        "deployment": DEPLOYMENT_LABEL,
        "web_port": GREEN_WEB_PORT,
        "web_only": True,
        "local_database": local_role,
        "primary_database": primary_role,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    preflight = _prepare_green_environment()
    print(json.dumps(preflight, ensure_ascii=True, sort_keys=True), flush=True)
    if args.preflight_only:
        return 0

    import uvicorn

    uvicorn.run(
        "takealot_ops.erp.web:app",
        host="0.0.0.0",
        port=GREEN_WEB_PORT,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
