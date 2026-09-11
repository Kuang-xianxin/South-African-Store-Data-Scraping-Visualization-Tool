"""Temporarily route GREEN reads through an already prepared local standby.

Only the existing GREEN HTTPS config is edited. Write methods keep their normal
upstream; they are never mirrored or retried by this helper. Compare-and-restore
prevents overwriting an operator's concurrent proxy change.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

CONFIG = "/etc/nginx/conf.d/takealot-erp-zz-public.conf"
SSH = ["ssh", "-T", "-i", str(Path.home() / ".ssh/takealot_tencent_witness_ed25519"),
       "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=8", "ubuntu@119.91.117.232"]


def bridge_config(original: str, remote_port: int) -> str:
    needle = "proxy_pass http://takealot_erp_ha;"
    if original.count(needle) != 1 or "takealot_release_read" in original:
        raise RuntimeError("Unexpected GREEN proxy config; no change made")
    prefix = (f"upstream takealot_release_read {{ server 127.0.0.1:{remote_port}; }}\n"
              "map $request_method:$uri $takealot_release_upstream {\n"
              "    default takealot_erp_ha;\n"
              "    ~^(GET|HEAD):/(?:$|index\\.html$|assets/|api/(?:health$|auth/(?:session|status)$|competitors(?:$|/(?:own-store|date-range)$))) takealot_release_read;\n}\n")
    return prefix + original.replace(needle, "proxy_pass http://$takealot_release_upstream;")


def remote(script: str) -> str:
    result = subprocess.run([*SSH, "sudo -n python3 -"], input=script, text=True,
                            capture_output=True, check=True, timeout=35)
    return result.stdout


def install(expected: str, replacement: str) -> None:
    # Arguments are JSON literals in Python input, never interpolated shell code.
    remote(f"""import base64, hashlib, os, pathlib, subprocess
p = pathlib.Path({CONFIG!r})
old = p.read_bytes()
assert hashlib.sha256(old).hexdigest() == {expected!r}, 'GREEN config changed concurrently'
new = base64.b64decode({base64.b64encode(replacement.encode()).decode()!r})
temp = p.with_suffix('.release-tmp')
temp.write_bytes(new)
os.chmod(temp, 0o644)
temp.replace(p)
try:
    subprocess.run(['/usr/sbin/nginx', '-t'], check=True, capture_output=True)
    subprocess.run(['/usr/sbin/nginx', '-s', 'reload'], check=True, capture_output=True)
except BaseException:
    temp.write_bytes(old)
    os.chmod(temp, 0o644)
    temp.replace(p)
    subprocess.run(['/usr/sbin/nginx', '-s', 'reload'], check=True, capture_output=True)
    raise
print('GREEN proxy switched')
""")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("activate", "restore"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--remote-port", type=int, default=18507)
    args = parser.parse_args()
    if args.action == "activate":
        if args.state.exists():
            raise RuntimeError("A bridge state already exists; restore or inspect it first")
        original = remote(f"import pathlib; print(pathlib.Path({CONFIG!r}).read_text(), end='')")
        changed = bridge_config(original, args.remote_port)
        state = {"original": original, "installed_sha256": hashlib.sha256(changed.encode()).hexdigest()}
        args.state.write_text(json.dumps(state), encoding="utf-8")
        # The public entry must never be switched to an unconnected tunnel.
        remote(f"import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:{args.remote_port}/api/health', timeout=5); assert r.headers.get('X-ERP-Release-Bridge') == '1'")
        install(hashlib.sha256(original.encode()).hexdigest(), changed)
    else:
        restored = args.state.with_suffix(".restored.json")
        state = json.loads((args.state if args.state.exists() else restored).read_text(encoding="utf-8"))
        current_hash = remote(f"import pathlib,hashlib; print(hashlib.sha256(pathlib.Path({CONFIG!r}).read_bytes()).hexdigest())").strip()
        original_hash = hashlib.sha256(state["original"].encode()).hexdigest()
        if current_hash != original_hash:
            install(state["installed_sha256"], state["original"])
        if args.state.exists():
            args.state.replace(restored)
    print(args.action + " complete")


if __name__ == "__main__":
    main()
