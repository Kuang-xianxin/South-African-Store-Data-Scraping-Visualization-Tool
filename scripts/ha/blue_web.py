"""Serve the new isolated blue snapshot for validation, never the green database."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import blue_node


def proxy_options(node: str) -> dict[str, object]:
    # Only the TLS-terminating Tencent peer may assert HTTPS on the laptop.
    # Do not trust loopback: the legacy 8502 TCP alias also arrives from there.
    if node == "laptop":
        return {"proxy_headers": True, "forwarded_allow_ips": "100.72.100.10"}
    return {"proxy_headers": False}


def prepare_environment() -> dict[str, object]:
    if (blue_node.ROOT / "state/writable-test.json").exists():
        from blue_runtime import prepare_environment as prepare_writable

        return prepare_writable()
    from sqlalchemy.engine import URL

    cfg = blue_node.config()
    root = blue_node.ROOT / "app"
    if (root / ".env").exists():
        raise RuntimeError("Blue stage must not inherit a project .env")
    seed = json.loads((blue_node.ROOT / "state/seed.json").read_text(encoding="utf-8"))
    with blue_node.connection("blue_app") as conn, conn.cursor() as cursor:
        cursor.execute("SELECT seed_sha256,environment FROM takealot_ops.blue_environment_marker "
                       "WHERE id=1")
        if cursor.fetchone() != (seed["seed_sha256"], "blue"):
            raise RuntimeError("Blue seed marker mismatch")
    for name in list(os.environ):
        if name.startswith(("TAKEALOT_", "W8_")):
            os.environ.pop(name)
    os.environ.update({
        "TAKEALOT_PROJECT_ROOT": str(root),
        "TAKEALOT_DATABASE_URL": URL.create(
            "mysql+pymysql", username="blue_app",
            password=blue_node.protected_credentials()["blue_app"],
            host="127.0.0.1", port=3307, database="takealot_ops",
        ).render_as_string(hide_password=False),
        "TAKEALOT_WEB_ONLY": "1",
        "TAKEALOT_READ_ONLY_TEST_MODE": "1",
        "TAKEALOT_DEPLOYMENT_LABEL": f"blue-stage-{cfg['node']}",
        "TAKEALOT_SESSION_COOKIE_NAME": "takealot_blue_stage_session",
        "TAKEALOT_DASHBOARD_PORT": "8503",
        "TAKEALOT_COMPETITOR_BROWSER_PROXY": "",
    })
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    import takealot_ops

    if not Path(takealot_ops.__file__).resolve().is_relative_to(root / "src"):
        raise RuntimeError("Blue application imported code outside its isolated release")
    return {"node": cfg["node"], "port": 8503, "mode": "isolated-read-only-snapshot"}


if __name__ == "__main__":
    state = prepare_environment()
    routing = (blue_node.ROOT / "state/web-routing-enabled.json").exists()
    if routing:
        mode = json.loads((blue_node.ROOT / "state/web-routing-enabled.json").read_text())
        if mode != {"version": 1, "web_routing": True, "replica_reads": True, "crawler_policy_changed": False}:
            raise RuntimeError("Unapproved BLUE web routing mode")
        if state["node"] == "laptop":
            os.environ["TAKEALOT_BLUE_READ_REPLICA"] = "1"
    print(json.dumps(state), flush=True)
    import uvicorn

    # Laptop inbound TCP 8503 is restricted by the dedicated Windows firewall rule.
    # Main stays loopback-only behind its existing Tailscale Serve profile.
    bind = "0.0.0.0" if state["node"] == "laptop" else "127.0.0.1"
    from takealot_ops.erp.web import app

    if (blue_node.ROOT / "state/shared-crawl-enabled.json").exists():
        shared_mode = json.loads((blue_node.ROOT / "state/shared-crawl-enabled.json").read_text(encoding="utf-8"))
        if shared_mode != {"version": 2, "mode": "small-shared-crawl", "max_targets": 20,
                           "legacy_crawl_disabled": False, "scheduled_crawl_disabled": True,
                           "automatic_failover": False}:
            raise RuntimeError("Unapproved BLUE shared crawler mode")
        from blue_shared_crawl import install
        from takealot_ops.storage.migrations import create_engine_for_database_url
        import blue_runtime

        install(app, blue_node.ROOT, create_engine_for_database_url(blue_runtime.database_url()))
    if routing:
        from blue_web_capacity import install as install_capacity

        install_capacity(app, blue_node.ROOT, str(state["node"]))
    uvicorn.run(app, host=bind, port=8503,
                access_log=False, **proxy_options(str(state["node"])))
