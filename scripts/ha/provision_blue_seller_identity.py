"""Create a Seller-only SSH identity for the CURRENT Windows task identity.

Run once as each actual consumer (GREEN user and BLUE SYSTEM). Only the public
key is exported. Existing keys and the database arbiter's ACLs remain untouched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from blue_seller_runtime import ROOT, current_sid


def provision() -> dict:
    import blue_node

    cfg = blue_node.config()
    sid = current_sid()
    directory = ROOT / "secrets/seller-api" / sid
    directory.mkdir(parents=True, exist_ok=True)
    identities = dict.fromkeys((sid, "S-1-5-18", "S-1-5-32-544"))
    subprocess.run(["icacls.exe", str(directory), "/inheritance:r", "/grant:r",
                    *(f"*{value}:(OI)(CI)F" for value in identities)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    key = directory / "id_ed25519"
    if not key.exists():
        subprocess.run(["ssh-keygen.exe", "-q", "-t", "ed25519", "-N", "", "-f", str(key),
                        "-C", f"blue-seller-{cfg['node']}-{sid}"], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    # Derive from the actual existing private key; do not trust a stale .pub file.
    public = subprocess.check_output(["ssh-keygen.exe", "-y", "-f", str(key)],
                                     creationflags=subprocess.CREATE_NO_WINDOW).decode("ascii").strip()
    if not public.startswith("ssh-ed25519 ") or "\n" in public:
        raise RuntimeError("unexpected-seller-key-format")
    return {"version": 1, "cluster": "takealot-blue-3307-v1", "node": cfg["node"],
            "sid": sid, "public_key": public, "scope": "seller-only"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = provision()
        if args.output:
            target = args.output.resolve()
            if not target.is_relative_to((ROOT / "state/seller-api").resolve()) or target.suffix != ".json":
                raise ValueError("public-receipt-path-outside-blue-state")
            target.write_text(json.dumps(result), encoding="utf-8")
        else:
            print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
