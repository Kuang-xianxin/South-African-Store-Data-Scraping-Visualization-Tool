"""Protected, explicit conflict-choice artifacts; never connect to production SQL."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path

from blue_branch_apply import open_review
from blue_branch_capture import archive_path
from blue_branch_merge import canonical, readonly


def resolution_template(plan_path: Path, output: Path) -> dict:
    """No preselected conflict answers and no password/session payloads in JSON.

    A template is not an authorization or a verified plan. validate rechecks the
    entire plan against all three sealed snapshots before accepting any choices.
    """
    with closing(readonly(plan_path)) as db:
        db.execute("BEGIN")
        record = db.execute("SELECT header,seal FROM metadata WHERE id=1").fetchone()
        if not record or not record[1]:
            raise ValueError("plan-incomplete")
        header = json.loads(record[0])
        if header.get("version") != 1 or header.get("kind") != "blue-branch-merge-plan":
            raise ValueError("plan-format-invalid")
        choices = [{"table": table, "key": key, "take": None, "reason": ""}
                   for table, key in db.execute("SELECT table_name,key FROM changes WHERE decision='conflict' ORDER BY table_name,key")]
        document = {"version": 1, "plan_seal": record[1], "choices": choices}
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"conflicts": len(choices), "plan_seal": record[1], "template_only": True,
            "verified": False, "mysql_writes": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("resolution-template")
    template.add_argument("--plan", required=True, type=Path)
    template.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("validate")
    for name in ("baseline", "old", "current", "plan"):
        verify.add_argument("--" + name, required=True, type=Path)
    verify.add_argument("--resolutions", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "resolution-template":
            result = resolution_template(archive_path(args.plan), archive_path(args.output, create_parent=True, suffix=".json"))
        else:
            document = (json.loads(archive_path(args.resolutions, suffix=".json").read_text(encoding="utf-8"))
                        if args.resolutions else None)
            with open_review(*(archive_path(getattr(args, name)) for name in ("baseline", "old", "current", "plan")),
                             document) as review:
                result = {"status": "review-validated", "plan_seal": review.plan_seal,
                          "resolution_seal": review.resolution_seal, "result_seal": review.result_seal(),
                          "mysql_writes": 0, "production_apply_available": False}
        print(canonical(result))
        return 0
    except Exception as exc:
        # Neither driver errors nor row values may appear on the shared console.
        print(canonical({"status": "rejected", "error_type": type(exc).__name__, "mysql_writes": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
