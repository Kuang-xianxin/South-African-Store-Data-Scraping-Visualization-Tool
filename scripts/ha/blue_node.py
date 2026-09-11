"""Prepare isolated Windows blue nodes; never operate a production MySQL instance.

Commands deliberately separate preparation, initialization, restore and web serving.
No command promotes a replica or implements automatic failover.
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
import zipfile


ROOT = Path("D:/TakealotBlue")
DB_PORT = 3307
NODES = {
    "DESKTOP-NTRMANG": (101, "main"),
    "LAPTOP-2T5MN8EU": (102, "laptop"),
}


def node_identity(computer: str) -> tuple[int, str]:
    if computer not in NODES:
        raise RuntimeError("This machine is not an approved blue node")
    return NODES[computer]


def validate_database_identity(row: tuple[Any, ...], computer: str) -> None:
    server_id, _ = node_identity(computer)
    expected_dir = str(ROOT / "mysql/data").replace("\\", "/").rstrip("/").casefold()
    actual_dir = str(row[3]).replace("\\", "/").rstrip("/").casefold()
    if (str(row[0]), int(row[1]), int(row[2]), actual_dir) != (
        computer, server_id, DB_PORT, expected_dir
    ):
        raise RuntimeError("Blue database identity mismatch; operation refused")


def mysql_configuration(computer: str, basedir: Path) -> str:
    server_id, _ = node_identity(computer)
    base = basedir.as_posix()
    root = ROOT.as_posix()
    return f"""[mysqld]
