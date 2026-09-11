"""Provision only the BLUE-local child entrypoint and audited report helpers."""
from __future__ import annotations

import json
import subprocess

import blue_node


def main() -> None:
    blue_node.config()
    root = blue_node.ROOT
    link = root / "app/.venv"
    target = root / "runtime"
    if link.exists():
        if link.resolve() != target.resolve():
            raise RuntimeError("BLUE .venv exists and is not the dedicated runtime")
    else:
        command = "New-Item -ItemType Junction -Path 'D:\\TakealotBlue\\app\\.venv' -Target 'D:\\TakealotBlue\\runtime' | Out-Null"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if link.resolve() != target.resolve() or not (link / "Scripts/python.exe").is_file():
        raise RuntimeError("BLUE runtime junction verification failed")
    print(json.dumps({"venv": str(link), "target": str(target), "green_changed": False}))


if __name__ == "__main__":
    main()
