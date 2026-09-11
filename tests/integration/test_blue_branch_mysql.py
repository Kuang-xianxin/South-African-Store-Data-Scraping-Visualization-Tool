"""Opt-in real MySQL rehearsal. Creates and shuts down its own loopback instance.

BLUE_RUN_MYSQL_REHEARSAL=1 enables this file; no service or existing DB is used.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
apply = importlib.import_module("blue_branch_apply")
merge = importlib.import_module("blue_branch_merge")

pytestmark = pytest.mark.skipif(os.getenv("BLUE_RUN_MYSQL_REHEARSAL") != "1", reason="isolated MySQL opt-in")


@pytest.fixture(scope="module")
def mysql_server(tmp_path_factory):
    executable = Path(os.getenv("BLUE_REHEARSAL_MYSQLD", "C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqld.exe"))
    assert executable.is_file(), "MySQL binary missing"
    root = tmp_path_factory.mktemp("blue-merge-mysql")
    datadir = root / "data"
    datadir.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port not in {3306, 3307, 13317}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    common = [str(executable), "--no-defaults", f"--basedir={executable.parent.parent}",
              f"--datadir={datadir}", "--innodb-buffer-pool-size=64M", "--innodb-redo-log-capacity=64M"]
    initialized = subprocess.run([*common, "--initialize-insecure"], capture_output=True, timeout=90, creationflags=flags)
    assert initialized.returncode == 0, initialized.stderr.decode(errors="replace")[-2000:]
    process = subprocess.Popen([*common, f"--port={port}", "--bind-address=127.0.0.1", "--mysqlx=OFF",
        f"--log-bin={root / 'blue-bin'}", "--gtid-mode=ON", "--enforce-gtid-consistency=ON",
        "--binlog-format=ROW", "--binlog-row-image=FULL", "--binlog-checksum=CRC32",
        "--sync-binlog=1", "--innodb-flush-log-at-trx-commit=1",
        "--server-id=987654", "--max-connections=10", "--event-scheduler=OFF",
        "--local-infile=OFF", "--secure-file-priv=NULL", f"--log-error={root / 'mysql.log'}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)

    def connect(database=None):
        return pymysql.connect(host="127.0.0.1", port=port, user="root", password="", database=database,
            charset="utf8mb4", autocommit=True, connect_timeout=2, read_timeout=10, write_timeout=10)

    try:
        deadline = time.monotonic() + 60
        while True:
            if process.poll() is not None:
                pytest.fail((root / "mysql.log").read_text(errors="replace")[-2000:])
            try:
                with connect() as conn, conn.cursor() as cursor:
                    cursor.execute("SELECT @@server_uuid")
                    server_uuid = cursor.fetchone()[0]
                break
            except pymysql.Error:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        yield connect, {"server_uuid": server_uuid, "datadir": datadir}
    finally:
        if process.poll() is None:
            try:
                with connect() as conn, conn.cursor() as cursor:
                    cursor.execute("SHUTDOWN")
            except pymysql.Error:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()  # Only the Popen child owned by this fixture.
                process.wait(timeout=10)


@pytest.fixture
def scenario(mysql_server, tmp_path):
    connect, target = mysql_server
    connections = []

    def make(base, old, current, *, ddl=None, fks=None, resolutions=None):
        database = "blue_merge_rehearsal_" + uuid.uuid4().hex
        conn = connect()
        connections.append(conn)
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        conn.select_db(database)
        ddl = ddl or {"sale_items": "CREATE TABLE sale_items (id BIGINT PRIMARY KEY, value VARCHAR(100) UNIQUE) ENGINE=InnoDB"}
        schemas = {}
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {apply.MARKER} (id INT PRIMARY KEY,nonce CHAR(32) NOT NULL) ENGINE=InnoDB")
            cursor.execute(f"INSERT INTO {apply.MARKER} VALUES (1,%s)", (database.removeprefix("blue_merge_rehearsal_"),))
            cursor.execute(apply.RECEIPT_DDL)
            for table, sql in ddl.items():
                cursor.execute(sql)
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                definition = re.sub(r"\sAUTO_INCREMENT=\d+(?=\s|$)", "", cursor.fetchone()[1])
                cursor.execute(f"SELECT * FROM `{table}` LIMIT 0")
                columns = [column[0] for column in cursor.description]
                schemas[table] = {"columns": columns, "primary_key": ["id"], "ddl": definition,
                                  "foreign_keys": (fks or {}).get(table, [])}
                for values in current.get(table, []):
                    cursor.execute(f"INSERT INTO `{table}` VALUES ({','.join('%s' for _ in values)})", values)
        directory = tmp_path / database
        directory.mkdir()
        paths = [directory / (name + ".sqlite3") for name in ("baseline", "old", "current", "plan")]
        base_seal = None
        for index, (path, tables) in enumerate(zip(paths, (base, old, current))):
            identity = {"cluster": merge.CLUSTER, "role": "branch" if index else "baseline", "epoch": 1,
                "node": "main" if index == 1 else "laptop", "gtid": "synthetic-gtid", "seed_sha256": "a" * 64}
            if index:
                identity["baseline_seal"] = base_seal
            writer = merge.SnapshotWriter(path, identity, schemas)
            try:
                for table, rows in tables.items():
                    for row in rows:
                        writer.append(table, row)
                seal = writer.seal()
                if not index:
                    base_seal = seal
            finally:
                writer.close()
        result = merge.plan(*paths)
        document = None
        if resolutions is not None:
            document = {"version": 1, "plan_seal": result["plan_seal"], "choices": [
                {"table": table, "key": merge.canonical([merge.encode(key)]), "take": take,
                 "reason": "Synthetic conflict decision"} for table, key, take in resolutions]}
        return conn, {**target, "database": database}, paths, document

    yield make
    for conn in connections:
        conn.close()


def rows(conn, table="sale_items"):
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT * FROM `{table}` ORDER BY id")
        return list(cursor.fetchall())


def receipts(conn):
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {apply.RECEIPTS}")
        return cursor.fetchone()[0]


def test_atomic_add_update_delete_dry_run_and_idempotence(scenario):
    base = {"sale_items": [(1, "before"), (2, "remove"), (3, "same")]}
    old = {"sale_items": [(1, "edited"), (3, "same"), (4, "new")]}
    current = {"sale_items": [(1, "before"), (2, "remove"), (3, "laptop")]}
    conn, target, paths, _ = scenario(base, old, current)
    with apply.open_review(*paths) as review:
        assert apply.rehearse(conn, review, **target)["status"] == "validated-and-rolled-back"
        assert rows(conn) == current["sale_items"] and receipts(conn) == 0
        assert apply.rehearse(conn, review, **target, commit=True)["status"] == "committed"
        assert rows(conn) == [(1, "edited"), (3, "laptop"), (4, "new")] and receipts(conn) == 1
        # A retry after later local edits must not replay the old merge.
        with conn.cursor() as cursor:
            cursor.execute("UPDATE sale_items SET value='later' WHERE id=1")
        assert apply.rehearse(conn, review, **target, commit=True)["status"] == "already-committed"
        assert rows(conn)[0] == (1, "later")


def test_collation_unique_collision_rolls_back_prior_writes(scenario):
    base = {"sale_items": [(1, "before")]}
    old = {"sale_items": [(1, "edited"), (2, "CAFÉ")]}
    current = {"sale_items": [(1, "before"), (3, "cafe")]}
    conn, target, paths, _ = scenario(base, old, current)
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected, match="mysql-merge-rejected"):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == current["sale_items"] and receipts(conn) == 0


def test_unrelated_current_row_drift_blocks_entire_merge(scenario):
    base = {"sale_items": [(1, "before"), (2, "untouched")]}
    conn, target, paths, _ = scenario(base, {"sale_items": [(1, "edited"), (2, "untouched")]}, base)
    with conn.cursor() as cursor:
        cursor.execute("UPDATE sale_items SET value='concurrent' WHERE id=2")
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected, match="drifted"):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [(1, "before"), (2, "concurrent")] and receipts(conn) == 0


def parent_schema(rule="CASCADE"):
    return {
        "company_products": "CREATE TABLE company_products (id BIGINT PRIMARY KEY,value VARCHAR(100)) ENGINE=InnoDB",
        "company_product_costs": f"""CREATE TABLE company_product_costs (
            id BIGINT PRIMARY KEY,parent_id BIGINT,value VARCHAR(100),
            CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES company_products(id) ON DELETE {rule}) ENGINE=InnoDB"""}, {
        "company_product_costs": [{"name": "fk_parent", "column": "parent_id", "referenced_table": "company_products",
            "referenced_column": "id", "delete_rule": rule, "update_rule": "NO ACTION"}]}


@pytest.mark.parametrize("rule", ["CASCADE", "SET NULL"])
def test_unreviewed_cascade_or_set_null_rolls_back(scenario, rule):
    ddl, fks = parent_schema(rule)
    base = {"company_products": [(1, "parent")], "company_product_costs": [(1, 1, "base child")]}
    # Old node deletes the family; current node adds a child the old node never saw.
    current = {"company_products": [(1, "parent")], "company_product_costs": [(1, 1, "base child"), (2, 1, "new child")]}
    conn, target, paths, resolution = scenario(base, {}, current, ddl=ddl, fks=fks,
        resolutions=[("company_products", 1, "old")])
    with apply.open_review(*paths, resolution) as review, pytest.raises(apply.MergeRejected, match="final-state"):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn, "company_products") == current["company_products"]
    assert rows(conn, "company_product_costs") == current["company_product_costs"] and receipts(conn) == 0


def test_parent_child_insert_and_reviewed_family_delete(scenario):
    ddl, fks = parent_schema()
    family = {"company_products": [(1, "parent")], "company_product_costs": [(1, 1, "child")]}
    conn, target, paths, _ = scenario({}, family, {}, ddl=ddl, fks=fks)
    with apply.open_review(*paths) as review:
        assert apply.rehearse(conn, review, **target, commit=True)["status"] == "committed"
    assert rows(conn, "company_product_costs") == family["company_product_costs"]
    conn, target, paths, resolution = scenario(family, {}, family, ddl=ddl, fks=fks,
        resolutions=[("company_products", 1, "old")])
    with apply.open_review(*paths, resolution) as review:
        assert apply.rehearse(conn, review, **target, commit=True)["status"] == "committed"
    assert rows(conn, "company_products") == rows(conn, "company_product_costs") == []


def test_foreign_key_failure_rolls_back_previous_change(scenario):
    ddl, fks = parent_schema()
    base = {"company_products": [(1, "before")]}
    old = {"company_products": [(1, "edited")], "company_product_costs": [(1, 99, "missing parent")]}
    conn, target, paths, _ = scenario(base, old, base, ddl=ddl, fks=fks)
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn, "company_products") == base["company_products"] and receipts(conn) == 0


@pytest.mark.parametrize("alter", [
    "ALTER TABLE sale_items ADD COLUMN extra INT",
    "CREATE TRIGGER test_merge_trigger BEFORE UPDATE ON sale_items FOR EACH ROW SET NEW.value='triggered'",
    "CREATE TABLE unexpected (id INT PRIMARY KEY) ENGINE=InnoDB",
])
def test_schema_trigger_and_inventory_drift_rejected(scenario, alter):
    base = {"sale_items": [(1, "before")]}
    conn, target, paths, _ = scenario(base, {"sale_items": [(1, "edited")]}, base)
    with conn.cursor() as cursor:
        cursor.execute(alter)
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn)[0][:2] == (1, "before") and receipts(conn) == 0


@pytest.mark.parametrize("take", ["old", "current"])
def test_explicit_business_conflict_resolution(scenario, take):
    conn, target, paths, document = scenario({"sale_items": [(1, "before")]},
        {"sale_items": [(1, "old change")]}, {"sale_items": [(1, "current change")]},
        resolutions=[("sale_items", 1, take)])
    with pytest.raises(apply.MergeRejected, match="unresolved"):
        with apply.open_review(*paths):
            pass
    with apply.open_review(*paths, document) as review:
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [(1, take + " change")]


def test_guarded_old_permissions_cannot_be_restored(scenario):
    ddl = {"erp_users": "CREATE TABLE erp_users (id BIGINT PRIMARY KEY,value VARCHAR(100)) ENGINE=InnoDB"}
    conn, target, paths, document = scenario({"erp_users": [(1, "enabled")]},
        {"erp_users": [(1, "old admin")]}, {"erp_users": [(1, "disabled")]}, ddl=ddl,
        resolutions=[("erp_users", 1, "old")])
    with pytest.raises(apply.MergeRejected, match="domain-specific"):
        with apply.open_review(*paths, document):
            pass
    document["choices"][0]["take"] = "current"
    with apply.open_review(*paths, document) as review:
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn, "erp_users") == [(1, "disabled")]


class LostCommit:
    def __init__(self, conn, *, after):
        self.conn, self.after = conn, after

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def commit(self):
        if self.after:
            self.conn.commit()
        raise OSError("Synthetic lost COMMIT response")


@pytest.mark.parametrize("after", [False, True])
def test_lost_commit_resolved_by_transactional_receipt(scenario, after):
    base = {"sale_items": [(1, "before")]}
    conn, target, paths, _ = scenario(base, {"sale_items": [(1, "edited")]}, base)
    with apply.open_review(*paths) as review:
        with pytest.raises(apply.CommitUncertain):
            apply.rehearse(LostCommit(conn, after=after), review, **target, commit=True)
        assert receipts(conn) == int(after)
        result = apply.rehearse(conn, review, **target, commit=True)
        assert result["status"] == ("already-committed" if after else "committed")
    assert rows(conn) == [(1, "edited")] and receipts(conn) == 1


def test_production_name_or_identity_refused(scenario):
    conn, target, paths, _ = scenario({}, {}, {})
    with apply.open_review(*paths) as review:
        for change in ({"database": "takealot_ops"}, {"server_uuid": apply.UUIDS["main"]}):
            with pytest.raises(apply.MergeRejected, match="synthetic-rehearsal"):
                apply.rehearse(conn, review, **{**target, **change}, commit=True)
    assert receipts(conn) == 0


def test_tampered_or_truncated_plan_rejected_even_with_recomputed_seal(scenario):
    import sqlite3

    conn, _, paths, _ = scenario({"sale_items": [(1, "a"), (2, "b")]},
        {"sale_items": [(1, "A"), (2, "B")]}, {"sale_items": [(1, "a"), (2, "b")]})
    with sqlite3.connect(paths[-1]) as db:
        header = json.loads(db.execute("SELECT header FROM metadata").fetchone()[0])
        db.execute("DELETE FROM changes WHERE key=?", (merge.canonical([merge.encode(2)]),))
        summary = header.pop("summary")
        summary["apply"] = 1
        seal = apply.hashlib.sha256(merge.canonical(header).encode())
        for record in db.execute("SELECT * FROM changes ORDER BY table_name,key"):
            seal.update(merge.canonical(record).encode() + b"\n")
        seal.update(merge.canonical(summary).encode())
        header["summary"] = summary
        db.execute("UPDATE metadata SET header=?,seal=?", (merge.canonical(header), seal.hexdigest()))
    with pytest.raises(apply.MergeRejected, match="complete-source"):
        with apply.open_review(*paths):
            pass
    assert receipts(conn) == 0


def test_table_lock_excludes_concurrent_writer(scenario, mysql_server, monkeypatch):
    base = {"sale_items": [(1, "before"), (2, "other row")]}
    conn, target, paths, _ = scenario(base, {"sale_items": [(1, "edited"), (2, "other row")]}, base)
    connect, _ = mysql_server
    original_write = apply._write_rows

    def competing_writer():
        with connect(target["database"]) as other, other.cursor() as cursor:
            cursor.execute("SET SESSION lock_wait_timeout=1,innodb_lock_wait_timeout=1")
            with pytest.raises(pymysql.err.OperationalError) as failure:
                cursor.execute("UPDATE sale_items SET value='concurrent' WHERE id=2")
            assert failure.value.args[0] == 1205

    def write_under_contended_lock(*args):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing_writer).result(timeout=5)
        original_write(*args)

    monkeypatch.setattr(apply, "_write_rows", write_under_contended_lock)
    with apply.open_review(*paths) as review:
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [(1, "edited"), (2, "other row")]


def test_exact_types_and_zero_generated_key(scenario):
    ddl = {"sale_items": """CREATE TABLE sale_items (id BIGINT PRIMARY KEY AUTO_INCREMENT,
        amount DECIMAL(30,6), binary_value VARBINARY(20), observed DATETIME(6),
        day DATE, elapsed TIME(6), value VARCHAR(100)) ENGINE=InnoDB"""}
    expected = (0, Decimal("123456789012345.000123"), b"\x00\xff", datetime(2026, 9, 11, 1, 2, 3, 4),
                date(2026, 9, 11), timedelta(days=-1, microseconds=999999), None)
    conn, target, paths, _ = scenario({}, {"sale_items": [expected]}, {}, ddl=ddl)
    with apply.open_review(*paths) as review:
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [expected]


def test_prior_committed_plan_cannot_be_reinterpreted(scenario):
    conn, target, paths, resolution = scenario({"sale_items": [(1, "before")]},
        {"sale_items": [(1, "old")]}, {"sale_items": [(1, "current")]},
        resolutions=[("sale_items", 1, "current")])
    with apply.open_review(*paths, resolution) as review:
        apply.rehearse(conn, review, **target, commit=True)
    resolution["choices"][0]["take"] = "old"
    with apply.open_review(*paths, resolution) as review, pytest.raises(apply.MergeRejected, match="different-resolution"):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [(1, "current")] and receipts(conn) == 1


def test_late_failure_after_writes_rolls_back_before_unlock(scenario, monkeypatch):
    base = {"sale_items": [(1, "before")]}
    conn, target, paths, _ = scenario(base, {"sale_items": [(1, "edited")]}, base)
    original_check = apply._assert_rows

    def fail_final_check(*args, **kwargs):
        if kwargs["desired"]:
            raise OSError("Synthetic evidence failure after SQL update")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(apply, "_assert_rows", fail_final_check)
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == base["sale_items"] and receipts(conn) == 0


def test_schema_cycle_is_rejected_before_dml(scenario):
    ddl = {"sale_items": """CREATE TABLE sale_items (id BIGINT PRIMARY KEY,parent_id BIGINT,
        CONSTRAINT fk_self FOREIGN KEY(parent_id) REFERENCES sale_items(id)) ENGINE=InnoDB"""}
    fks = {"sale_items": [{"referenced_table": "sale_items"}]}
    conn, target, paths, _ = scenario({}, {"sale_items": [(1, None)]}, {}, ddl=ddl, fks=fks)
    with apply.open_review(*paths) as review, pytest.raises(apply.MergeRejected, match="cyclic"):
        apply.rehearse(conn, review, **target, commit=True)
    assert rows(conn) == [] and receipts(conn) == 0


def journal_inputs(conn, target, baseline):
    with conn.cursor() as cursor:
        cursor.execute("SHOW MASTER STATUS")
        last, position, _, _, executed = cursor.fetchone()
        cursor.execute("SHOW BINARY LOGS")
        logs = cursor.fetchall()
    files = []
    for row in logs:
        files.append((target["datadir"].parent / row[0], position if row[0] == last else row[1]))
        if row[0] == last:
            break

    def covers(covered):
        with conn.cursor() as cursor:
            cursor.execute("SELECT GTID_SUBSET(GTID_SUBTRACT(%s,%s),%s)", (executed, baseline, covered))
            return cursor.fetchone()[0] == 1

    executable = Path(os.getenv("BLUE_REHEARSAL_MYSQLD", "C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqld.exe"))
    return files, executable.with_name("mysqlbinlog.exe" if os.name == "nt" else "mysqlbinlog"), covers


def test_binlog_journal_preserves_complete_multirow_transaction(scenario, tmp_path):
    journal = importlib.import_module("blue_branch_journal")
    conn, target, _, _ = scenario({}, {}, {})
    assert journal.boundary(conn)["config"]["binlog_format"] == "ROW"
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@global.gtid_executed")
        baseline = cursor.fetchone()[0]
    conn.autocommit(False)
    with conn.cursor() as cursor:
        cursor.execute("INSERT INTO sale_items VALUES (1,'first row'),(2,'second row')")
        cursor.execute("UPDATE sale_items SET value='changed within same transaction' WHERE id=1")
    conn.commit()
    conn.autocommit(True)
    files, executable, covers = journal_inputs(conn, target, baseline)
    folder = tmp_path / "complete-journal"
    result = journal.archive_files(folder, files, executable, {"cluster": merge.CLUSTER}, covers, lambda: None)
    header, seal = journal.verify_journal(folder)
    assert result["journal_seal"] == seal and covers(header["gtids"])
    assert header["transactions"] > 0
    # The final transaction has several row events but exactly one GTID region.
    regions = list(journal.transaction_regions(folder / files[-1][0].name))
    assert regions[-1][2] + regions[-1][3] == files[-1][1]
    assert result["mysql_writes"] == 0


def test_binlog_missing_transaction_file_leaves_unsealed_journal(scenario, tmp_path):
    journal = importlib.import_module("blue_branch_journal")
    conn, target, _, _ = scenario({}, {}, {})
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@global.gtid_executed")
        baseline = cursor.fetchone()[0]
        cursor.execute("INSERT INTO sale_items VALUES (1,'first')")
        cursor.execute("FLUSH BINARY LOGS")
        cursor.execute("INSERT INTO sale_items VALUES (2,'second')")
    files, executable, covers = journal_inputs(conn, target, baseline)
    folder = tmp_path / "missing-journal"
    with pytest.raises(journal.JournalRejected, match="missing-required-gtids"):
        journal.archive_files(folder, files[-1:], executable, {"cluster": merge.CLUSTER}, covers, lambda: None)
    with pytest.raises(journal.JournalRejected, match="incomplete"):
        journal.verify_journal(folder)


def test_checksum_valid_gtid_only_prefix_is_not_complete_transaction(scenario, tmp_path):
    import struct
    journal = importlib.import_module("blue_branch_journal")
    conn, target, _, _ = scenario({}, {}, {})
    files, executable, _ = journal_inputs(conn, target, "")
    source, size = files[-1]
    full = tmp_path / "full-binlog"
    journal.prefix_digest(source, size, full)
    first = next(journal.transaction_regions(full))
    with full.open("rb") as stream:
        stream.seek(first[2])
        event_length = struct.unpack("<IBIIIH", stream.read(19))[3]
    truncated = tmp_path / "gtid-only"
    journal.prefix_digest(full, first[2] + event_length, truncated)
    # Every included event still has its original correct CRC32.
    journal.verify_binlog(truncated, executable)
    with pytest.raises(journal.JournalRejected, match="incomplete-transaction"):
        list(journal.transaction_regions(truncated))


def test_binlog_corruption_or_index_edit_is_detected(scenario, tmp_path):
    import sqlite3
    journal = importlib.import_module("blue_branch_journal")
    conn, target, _, _ = scenario({}, {}, {})
    files, executable, covers = journal_inputs(conn, target, "")
    folder = tmp_path / "tampered-journal"
    journal.archive_files(folder, files, executable, {"cluster": merge.CLUSTER}, covers, lambda: None)
    with sqlite3.connect(folder / "transactions.sqlite3") as db:
        db.execute("UPDATE transactions SET length=length+1 WHERE gno=(SELECT MIN(gno) FROM transactions)")
    with pytest.raises(journal.JournalRejected, match="index-mismatch"):
        journal.verify_journal(folder)
    damaged = tmp_path / "damaged-binlog"
    journal.prefix_digest(files[-1][0], files[-1][1], damaged)
    with damaged.open("r+b") as stream:
        stream.seek(-5, 2)
        value = stream.read(1)
        stream.seek(-1, 1)
        stream.write(bytes([value[0] ^ 1]))
    with pytest.raises(journal.JournalRejected, match="validation-failed"):
        journal.verify_binlog(damaged, executable)