basedir={base}
datadir={root}/mysql/data
port={DB_PORT}
bind-address=127.0.0.1
mysqlx=OFF
read-only=ON
super-read-only=ON
server-id={server_id}
gtid-mode=ON
enforce-gtid-consistency=ON
log-bin={root}/mysql/logs/blue-bin
relay-log={root}/mysql/logs/blue-relay
log-replica-updates=ON
relay-log-recovery=ON
binlog-format=ROW
binlog-expire-logs-seconds=604800
sync-binlog=1
innodb-flush-log-at-trx-commit=1
innodb-buffer-pool-size=256M
innodb-redo-log-capacity=128M
max-connections=60
max-allowed-packet=128M
log-error={root}/mysql/logs/mysql-error.log
pid-file={root}/mysql/blue.pid
local-infile=OFF
secure-file-priv=NULL
[client]
host=127.0.0.1
port={DB_PORT}
"""


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_secret(payload: bytes, *, decrypt: bool = False) -> bytes:
    """Use machine DPAPI; filesystem ACLs restrict who can decrypt the payload."""
    buffer = ctypes.create_string_buffer(payload)
    input_blob = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = Blob()
    crypt = ctypes.windll.crypt32
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                          ctypes.POINTER(Blob)]
    operation.restype = ctypes.c_int
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    flags = 1 if decrypt else 5  # UI_FORBIDDEN; protect also LOCAL_MACHINE
    if not operation(ctypes.byref(input_blob), None, None, None, None, flags,
                     ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        ctypes.memset(output_blob.data, 0, output_blob.size)
        free(output_blob.data)


def protected_credentials() -> dict[str, str]:
    return json.loads(protect_secret((ROOT / "secrets/mysql.dpapi").read_bytes(), decrypt=True))


def config() -> dict[str, Any]:
    result = json.loads((ROOT / "node.json").read_text(encoding="utf-8"))
    if result["computer"] != os.environ.get("COMPUTERNAME"):
        raise RuntimeError("Node configuration belongs to another computer")
    node_identity(result["computer"])
    return result


def connection(user: str = "root", *, password: str | None = None) -> Any:
    import pymysql

    cfg = config()
    secret = protected_credentials()[user] if password is None else password
    conn = pymysql.connect(host="127.0.0.1", port=DB_PORT, user=user, password=secret,
                           connect_timeout=5, read_timeout=30, write_timeout=30,
                           autocommit=True, charset="utf8mb4")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir")
            validate_database_identity(cursor.fetchone(), cfg["computer"])
    except BaseException:
        conn.close()
        raise
    return conn


def prepare(project: Path, mysql_bin: Path) -> dict[str, Any]:
    computer = os.environ.get("COMPUTERNAME", "")
    server_id, node = node_identity(computer)
    project = project.resolve(strict=True)
    mysql_bin = mysql_bin.resolve(strict=True)
    if not (mysql_bin / "mysqld.exe").is_file():
        raise RuntimeError("mysqld.exe is missing")
    for name in ("src", "config", "frontend/competitor/dist", ".venv/Scripts/python.exe",
                 "pyproject.toml", "README.md"):
        if not (project / name).exists():
            raise RuntimeError(f"Required release input is missing: {name}")
    if ROOT.exists():
        raise RuntimeError("Blue root already exists; refusing to overwrite it")
    if shutil.disk_usage(ROOT.parent).free < 8 * 1024**3:
        raise RuntimeError("At least 8 GiB of free space is required")
    ROOT.mkdir()
    # Apply explicit permissions before any credential or business data is created.
    identity = subprocess.check_output(["whoami"], text=True).strip()
    subprocess.run(["icacls", str(ROOT), "/inheritance:r", "/grant:r",
                    f"{identity}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True)
    for name in ("app", "secrets", "mysql/logs", "logs", "staging", "state"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in ("src", "config", "frontend/competitor/dist"):
        shutil.copytree(project / name, ROOT / "app" / name, ignore=ignore)
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(project / name, ROOT / "app" / name)
    # Copy each machine's own venv so its pyvenv.cfg keeps the correct local base Python.
    shutil.copytree(project / ".venv", ROOT / "runtime", ignore=ignore)
    shutil.copy2(Path(__file__).resolve(), ROOT / "blue_node.py")
    files = {
        item.relative_to(ROOT / "app").as_posix(): digest(item)
        for item in (ROOT / "app").rglob("*") if item.is_file()
    }
    (ROOT / "release-manifest.json").write_text(json.dumps(files, sort_keys=True), encoding="utf-8")
    credentials = {user: secrets.token_urlsafe(36) for user in ("root", "blue_app", "blue_crawler")}
    (ROOT / "secrets/mysql.dpapi").write_bytes(protect_secret(json.dumps(credentials).encode()))
    (ROOT / "mysql/my.ini").write_text(mysql_configuration(computer, mysql_bin.parent), encoding="utf-8")
    result = {"computer": computer, "node": node, "server_id": server_id,
              "mysql_port": DB_PORT, "web_port": 8503, "mysql_bin": str(mysql_bin),
              "environment": "blue", "auto_failover": False,
              "formal_crawler_enabled": False, "source_project": str(project)}
    (ROOT / "node.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return {**result, "files": len(files), "status": "prepared"}


def start_mysql() -> dict[str, Any]:
    cfg = config()
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", DB_PORT)) == 0:
            with connection():
                return {"status": "already-running", "node": cfg["node"]}
    command = [str(Path(cfg["mysql_bin"]) / "mysqld.exe"),
               f"--defaults-file={ROOT / 'mysql/my.ini'}", "--console"]
    with (ROOT / "logs/mysql-console.log").open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    (ROOT / "state/mysql-process.json").write_text(json.dumps({"pid": process.pid}), encoding="utf-8")
    return {"status": "started", "pid": process.pid, "port": DB_PORT}


def initialize() -> dict[str, Any]:
    cfg = config()
    data = ROOT / "mysql/data"
    if data.exists():
        raise RuntimeError("Blue data directory already exists; initialization refused")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", DB_PORT)) == 0:
            raise RuntimeError("Blue port is already occupied; initialization refused")
    mysqld = Path(cfg["mysql_bin"]) / "mysqld.exe"
    subprocess.run([str(mysqld), f"--defaults-file={ROOT / 'mysql/my.ini'}",
                    "--initialize-insecure"], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    start_mysql()
    return provision()


def provision() -> dict[str, Any]:
    """Complete a new blue bootstrap, without reinitializing an existing data directory."""
    cfg = config()
    conn = None
    for _ in range(60):
        try:
            conn = connection(password="")
            break
        except Exception as exc:
            if getattr(exc, "args", (None,))[0] != 2003:
                raise RuntimeError("Blue bootstrap connection rejected; initialization halted") from exc
            time.sleep(0.5)
    if conn is None:
        raise RuntimeError("Blue MySQL did not start; inspect the restricted blue logs")
    credentials = protected_credentials()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables "
                           "WHERE table_schema='takealot_ops'")
            if cursor.fetchone()[0] != 0:
                raise RuntimeError("Cannot provision a non-empty blue database")
            cursor.execute("SET SESSION sql_log_bin=0")
            cursor.execute("SET GLOBAL super_read_only=OFF")
            cursor.execute("SET GLOBAL read_only=OFF")
            cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (credentials["root"],))
            cursor.execute("CREATE DATABASE takealot_ops CHARACTER SET utf8mb4")
            for user in ("blue_app", "blue_crawler"):
                cursor.execute(f"CREATE USER '{user}'@'localhost' IDENTIFIED BY %s", (credentials[user],))
            cursor.execute("GRANT ALL PRIVILEGES ON takealot_ops.* TO 'blue_app'@'localhost'")
            cursor.execute("SET GLOBAL super_read_only=ON")
    except BaseException:
        # The connection has already passed the exact blue node identity guard.
        # Never leave a partly provisioned, passwordless blue instance accepting clients.
        with conn.cursor() as cursor:
            cursor.execute("SET GLOBAL super_read_only=ON")
            cursor.execute("SHUTDOWN")
        raise
    finally:
        conn.close()
    return {"status": "initialized-read-only", "port": DB_PORT, "server_id": cfg["server_id"]}


def restore(seed: Path, expected_sha256: str) -> dict[str, Any]:
    cfg = config()
    if len(expected_sha256) != 64 or digest(seed).lower() != expected_sha256.lower():
        raise RuntimeError("Seed SHA256 mismatch")
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='takealot_ops'")
        if cursor.fetchone()[0] != 0:
            raise RuntimeError("Blue schema is not empty; refusing to overwrite business data")
        cursor.execute("SET GLOBAL super_read_only=OFF")
        cursor.execute("SET GLOBAL read_only=OFF")
        try:
            credentials = protected_credentials()
            environment = dict(os.environ, MYSQL_PWD=credentials["root"])
            command = [str(Path(cfg["mysql_bin"]) / "mysql.exe"), "--no-defaults",
                       "--host=127.0.0.1", f"--port={DB_PORT}", "--user=root",
                       "--binary-mode=1", "--max-allowed-packet=1G",
                       "--init-command=SET SESSION sql_log_bin=0", "takealot_ops"]
            # Consume only the trusted, checksum-verified project backup. No green credentials.
            with (ROOT / "logs/restore.log").open("wb") as log:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                           env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    opener = gzip.open if seed.suffix == ".gz" else open
                    with opener(seed, "rb") as source:
                        assert process.stdin is not None
                        shutil.copyfileobj(source, process.stdin, length=1024**2)
                    process.stdin.close()
                    if process.wait() != 0:
                        raise RuntimeError("Blue restore failed; inspect restricted restore.log")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait()
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='takealot_ops'")
            count = int(cursor.fetchone()[0])
            if count < 60:
                raise RuntimeError("Restored blue schema is incomplete")
            cursor.execute("SET SESSION sql_log_bin=0")
            for table in ("erp_sessions", "competitor_collection_jobs", "competitor_worker_heartbeats"):
                cursor.execute(f"DELETE FROM takealot_ops.{table}")
            cursor.execute("CREATE TABLE takealot_ops.blue_environment_marker "
                           "(id INT PRIMARY KEY, seed_sha256 CHAR(64) NOT NULL, "
                           "environment VARCHAR(16) NOT NULL)")
            cursor.execute("INSERT INTO takealot_ops.blue_environment_marker VALUES (1,%s,'blue')",
                           (expected_sha256.lower(),))
            result = {"status": "restored-read-only", "tables": count + 1,
                      "seed_sha256": expected_sha256.lower(), "seed_file": seed.name}
            (ROOT / "state/seed.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            cursor.execute("SET GLOBAL super_read_only=ON")


def install_release(archive: Path, expected_sha256: str) -> dict[str, Any]:
    """Install an identical blue-only release, retaining the previous staged directory."""
    config()
    if digest(archive) != expected_sha256.lower():
        raise RuntimeError("Release SHA256 mismatch")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8503)) == 0:
            raise RuntimeError("Stop only the staged blue web before installing a release")
    app = ROOT / "app"
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            path = Path(item.filename)
            if (path.is_absolute() or ".." in path.parts or ":" in item.filename
                    or not path.parts or path.parts[0] not in {
                        "src", "config", "frontend", "pyproject.toml", "README.md"
                    } or not (app / path).resolve().is_relative_to(app.resolve())):
                raise RuntimeError("Unsafe release path")
        previous = ROOT / "staging" / f"app-before-{time.time_ns()}"
        app.rename(previous)
        app.mkdir()
        bundle.extractall(app)
    files = {item.relative_to(app).as_posix(): digest(item)
             for item in app.rglob("*") if item.is_file()}
    (ROOT / "release-manifest.json").write_text(json.dumps(files, sort_keys=True), encoding="utf-8")
    # Config is regenerated only before this node has initialized a database.
    cfg = config()
    if not (ROOT / "mysql/data").exists():
        (ROOT / "mysql/my.ini").write_text(
            mysql_configuration(cfg["computer"], Path(cfg["mysql_bin"]).parent), encoding="utf-8")
    return {"status": "release-installed", "files": len(files), "sha256": expected_sha256}


def audit_stage() -> dict[str, Any]:
    cfg = config()
    app = ROOT / "app"
    files = {}
    for directory in ("src", "config", "frontend/competitor/dist"):
        for item in (app / directory).rglob("*"):
            if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                files[item.relative_to(app).as_posix()] = digest(item)
    for name in ("README.md", "pyproject.toml"):
        files[name] = digest(app / name)
    release_hash = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@server_id,@@read_only,@@super_read_only,@@gtid_mode")
        role = cursor.fetchone()
        cursor.execute("SELECT seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
        seed_hash = cursor.fetchone()[0]
        counts = {}
        for table in ("erp_users", "erp_sessions", "competitor_snapshots",
                      "competitor_variant_snapshots", "competitor_collection_jobs",
                      "competitor_worker_heartbeats"):
            cursor.execute(f"SELECT COUNT(*) FROM takealot_ops.{table}")
            counts[table] = cursor.fetchone()[0]
    return {"node": cfg["node"], "role": role, "seed_sha256": seed_hash,
            "release_sha256": release_hash, "release_files": len(files), "counts": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "initialize", "provision", "start-mysql",
                                           "restore", "install-release", "status", "audit"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--mysql-bin", type=Path)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command not in {"status", "audit"} and not args.apply:
        print(json.dumps({"apply": False, "root": str(ROOT), "port": DB_PORT,
                          "node": node_identity(os.environ.get("COMPUTERNAME", ""))[1]}))
        return 0
    if args.command == "prepare":
        if not args.project or not args.mysql_bin:
            parser.error("prepare requires --project and --mysql-bin")
        result = prepare(args.project, args.mysql_bin)
    elif args.command == "initialize":
        result = initialize()
    elif args.command == "provision":
        result = provision()
    elif args.command == "start-mysql":
        result = start_mysql()
    elif args.command == "restore":
        if not args.seed or not args.sha256:
            parser.error("restore requires --seed and --sha256")
        result = restore(args.seed, args.sha256)
    elif args.command == "install-release":
        if not args.seed or not args.sha256:
            parser.error("install-release requires --seed and --sha256")
        result = install_release(args.seed, args.sha256)
    elif args.command == "audit":
        result = audit_stage()
    else:
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT @@server_id,@@read_only,@@super_read_only,@@gtid_mode")
            result = {"node": config()["node"], "database_state": cursor.fetchone()}
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
