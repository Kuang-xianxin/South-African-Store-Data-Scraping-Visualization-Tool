"""Windows process checks for the scheduled health guard's console-free entry."""

import os
import runpy
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_hidden_child_has_no_console_and_preserves_failure(tmp_path: Path) -> None:
    run_hidden = runpy.run_path(
        str(PROJECT_ROOT / "scripts/run_erp_health_guard.pyw")
    )["run_hidden"]
    child = tmp_path / "check_console.py"
    child.write_text(
        "import ctypes, pathlib, sys, time\n"
        "assert ctypes.windll.kernel32.GetConsoleWindow() == 0\n"
        "time.sleep(0.2)\n"
        "pathlib.Path('completed').write_text('done')\n"
        "print('expected test failure', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "output.log"

    result = run_hidden([sys.executable, str(child)], tmp_path, output_path)

    assert result == 7
    assert (tmp_path / "completed").read_text() == "done"
    assert b"expected test failure" in output_path.read_bytes()


def test_launch_failure_is_reported_to_scheduler_and_log(tmp_path: Path) -> None:
    run_hidden = runpy.run_path(
        str(PROJECT_ROOT / "scripts/run_erp_health_guard.pyw")
    )["run_hidden"]
    output_path = tmp_path / "output.log"

    result = run_hidden([str(tmp_path / "missing.exe")], tmp_path, output_path)

    assert result == 1
    assert b"FileNotFoundError" in output_path.read_bytes()
