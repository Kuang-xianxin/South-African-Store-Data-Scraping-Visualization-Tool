"""Verify prepared reads with real, short-lived sessions; retain no credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from takealot_ops.erp.auth import _identity
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.models import ErpSession, ErpUser


def verify(base: str, ready: Path, output: Path) -> None:
    engine = create_engine(DashboardSettings.from_env(Path.cwd()).database_url)
    sessions: dict[int, str] = {}
    hashes: list[str] = []
    results = []
    try:
        with httpx.Client(base_url=base, trust_env=False, timeout=20) as client:
            for scope in json.loads(ready.read_text(encoding="utf-8"))["scopes"]:
                uid = scope["user_id"]
                with Session(engine) as db, db.begin():
                    user = db.get(ErpUser, uid)
                    if user is None or not user.active:
                        raise RuntimeError("Preparation user changed; keep the old service")
                    identity = _identity(db, user)
                    if uid not in sessions:
                        token = secrets.token_urlsafe(32)
                        hashed = hashlib.sha256(token.encode()).hexdigest()
                        now = datetime.now(UTC).replace(tzinfo=None)
                        db.add(ErpSession(token_hash=hashed, user_id=uid,
                            csrf_token=secrets.token_urlsafe(24), created_at=now,
                            expires_at=now + timedelta(minutes=30), last_seen_at=now))
                        sessions[uid] = token
                        hashes.append(hashed)
                allowed = {s.code for s in identity.accessible_stores if s.active and s.data_connected
                           and (scope["scope"] != "operating" or s.id in identity.assigned_store_ids)
                           and (scope["scope"] != "current" or s.code == scope["store"])}
                params = {"page": 1, "page_size": 20, "own_store_scope": scope["scope"], "prefer_cached": "true"}
                if not scope["own"]:
                    params["include_own_store"] = "false"
                for part in ("start", "end"):
                    if scope[part]:
                        params[part + "_date"] = scope[part]
                client.cookies.clear()
                client.cookies.set("takealot_erp_session", sessions[uid])
                started = time.perf_counter()
                response = client.get("/api/competitors/own-store" if scope["own"] else "/api/competitors",
                                      params=params, headers={"X-Store-Code": scope["store"]})
                response.raise_for_status()
                if response.headers.get("x-erp-release-bridge") != "1":
                    raise RuntimeError("Authenticated read did not reach the prepared bridge")
                payload = response.json()
                cards = payload["store_items" if scope["own"] else "items"]
                if scope["own"] and any(q["卖家ID"] not in allowed for card in cards
                        for q in card.get("对比报价", []) if q.get("报价来源") == "seller_api"):
                    raise RuntimeError("Unexpected private store quote in release reader")
                results.append({"user_id": uid, "own": scope["own"], "scope": scope["scope"],
                                "store": scope["store"], "explicit_dates": bool(scope["start"] or scope["end"]),
                                "status": response.status_code, "total": payload["pagination"]["total"],
                                "seconds": round(time.perf_counter() - started, 3),
                                "bridge": response.headers.get("x-erp-release-bridge")})
                output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ErpSession).where(ErpSession.token_hash.in_(hashes)))
        engine.dispose()
    print(f"Verified {len(results)} authenticated prepared scopes at {base}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.base, args.ready, args.output)
