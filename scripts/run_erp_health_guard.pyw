"""Run the existing ERP health check without allocating a Windows console."""

from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path


def run_hidden(command: list[str], project_root: Path, output_path: Path) -> int:
    """Wait for the check and preserve its exit code for Task Scheduler retries."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("ab") as output:
        try:
            return subprocess.run(
                command,
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            ).returncode
        except OSError:
            output.write(traceback.format_exc().encode("utf-8"))
            return 1


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return run_hidden(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project_root / "scripts/ensure_erp_started.ps1"),
            "-QuietWhenHealthy",
        ],
        project_root,
        project_root / "logs/erp-health-guard.stderr.log",
    )


if __name__ == "__main__":
    raise SystemExit(main())
