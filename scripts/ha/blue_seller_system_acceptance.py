"""One-shot SYSTEM identity preparation and bounded, read-only SSH acceptance."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, "D:/TakealotBlue")
from blue_seller_runtime import current_sid
from blue_seller_status import inspect
from provision_blue_seller_identity import provision

ROOT = Path("D:/TakealotBlue/state/seller-api")


def main() -> None:
    if current_sid() != "S-1-5-18":
        raise RuntimeError("system-identity-required")
    public = provision()
    (ROOT / "system-identity-public.json").write_text(json.dumps(public), encoding="utf-8")
    deadline = time.monotonic() + 90
    while True:
        try:
            result = inspect()
            if result.get("status") == "ok" or result.get("reason") == "seller-coverage-not-enabled":
                (ROOT / "system-authority-acceptance.json").write_text(json.dumps({
                    "sid": public["sid"], "node": public["node"], "ssh_authenticated": True,
                    "http_calls": 0, "authority": result}), encoding="utf-8")
                return
        except Exception:
            pass  # A public key may still be waiting for the scoped cloud registration.
        if time.monotonic() >= deadline:
            raise RuntimeError("seller-system-acceptance-not-ready")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        (ROOT / "system-authority-acceptance.json").write_text(json.dumps({
            "sid": "S-1-5-18", "ssh_authenticated": False, "error_type": type(exc).__name__, "http_calls": 0}), encoding="utf-8")
        raise SystemExit(1) from None
