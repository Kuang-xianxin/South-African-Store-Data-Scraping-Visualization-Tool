from contextlib import closing
import importlib
from pathlib import Path
import sqlite3
import struct
import sys
from types import SimpleNamespace
import uuid
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
journal = importlib.import_module("blue_branch_journal")


def event(kind, body, offset):
    size = 19 + len(body) + 4
    data = struct.pack("<IBIIIH", 0, kind, 987654, size, offset + size, 0) + body
    return data + struct.pack("<I", zlib.crc32(data))


def binlog(path):
    data = b"\xfebin"
    data += event(15, b"\x04\x00" + b"8.0.46".ljust(50, b"\x00") + b"\x00" * 4 + b"\x13" + b"\x00" * 40 + b"\x01", len(data))
    sid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    body = b"\x00" + sid.bytes + struct.pack("<q", 1) + b"\x02" + b"\x00" * 16 + b"\x00" * 7
    # GTID event plus an XID event; transaction length fits a one-byte integer.
    length = 19 + len(body) + 1 + 4 + 19 + 8 + 4
    data += event(33, body + bytes([length]), len(data))
    data += event(16, b"\x00" * 8, len(data))
    path.write_bytes(data)
    return str(sid)


def test_range_index_handles_out_of_order_and_multiple_sources():
    ranges = journal.Ranges()
    for value in (8, 2, 4, 3, 9, 2, 1, 6):
        ranges.add("b", value)
    ranges.add("a", 99)
    assert ranges.text() == "a:99,b:1-4:6:8-9"


@pytest.mark.parametrize("cut", [1, 3, 7, 21, -1, -10])
def test_truncated_binary_event_is_never_complete(tmp_path, cut):
    path = tmp_path / "blue-bin.000001"
    binlog(path)
    path.write_bytes(path.read_bytes()[:cut])
    with pytest.raises(journal.JournalRejected):
        list(journal.transaction_regions(path))


@pytest.mark.parametrize("marker", [251, 255])
def test_null_or_reserved_packed_transaction_length_rejected(marker):
    with pytest.raises(journal.JournalRejected):
        journal._packed_length(bytes([marker]), 0)


def test_prefix_copy_does_not_include_newer_bytes_and_never_overwrites(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"originalnewer")
    assert journal.prefix_digest(source, 8, target) == journal.prefix_digest(target, 8)
    assert target.read_bytes() == b"original"
    with pytest.raises(FileExistsError):
        journal.prefix_digest(source, 8, target)
    with pytest.raises(journal.JournalRejected):
        journal.prefix_digest(source, 100)


def test_successful_archive_is_sealed_and_detects_binary_tampering(tmp_path, monkeypatch):
    source = tmp_path / "blue-bin.000001"
    sid = binlog(source)
    monkeypatch.setattr(journal, "verify_binlog", lambda *args: None)
    target = tmp_path / "archive"
    result = journal.archive_files(target, [(source, source.stat().st_size)], Path("unused"),
        {"cluster": journal.CLUSTER}, lambda gtids: gtids == sid + ":1", lambda: None)
    header, seal = journal.verify_journal(target)
    assert seal == result["journal_seal"] and header["transactions"] == 1
    with (target / source.name).open("r+b") as output:
        output.seek(-1, 2)
        output.write(b"x")
    with pytest.raises(journal.JournalRejected, match="integrity"):
        journal.verify_journal(target)


@pytest.mark.parametrize("failure", ["source-changed", "coverage", "late-state", "checksum"])
def test_archive_failure_preserves_unsealed_evidence(tmp_path, monkeypatch, failure):
    source = tmp_path / "blue-bin.000001"
    binlog(source)

    def verify(*args):
        if failure == "source-changed":
            data = source.read_bytes()
            source.write_bytes(data[:-1] + b"x")
        if failure == "checksum":
            raise journal.JournalRejected("checksum")

    def final_check():
        if failure == "late-state":
            raise journal.JournalRejected("state-changed")

    monkeypatch.setattr(journal, "verify_binlog", verify)
    folder = tmp_path / "incomplete"
    with pytest.raises(journal.JournalRejected):
        journal.archive_files(folder, [(source, source.stat().st_size)], Path("unused"),
            {"cluster": journal.CLUSTER}, lambda _: failure != "coverage", final_check)
    assert (folder / source.name).is_file()
    with pytest.raises(journal.JournalRejected, match="incomplete"):
        journal.verify_journal(folder)
    with closing(sqlite3.connect(folder / "transactions.sqlite3")) as db:
        assert db.execute("SELECT seal FROM metadata WHERE id=1").fetchone() == (None,)


def test_insufficient_space_rejects_before_creating_archive(tmp_path, monkeypatch):
    source = tmp_path / "blue-bin.000001"
    binlog(source)
    monkeypatch.setattr(journal.shutil, "disk_usage", lambda _: SimpleNamespace(free=20))
    with pytest.raises(journal.JournalRejected, match="disk-space"):
        journal.archive_files(tmp_path / "no-space", [(source, source.stat().st_size)], Path("unused"),
            {"cluster": journal.CLUSTER}, lambda _: True, lambda: None)
    assert not (tmp_path / "no-space").exists()


def test_duplicate_transactions_cannot_be_silently_deduplicated(tmp_path, monkeypatch):
    first, second = tmp_path / "blue-bin.000001", tmp_path / "blue-bin.000002"
    binlog(first)
    second.write_bytes(first.read_bytes())
    monkeypatch.setattr(journal, "verify_binlog", lambda *args: None)
    folder = tmp_path / "duplicated"
    with pytest.raises(sqlite3.IntegrityError):
        journal.archive_files(folder, [(first, first.stat().st_size), (second, second.stat().st_size)], Path("unused"),
            {"cluster": journal.CLUSTER}, lambda _: True, lambda: None)
    with pytest.raises(journal.JournalRejected, match="incomplete"):
        journal.verify_journal(folder)
