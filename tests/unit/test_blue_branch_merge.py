from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
merge = importlib.import_module("blue_branch_merge")


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 2**65, "中文", "", b"\x00\xff",
    Decimal("10000000000000.123400"), Decimal("0.00"), 0.0, -0.0, 1.5,
    date(2026, 9, 11), datetime(2026, 9, 11, 1, 2, 3, 4),
    datetime(2026, 9, 11, tzinfo=timezone.utc), timedelta(days=-2, microseconds=12)])
def test_lossless_sql_value_round_trip(value):
    restored = merge.decode(merge.encode(value))
    assert type(restored) is type(value)
    assert restored == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN"), object()])
def test_unsupported_values_stop_snapshot(value):
    with pytest.raises(ValueError):
        merge.encode(value)


def snapshots(tmp_path, base_rows, old_rows, current_rows, *, table="sale_items", referenced=False):
    schema = {table: {"columns": ["id", "value"], "primary_key": ["id"], "foreign_keys": []}}
    if referenced:
        schema["company_product_costs"] = {"columns": ["id", "value"], "primary_key": ["id"],
            "foreign_keys": [{"referenced_table": table, "delete_rule": "CASCADE"}]}
    paths = [tmp_path / f"{name}.sqlite3" for name in ("base", "old", "current")]
    base_seal = None
    for index, (path, rows) in enumerate(zip(paths, (base_rows, old_rows, current_rows), strict=True)):
        identity = {"cluster": merge.CLUSTER, "node": "main" if index == 1 else "laptop",
            "role": "baseline" if index == 0 else "branch", "epoch": 1,
            "gtid": "fixture-gtid", "seed_sha256": "a" * 64}
        if index:
            identity["baseline_seal"] = base_seal
        writer = merge.SnapshotWriter(path, identity, schema)
        try:
            for row in rows:
                writer.append(table, row)
            seal = writer.seal()
            if not index:
                base_seal = seal
        finally:
            writer.close()
    return paths


def changes(path):
    with closing(merge.readonly(path)) as db:
        return db.execute("SELECT decision,reason,base,old,current FROM changes ORDER BY key").fetchall()


def test_three_way_add_edit_delete_and_deduplicate(tmp_path):
    paths = snapshots(tmp_path, [(1, "old"), (2, "remove"), (3, "same"), (4, "same")],
        [(1, "edited"), (3, "same"), (4, "same"), (5, "new"), (6, "both added")],
        [(1, "old"), (2, "remove"), (3, "same"), (4, "laptop edit"), (6, "both added")])
    result = merge.plan(*paths, tmp_path / "plan.db")
    assert result["apply"] == 3
    assert result["keep"] == 3
    assert result["conflict"] == 0
    assert result["conflict_free"]
    assert result["mysql_apply_available"] is False
    assert len(changes(tmp_path / "plan.db")) == 3


@pytest.mark.parametrize("base,old,current", [([], [(1, "old new")], [(1, "laptop new")]),
    ([(1, "before")], [(1, "a")], [(1, "b")]), ([(1, "before")], [], [(1, "edit")])])
def test_generated_id_edit_and_delete_edit_collisions_keep_both(tmp_path, base, old, current):
    paths = snapshots(tmp_path, base, old, current)
    result = merge.plan(*paths, tmp_path / "plan.db")
    assert result["conflict"] == 1 and not result["conflict_free"]
    decision, reason, b, o, c = changes(tmp_path / "plan.db")[0]
    assert (decision, reason) == ("conflict", "both-branches-changed")
    assert o != c
    assert (b is None) == (not base)


@pytest.mark.parametrize("table", ["erp_users", "erp_user_stores", "erp_sessions", "blue_codex_quota",
                                     "competitor_collection_jobs", "erp_refresh_state"])
def test_security_quota_or_jobs_never_apply_from_disconnected_old_node(tmp_path, table):
    paths = snapshots(tmp_path, [(1, "before")], [(1, "old change")], [(1, "before")], table=table)
    assert merge.plan(*paths, tmp_path / "plan.db")["conflict"] == 1
    assert changes(tmp_path / "plan.db")[0][1] == "security-or-execution-state-review"


def test_laptop_revocation_cannot_be_undone_by_unchanged_old_record(tmp_path):
    paths = snapshots(tmp_path, [(1, "enabled")], [(1, "enabled")], [(1, "disabled")], table="erp_users")
    result = merge.plan(*paths, tmp_path / "plan.db")
    assert result["apply"] == 0 and result["keep"] == 1


def test_parent_delete_is_not_automatically_cascaded(tmp_path):
    paths = snapshots(tmp_path, [(1, "parent")], [], [(1, "parent")], table="company_products", referenced=True)
    assert merge.plan(*paths, tmp_path / "plan.db")["conflict"] == 1
    assert changes(tmp_path / "plan.db")[0][1] == "parent-delete-needs-related-row-review"


def test_changed_history_is_not_deduplicated_as_a_new_observation(tmp_path):
    paths = snapshots(tmp_path, [(1, "old")], [(1, "overwritten")], [(1, "old")], table="offer_snapshots")
    assert merge.plan(*paths, tmp_path / "plan.db")["conflict"] == 1
    assert changes(tmp_path / "plan.db")[0][1] == "immutable-history-changed"


def test_any_conflict_blocks_complete_plan_even_with_safe_other_rows(tmp_path):
    paths = snapshots(tmp_path, [(1, "a"), (2, "b")], [(1, "old"), (2, "safe")], [(1, "lap"), (2, "b")])
    result = merge.plan(*paths, tmp_path / "plan.db")
    assert result["apply"] == result["conflict"] == 1
    assert not result["conflict_free"]


def test_corrupt_row_stops_plan_and_no_output_is_created(tmp_path):
    paths = snapshots(tmp_path, [(1, "base")], [(1, "old")], [(1, "base")])
    with closing(sqlite3.connect(paths[1])) as db, db:
        db.execute("UPDATE rows SET payload='[]'")
    with pytest.raises(ValueError):
        merge.plan(*paths, tmp_path / "plan.db")
    assert not (tmp_path / "plan.db").exists()


def test_incomplete_archive_cannot_be_used_or_overwritten(tmp_path):
    paths = snapshots(tmp_path, [], [], [])
    with closing(sqlite3.connect(paths[1])) as db, db:
        db.execute("UPDATE metadata SET seal=NULL")
    with pytest.raises(ValueError, match="incomplete"):
        merge.verify_snapshot(paths[1])
    with pytest.raises(FileExistsError):
        merge.exclusive_database(paths[1])


def test_future_table_requires_policy_review(tmp_path):
    with pytest.raises(ValueError, match="unreviewed"):
        merge.SnapshotWriter(tmp_path / "unknown.db", {"cluster": merge.CLUSTER, "node": "main",
            "role": "baseline", "gtid": "x"}, {"future_permissions": {}})
    assert not (tmp_path / "unknown.db").exists()


def test_table_policies_match_verified_68_table_inventory():
    assert len(merge.KNOWN_TABLES) == 68
    assert not (merge.GUARDED & merge.APPEND_ONLY or merge.GUARDED & merge.ORDINARY or merge.APPEND_ONLY & merge.ORDINARY)
