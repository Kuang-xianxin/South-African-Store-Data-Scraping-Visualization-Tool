"""Real BLUE HTTP checks using a temporary, scoped BLUE-only operator account."""
from __future__ import annotations

import json
import secrets
import time
from uuid import uuid4

import httpx

import blue_node
import blue_runtime
import blue_test_db


def main() -> None:
    blue_test_db.require_main()
    blue_runtime.prepare_environment()
    from takealot_ops.erp.auth import AuthManager

    auth = AuthManager(blue_node.ROOT / "app")
    username = "blue-test-" + uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    user = auth.create_user(username=username, display_name="BLUE temporary acceptance",
                            password=password, role="admin", all_stores=True)
    evidence = []
    library_id = None
    target = blue_node.ROOT / "state/web-acceptance.json"

    def check(client, method: str, path: str, **kwargs):
        started = time.perf_counter()
        response = client.request(method, path, **kwargs)
        row = {"entry": str(client.base_url), "method": method, "path": path,
               "status": response.status_code, "seconds": round(time.perf_counter() - started, 3)}
        evidence.append(row)
        target.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(row), flush=True)
        response.raise_for_status()
        return response

    try:
        with httpx.Client(base_url="http://127.0.0.1:8503", trust_env=False, timeout=180) as main_client, \
             httpx.Client(base_url="http://192.168.110.13:8502", trust_env=False, timeout=180) as laptop_client:
            result = check(main_client, "POST", "/api/auth/login", json={"username": username, "password": password}).json()
            main_client.headers["X-CSRF-Token"] = result["csrf_token"]
            token = main_client.cookies.get("takealot_blue_stage_session")
            assert token, "BLUE session cookie missing"
            laptop_client.cookies.set("takealot_blue_stage_session", token)
            laptop_client.headers["X-CSRF-Token"] = result["csrf_token"]
            assert check(laptop_client, "GET", "/api/auth/session").json()["user"]["id"] == user["id"]
            library = check(main_client, "POST", "/api/competitors/personal-watchlist/libraries",
                            json={"name": "BLUE test " + username}).json()["library"]
            library_id = library["id"]
            check(laptop_client, "PATCH", f"/api/competitors/personal-watchlist/libraries/{library_id}",
                  json={"name": "BLUE updated " + username})
            with blue_test_db.primary_connection("blue_web_main", writable=True) as conn, conn.cursor() as q:
                q.execute("SELECT name FROM takealot_ops.personal_watchlist_libraries WHERE id=%s AND user_id=%s", (library_id, user["id"]))
                assert q.fetchone() == ("BLUE updated " + username,), "HTTP modification not persisted in BLUE"
            check(main_client, "DELETE", f"/api/competitors/personal-watchlist/libraries/{library_id}")
            library_id = None
            for path in (
                "/api/auth/users", "/api/auth/stores", "/api/erp/summary", "/api/erp/summary/stores",
                "/api/erp/keyword-traffic", "/api/erp/search-ranking", "/api/erp/quadrants",
                "/api/erp/anomaly-products", "/api/erp/returns", "/api/erp/logistics",
                "/api/erp/container-selection", "/api/competitors", "/api/competitors/own-store",
                "/api/erp/refresh-status", "/api/competitors/batch-status",
            ):
                check(main_client, "GET", path)
            for path in ("/api/erp/summary", "/api/erp/logistics", "/api/competitors/targets"):
                check(laptop_client, "GET", path)
            check(main_client, "POST", "/api/auth/logout")
            assert laptop_client.get("/api/auth/session").status_code == 401, "BLUE logout did not invalidate cross-node session"
            evidence.append({"check": "cross-node logout", "passed": True})
    finally:
        # Preserve the audit identity, but make the temporary test account unusable.
        auth.update_user(user["id"], active=False)
        auth.close()
        evidence.append({"temporary_user_id": user["id"], "active": False, "pending_library_cleanup": library_id})
        target.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
