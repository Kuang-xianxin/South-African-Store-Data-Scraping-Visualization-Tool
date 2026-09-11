"""Writable BLUE application settings and fail-closed database isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import blue_node
import blue_test_db


INTEGRATION_NAMES = frozenset({
    "TAKEALOT_API_KEY", "TAKEALOT_STORES", "TAKEALOT_BASE_URL",
    "W8_API_TOKEN", "W8_BASE_URL", "DASHSCOPE_API_KEY", "ARK_API_KEY",
    "TAKEALOT_SEARCH_QWEN_MODEL", "TAKEALOT_SEARCH_QWEN_BASE_URL",
    "TAKEALOT_SEARCH_DOUBAO_MODEL", "TAKEALOT_SEARCH_DOUBAO_BASE_URL",
})


def local_proxy(node: str) -> str:
    # Main Windows proxy is Xuelian 7890; laptop uses its independent Clash 7897.
    return {"main": "socks5://127.0.0.1:7890", "laptop": "socks5://127.0.0.1:7897"}[node]


def allowed_integration(name: str) -> bool:
    return name in INTEGRATION_NAMES or bool(re.fullmatch(r"TAKEALOT_STORE_\d{2}_API_KEY", name))


def save_integrations(values: dict[str, str]) -> dict[str, object]:
    blue_node.config()
    if any(not allowed_integration(name) for name in values):
        raise RuntimeError("Unapproved BLUE integration setting")
    target = blue_node.ROOT / "secrets/blue-integrations.dpapi"
    if target.exists():
        raise RuntimeError("BLUE integration settings already exist; refusing overwrite")
    target.write_bytes(blue_node.protect_secret(json.dumps(values).encode()))
    return {"keys": sorted(values), "plaintext_written": False}


def integration_values() -> dict[str, str]:
    values = json.loads(blue_node.protect_secret(
        (blue_node.ROOT / "secrets/blue-integrations.dpapi").read_bytes(), decrypt=True))
    if any(not allowed_integration(name) for name in values):
        raise RuntimeError("Unexpected BLUE integration secret scope")
    return values


def database_url(*, worker: bool = False) -> str:
    from sqlalchemy.engine import URL

    cfg = blue_node.config()
    user = f"blue_{'worker' if worker else 'web'}_{cfg['node']}"
    return URL.create(
        "mysql+pymysql", username=user, password=blue_test_db.credentials()[user],
        host="127.0.0.1", port=3307 if cfg["node"] == "main" else 13317,
        database="takealot_ops", query={
            "charset": "utf8mb4", "ssl_ca": str(blue_node.ROOT / "secrets/blue-primary-ca.pem"),
            "ssl_check_hostname": "true", "connect_timeout": "8",
            **({"read_timeout": "10", "write_timeout": "10"} if worker else {}),
        },
    ).render_as_string(hide_password=False)


def validate_dbapi_connection(conn: object, _: object) -> None:
    """Every pooled physical connection must be verified before application SQL."""
    if type(conn).__module__ != "pymysql.connections":
        raise RuntimeError("BLUE forbids SQLite and alternative database fallbacks")
    if getattr(conn, "_blue_read_role", None) == "replica":
        from blue_read_replica import validate_replica

        validate_replica(conn)
        conn.rollback()
        return
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid,"
                       "@@global.read_only,@@global.super_read_only")
        blue_test_db.validate_primary(cursor.fetchone(), writable=True)
        cursor.execute("SELECT environment,seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
        seed = json.loads((blue_node.ROOT / "state/seed.json").read_text(encoding="utf-8"))
        if cursor.fetchone() != ("blue", seed["seed_sha256"]):
            raise RuntimeError("BLUE seed identity mismatch")
    conn.rollback()


def install_database_guard() -> None:
    from sqlalchemy import Engine, event

    if not event.contains(Engine, "connect", validate_dbapi_connection):
        event.listen(Engine, "connect", validate_dbapi_connection)


def prepare_environment(*, worker: bool = False, allow_offline_spool: bool = False) -> dict[str, object]:
    cfg = blue_node.config()
    root = blue_node.ROOT / "app"
    if (root / ".env").exists():
        raise RuntimeError("BLUE cannot inherit a project .env")
    activation = json.loads((blue_node.ROOT / "state/writable-test.json").read_text(encoding="utf-8"))
    if activation != {"mode": "fixed-primary-blue-test", "primary_server_id": 101, "automatic_failover": False}:
        raise RuntimeError("Unapproved BLUE activation mode")
    user = f"blue_{'worker' if worker else 'web'}_{cfg['node']}"
    if allow_offline_spool:
        from blue_crawl_journal import enabled

        if not worker or not enabled(blue_node.ROOT):
            raise RuntimeError("Offline startup is only for the approved BLUE result spool")
    else:
        with blue_test_db.primary_connection(user, writable=True):
            pass
    for name in list(os.environ):
        if name.startswith(("TAKEALOT_", "W8_")) or name in {"DASHSCOPE_API_KEY", "ARK_API_KEY"}:
            os.environ.pop(name)
    os.environ.update(integration_values())
    os.environ.update({
        "TAKEALOT_PROJECT_ROOT": str(root), "TAKEALOT_DATABASE_URL": database_url(worker=worker),
        "TAKEALOT_WEB_ONLY": "0", "TAKEALOT_READ_ONLY_TEST_MODE": "0",
        "TAKEALOT_DEPLOYMENT_LABEL": f"blue-stage-{cfg['node']}",
        "TAKEALOT_SESSION_COOKIE_NAME": "takealot_blue_stage_session", "TAKEALOT_DASHBOARD_PORT": "8503",
        "TAKEALOT_COMPETITOR_BROWSER_PROXY": local_proxy(cfg["node"]),
        # BLUE never borrows real seller-browser sessions or enables upstream writes.
        "TAKEALOT_PORTAL_BFF_ENABLED": "0", "TAKEALOT_PORTAL_ENABLED_STORES": "",
        "TAKEALOT_BLUE_CHILD_GUARD": "1",
        "TAKEALOT_MYSQL_BIN": str(cfg["mysql_bin"]),
        "PYTHONUTF8": "1",
        "PYTHONPATH": os.pathsep.join((str(blue_node.ROOT / "child-guard"), str(root / "src"), str(blue_node.ROOT))),
    })
    mysql_bin = str(cfg["mysql_bin"])
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([mysql_bin, *(p for p in path_entries if p.casefold() != mysql_bin.casefold())])
    install_database_guard()
    os.chdir(root)
    import sys

    sys.path.insert(0, str(root / "src"))
    import takealot_ops

    if not Path(takealot_ops.__file__).resolve().is_relative_to(root / "src"):
        raise RuntimeError("BLUE imported code outside its isolated release")
    if (blue_node.ROOT / "state/codex-runtime.json").is_file() and not worker:
        from blue_codex_runtime import install

        install()
    return {"node": cfg["node"], "port": 8503, "mode": "fixed-primary-blue-test",
            "primary_server_id": 101, "replica_server_id": 102, "automatic_failover": False}


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["import-integrations"]:
        print(json.dumps(save_integrations(json.loads(sys.stdin.read()))))
    else:
        raise SystemExit("Only scoped integration import is supported")
