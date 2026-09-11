from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
apply = importlib.import_module("blue_branch_apply")
merge = importlib.import_module("blue_branch_merge")
review_tool = importlib.import_module("blue_branch_review")
capture = importlib.import_module("blue_branch_capture")


@pytest.fixture
def review_files(tmp_path):
    schema = {"sale_items": {"columns": ["id", "value"], "primary_key": ["id"], "foreign_keys": []}}
    paths = [tmp_path / (name + ".sqlite3") for name in ("base", "old", "current", "plan")]
    baseline = None
    for index, value in enumerate(("base", "old secret payload", "current")):
        identity = {"cluster": merge.CLUSTER, "role": "branch" if index else "baseline", "epoch": 7,
                    "node": "main" if index == 1 else "laptop", "seed_sha256": "a" * 64, "gtid": "fixture"}
        if index:
            identity["baseline_seal"] = baseline
        writer = merge.SnapshotWriter(paths[index], identity, schema)
        try:
            writer.append("sale_items", (1, value))
            seal = writer.seal()
            if not index:
                baseline = seal
        finally:
            writer.close()
    merge.plan(*paths)
    output = tmp_path / "resolution.json"
    review_tool.resolution_template(paths[-1], output)
    return paths, output


def test_template_has_no_preselected_answer_or_row_values(review_files):
    paths, output = review_files
    text = output.read_text(encoding="utf-8")
    assert "secret payload" not in text
    document = json.loads(text)
    assert document["choices"][0]["take"] is None
    assert document["choices"][0]["reason"] == ""
    with pytest.raises(apply.MergeRejected, match="invalid-resolution"):
        with apply.open_review(*paths, document):
            pass
    with pytest.raises(FileExistsError):
        review_tool.resolution_template(paths[-1], output)


def resolved(output):
    document = json.loads(output.read_text(encoding="utf-8"))
    document["choices"][0].update(take="current", reason="Retain the reviewed current edit")
    return document


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(plan_seal="b" * 64),
    lambda value: value["choices"].append(value["choices"][0].copy()),
    lambda value: value["choices"][0].update(key='[["int","2"]]'),
    lambda value: value["choices"][0].update(take="latest"),
    lambda value: value["choices"][0].update(reason="   "),
    lambda value: value["choices"][0].update(table="unknown_permissions"),
])
def test_wrong_plan_duplicate_unknown_or_implicit_choices_rejected(review_files, mutate):
    paths, output = review_files
    document = resolved(output)
    mutate(document)
    with pytest.raises(apply.MergeRejected):
        with apply.open_review(*paths, document):
            pass


def test_snapshot_change_between_verification_and_attachment_is_rejected(review_files, monkeypatch):
    paths, output = review_files
    original = apply.verify_snapshot

    def verify_then_change(path):
        result = original(path)
        if path == paths[2]:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE rows SET payload='[]'")
        return result

    monkeypatch.setattr(apply, "verify_snapshot", verify_then_change)
    with pytest.raises(ValueError):
        with apply.open_review(*paths, resolved(output)):
            pass


def test_existing_review_holds_read_transaction_on_all_evidence(review_files):
    paths, output = review_files
    with apply.open_review(*paths, resolved(output)) as review:
        for path in paths:
            with closing(sqlite3.connect(path, timeout=0.01)) as writer:
                writer.execute("UPDATE metadata SET seal=NULL")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.commit()
                writer.rollback()
        assert list(review.rows("sale_items", desired=True))[0][1].endswith('"current"]]')


def test_json_artifacts_obey_same_protected_path_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "ROOT", tmp_path / "blue")
    allowed = tmp_path / "blue/state/branches/choices.json"
    assert capture.archive_path(allowed, suffix=".json", create_parent=True) == allowed.resolve()
    for bad in (tmp_path / "outside.json", allowed.with_suffix(".exe")):
        with pytest.raises(ValueError):
            capture.archive_path(bad, suffix=".json")
    with pytest.raises(ValueError):
        capture.archive_path(allowed)  # Existing snapshot calls still reject JSON.


def test_cli_validates_all_three_inputs_without_mysql_writes(review_files, tmp_path, monkeypatch, capsys):
    paths, output = review_files
    monkeypatch.setattr(review_tool, "archive_path", lambda path, **kwargs: path)
    output.write_text(json.dumps(resolved(output)), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["blue_branch_review.py", "validate", *[
        part for name, path in zip(("baseline", "old", "current", "plan"), paths)
        for part in ("--" + name, str(path))], "--resolutions", str(output)])
    assert review_tool.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mysql_writes"] == 0 and report["production_apply_available"] is False
    output.write_text("not JSON", encoding="utf-8")
    assert review_tool.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
