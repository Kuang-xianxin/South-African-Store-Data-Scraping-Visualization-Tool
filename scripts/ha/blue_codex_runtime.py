"""BLUE-only Codex identity, child environment and shared MySQL quota state."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path


ROOT = Path("D:/TakealotBlue")
_installed = False


def child_environment() -> dict[str, str]:
    import blue_node

    node = blue_node.config()["node"]
    home = ROOT / "secrets/codex-home"
    if not (home / "auth.json").is_file():
        raise RuntimeError("BLUE Codex login has not been prepared")
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    # Only the model subprocess receives these proxies. ERP integrations retain
    # their existing network policy and never inherit the desktop Codex config.
    port = {"main": 7890, "laptop": 7897}[node]
    proxy = f"http://127.0.0.1:{port}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment[key] = proxy
    for key in ("ALL_PROXY", "all_proxy"):
        environment.pop(key, None)
    environment["NO_PROXY"] = environment["no_proxy"] = "localhost,127.0.0.1,::1"
    return environment


@contextmanager
def quota_transaction(*, write: bool):
    import blue_node
    import blue_test_db

    node = blue_node.config()["node"]
    with blue_test_db.primary_connection(f"blue_web_{node}", writable=write) as conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute("START TRANSACTION" if write else "START TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT payload FROM takealot_ops.blue_codex_quota WHERE id=1"
                    + (" FOR UPDATE" if write else "")
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("BLUE shared Codex budget has not been initialized")
                state = json.loads(row[0]) if row[0] is not None else None
                if state is not None and not isinstance(state, dict):
                    raise RuntimeError("BLUE shared Codex budget is invalid")

                def save(payload):
                    if not write:
                        raise RuntimeError("Cannot modify budget in a read-only transaction")
                    cursor.execute(
                        "UPDATE takealot_ops.blue_codex_quota SET payload=%s WHERE id=1",
                        (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
                    )

                yield state, save
            conn.commit() if write else conn.rollback()
        except BaseException:
            conn.rollback()
            raise


def install() -> None:
    """Install before importing search services; never touch GREEN processes."""
    global _installed
    if _installed:
        return
    import takealot_ops.search_ranking.codex_cli as cli

    class SharedQuotaGuard(cli.CodexWeeklyQuotaGuard):
        def status(self):
            with quota_transaction(write=False) as (state, _):
                return state

        def observe(self, window):
            # The first observer of a new weekly window establishes one baseline
            # for both nodes. A later node cannot create another ten-point budget.
            with quota_transaction(write=True) as (state, save):
                payload = self.evaluate(window, state)
                save(payload)
                return payload

    class BlueAppServerClient(cli.CodexAppServerClient):
        def __init__(self, *args, **kwargs):
            kwargs["subprocess_env"] = child_environment()
            super().__init__(*args, **kwargs)

    cli.CodexWeeklyQuotaGuard = SharedQuotaGuard
    cli.CodexAppServerClient = BlueAppServerClient
    # Importing the package may already have bound these names in service.py.
    import takealot_ops.search_ranking.service as service
    import takealot_ops.search_ranking.title_optimization as title_optimization

    for consumer in (service, title_optimization):
        consumer.CodexWeeklyQuotaGuard = SharedQuotaGuard
        consumer.CodexAppServerClient = BlueAppServerClient
    _installed = True
