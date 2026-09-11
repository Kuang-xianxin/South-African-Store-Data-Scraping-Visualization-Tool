"""Linux-only, isolated real-process smoke test; zero Seller HTTP/MySQL calls."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from blue_seller_authority import CLUSTER
from blue_seller_ledger import initialize


def verify(source: Path) -> dict:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    with tempfile.TemporaryDirectory(prefix="blue-seller-wire-") as directory:
        root = Path(directory)
        (root / "state").mkdir()
        initialize(root / "state/seller-api.sqlite3")
        report = {"cluster": CLUSTER, "version": 1, "epoch": 0, "owner": None, "boundary": None,
                  "reports": {node: {"boot": boot, "seen": time.monotonic(), "facts": {}}
                              for node in ("main", "laptop")}}
        (root / "state/ledger.json").write_text(json.dumps(report))
        (root / "config.json").write_text(json.dumps({"cluster": CLUSTER, "mode": "observe",
            "seller_api": {"version": 1, "enabled": True, "same_account_coverage_verified": True}}))
        # Only these fresh synthetic paths are passed to the server process.
        code = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "import blue_arbiter as a; a.BASE=Path(sys.argv[2]); a.CONFIG=a.BASE/'config.json'; "
                "a.serve(sys.argv[3],seller_only=True)")
        children = []
        checks = 0
        def check(condition):
            nonlocal checks
            checks += 1
            if not condition:
                raise AssertionError("isolated-wire-check-failed")
        def start(node):
            process = subprocess.Popen([sys.executable, "-u", "-c", code, str(source), str(root), node],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            children.append(process)
            check(json.loads(process.stdout.readline())["status"] == "hello")
            return process
        def send(process, action, **fields):
            process.stdin.write(json.dumps({"cluster": CLUSTER, "action": "seller." + action, **fields}) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())
        try:
            main, laptop = start("main"), start("laptop")
            run, request, token = "1" * 32, "2" * 32, "3" * 32
            granted = send(main, "begin_run", run_id=run)
            check(granted["status"] == "run-granted")
            generation = granted["generation"]
            check(send(laptop, "begin_run", run_id="4" * 32)["status"] == "denied")
            args = {"run_id": run, "generation": generation, "request_id": request, "token": token}
            check(send(main, "begin_request", **args)["request_permitted"] is True)
            main.kill()
            main.wait(timeout=5)
            check(send(laptop, "status")["blocked_by_unconfirmed_request"] is True)
            check(send(laptop, "begin_run", run_id="4" * 32)["reason"] == "previous-request-unconfirmed")
            recovered = start("main")
            check(send(recovered, "complete", http_status=200, **args)["status"] == "completed")
            check(send(recovered, "end_run", run_id=run, generation=generation)["status"] == "run-ended")
            check(send(recovered, "complete", http_status=200, **args)["status"] == "already-completed")
            report["reports"]["main"]["seen"] = time.monotonic() - 60
            report["reports"]["laptop"]["seen"] = time.monotonic()
            (root / "state/ledger.json").write_text(json.dumps(report))
            check(send(laptop, "begin_run", run_id="4" * 32)["status"] == "run-granted")
            check(send(recovered, "begin_request", **args)["request_permitted"] is False)
            return {"isolated_process_checks": checks, "status": "passed", "seller_http_calls": 0,
                    "mysql_calls": 0, "production_ledger_used": False}
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]).resolve(strict=True))))
