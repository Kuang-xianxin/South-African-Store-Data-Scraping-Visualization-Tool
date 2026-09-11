"""Prepare and audit the fixed-primary writable BLUE test cluster, never green."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess

import blue_node


ROOT = Path("D:/TakealotBlue")
PRIMARY = "DESKTOP-NTRMANG"
PRIMARY_UUID = "49de5abf-a8cc-11f1-9fc1-00e2699c0656"
USERS = ("blue_web_main", "blue_web_laptop", "blue_worker_main", "blue_worker_laptop", "blue_replication")
CHANNEL = "blue_test_main"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def require_main() -> None:
    if blue_node.config()["computer"] != PRIMARY:
        raise RuntimeError("This action is only for the main BLUE instance")


def credentials() -> dict[str, str]:
    return json.loads(blue_node.protect_secret(
        (ROOT / "secrets/blue-test-credentials.dpapi").read_bytes(), decrypt=True))


def validate_primary(row: tuple[object, ...], *, writable: bool = False) -> None:
    actual = (str(row[0]), int(row[1]), int(row[2]),
              str(row[3]).replace("\\", "/").rstrip("/").casefold(), str(row[4]))
    if actual != (PRIMARY, 101, 3307, "d:/takealotblue/mysql/data", PRIMARY_UUID):
        raise RuntimeError("Unexpected BLUE primary identity; GREEN endpoints are forbidden")
    if writable and tuple(row[5:7]) != (0, 0):
        raise RuntimeError("BLUE primary is not writable")


def tls_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ROOT / "secrets/blue-primary-ca.pem"))


def primary_connection(user: str, *, writable: bool = False):
    import pymysql

    cfg = blue_node.config()
    port = 3307 if cfg["node"] == "main" else 13317
    conn = pymysql.connect(host="127.0.0.1", port=port, user=user,
                           password=credentials()[user], ssl=tls_context(),
                           connect_timeout=8, read_timeout=60, write_timeout=60,
                           autocommit=True, charset="utf8mb4")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid,"
                           "@@global.read_only,@@global.super_read_only")
            validate_primary(cursor.fetchone(), writable=writable)
            cursor.execute("SELECT environment,seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
            seed = json.loads((ROOT / "state/seed.json").read_text(encoding="utf-8"))
            if cursor.fetchone() != ("blue", seed["seed_sha256"]):
                raise RuntimeError("BLUE marker mismatch")
    except BaseException:
        conn.close()
        raise
    return conn


def snapshot() -> dict[str, object]:
    cfg = blue_node.config()
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@global.read_only,@@global.super_read_only,@@global.gtid_executed")
        role = cursor.fetchone()
        if tuple(role[:2]) != (1, 1):
            raise RuntimeError("Baseline capture requires the read-only BLUE stage")
        cursor.execute("SELECT table_name FROM information_schema.tables "
                       "WHERE table_schema='takealot_ops' AND table_type='BASE TABLE' ORDER BY table_name")
        tables = [row[0] for row in cursor.fetchall()]
        checksums = {}
        for name in tables:
            if not name.replace("_", "").isalnum():
                raise RuntimeError("Unexpected table identifier")
            cursor.execute(f"CHECKSUM TABLE takealot_ops.`{name}` EXTENDED")
            checksums[name] = cursor.fetchone()[1]
            if checksums[name] is None:
                raise RuntimeError("A BLUE table did not produce a baseline checksum")
    folder = ROOT / "backups/writable-test-baseline"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "takealot_ops.sql.gz"
    if archive.exists():
        raise RuntimeError("Baseline archive already exists; refusing overwrite")
    cfg = blue_node.config()
    command = [str(Path(cfg["mysql_bin"]) / "mysqldump.exe"), "--no-defaults",
               "--host=127.0.0.1", "--port=3307", "--user=root", "--single-transaction",
               "--skip-lock-tables", "--set-gtid-purged=OFF", "--databases", "takealot_ops"]
    environment = dict(os.environ, MYSQL_PWD=blue_node.protected_credentials()["root"])
    with archive.open("xb") as output, gzip.GzipFile(fileobj=output, mode="wb") as zipped:
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.stdout is not None
        shutil.copyfileobj(process.stdout, zipped)
        _, error = process.communicate()
        if process.returncode:
            raise RuntimeError(f"BLUE backup failed ({len(error)} error bytes); archive is incomplete")
    result = {"node": cfg["node"], "role": role, "tables": len(tables), "checksums": checksums,
              "archive": str(archive), "sha256": blue_node.digest(archive)}
    write_json(folder / "manifest.json", result)
    return {key: value for key, value in result.items() if key != "checksums"}


def prepare_main() -> dict[str, object]:
    require_main()
    baseline = ROOT / "backups/writable-test-baseline/manifest.json"
    if not baseline.is_file():
        raise RuntimeError("A verified BLUE baseline backup is required")
    credential_file = ROOT / "secrets/blue-test-credentials.dpapi"
    if credential_file.exists():
        raise RuntimeError("BLUE test credentials already exist; inspect partial setup")
    pki = ROOT / "secrets/blue-test-pki"
    if pki.exists():
        raise RuntimeError("BLUE PKI already exists; refusing overwrite")
    pki.mkdir()
    openssl = Path("D:/software/Git/usr/bin/openssl.exe")
    if not openssl.is_file():
        raise RuntimeError("Reviewed OpenSSL binary is missing")
    commands = [
        ["req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "730",
         "-keyout", str(pki / "ca-key.pem"), "-out", str(pki / "ca.pem"),
         "-subj", "/CN=Takealot BLUE Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        ["req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
         "-keyout", str(pki / "server-key.pem"), "-out", str(pki / "server.csr"),
         "-subj", "/CN=localhost"],
        ["x509", "-req", "-in", str(pki / "server.csr"), "-CA", str(pki / "ca.pem"),
         "-CAkey", str(pki / "ca-key.pem"), "-CAcreateserial", "-out", str(pki / "server-cert.pem"),
         "-days", "365", "-sha256", "-extfile", str(pki / "server.ext")],
    ]
    (pki / "server.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1,"
        "IP:100.70.103.11,IP:192.168.110.180\n", encoding="ascii")
    for args in commands:
        subprocess.run([str(openssl), *args], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    backup = ROOT / "backups/writable-test-baseline"
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid")
        validate_primary(cursor.fetchone())
        cursor.execute("SELECT @@global.ssl_ca,@@global.ssl_cert,@@global.ssl_key")
        if cursor.fetchone() != ("ca.pem", "server-cert.pem", "server-key.pem"):
            raise RuntimeError("Unexpected TLS file configuration")
        for name in ("ca.pem", "server-cert.pem", "server-key.pem"):
            shutil.copy2(ROOT / "mysql/data" / name, backup / name)
            shutil.copy2(pki / name, ROOT / "mysql/data" / name)
        cursor.execute("ALTER INSTANCE RELOAD TLS")
        shutil.copy2(pki / "ca.pem", ROOT / "secrets/blue-primary-ca.pem")
        passwords = {user: secrets.token_urlsafe(24 if user == "blue_replication" else 36) for user in USERS}
        credential_file.write_bytes(blue_node.protect_secret(json.dumps(passwords).encode()))
        cursor.execute("SET SESSION sql_log_bin=0")
        cursor.execute("SET GLOBAL super_read_only=OFF")
        cursor.execute("SET GLOBAL read_only=OFF")
        try:
            for user, password in passwords.items():
                cursor.execute(f"CREATE USER '{user}'@'localhost' IDENTIFIED BY %s REQUIRE SSL", (password,))
                if user == "blue_replication":
                    cursor.execute(f"GRANT REPLICATION SLAVE ON *.* TO '{user}'@'localhost'")
                elif user.startswith("blue_web_"):
                    cursor.execute(f"GRANT ALL PRIVILEGES ON takealot_ops.* TO '{user}'@'localhost'")
                else:
                    cursor.execute(f"GRANT SELECT ON takealot_ops.* TO '{user}'@'localhost'")
                    for table in ("competitor_collection_jobs", "competitor_worker_heartbeats",
                                  "competitor_targets", "competitor_target_audits", "competitor_link_health",
                                  "competitor_snapshots", "competitor_variant_snapshots", "competitor_reviews"):
                        cursor.execute(f"GRANT INSERT,UPDATE ON takealot_ops.`{table}` TO '{user}'@'localhost'")
        finally:
            cursor.execute("SET GLOBAL super_read_only=ON")
    with primary_connection("blue_web_main"):
        pass
    write_json(ROOT / "state/blue-test-prepared.json", {"status": "prepared-read-only", "users": list(USERS)})
    return {"status": "prepared-read-only", "tls_hostname_verification": True, "users": list(USERS)}


def import_laptop() -> dict[str, object]:
    if blue_node.config()["node"] != "laptop":
        raise RuntimeError("Credential import is laptop-only")
    import sys

    payload = json.loads(sys.stdin.read())
    if set(payload) != {"credentials", "ca_pem", "primary_checksums"}:
        raise RuntimeError("Invalid BLUE import fields")
    allowed = {"blue_web_laptop", "blue_worker_laptop", "blue_replication"}
    if set(payload["credentials"]) != allowed:
        raise RuntimeError("Unapproved credential scope")
    baseline = json.loads((ROOT / "backups/writable-test-baseline/manifest.json").read_text(encoding="utf-8"))
    if baseline["checksums"] != payload["primary_checksums"]:
        raise RuntimeError("BLUE seed tables differ; replication refused")
    path = ROOT / "secrets/blue-test-credentials.dpapi"
    if path.exists():
        raise RuntimeError("Imported BLUE credentials already exist")
    path.write_bytes(blue_node.protect_secret(json.dumps(payload["credentials"]).encode()))
    (ROOT / "secrets/blue-primary-ca.pem").write_text(payload["ca_pem"], encoding="ascii")
    return {"status": "imported", "tables_matched": len(baseline["checksums"]), "private_keys_transferred": False}


def start_replica() -> dict[str, object]:
    if blue_node.config()["node"] != "laptop":
        raise RuntimeError("Replica configuration is laptop-only")
    with primary_connection("blue_web_laptop"):
        pass
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SHOW REPLICA STATUS")
        if cursor.fetchone() is not None:
            raise RuntimeError("Existing replica channel must not be overwritten")
        cursor.execute("SELECT @@global.gtid_executed,@@global.gtid_purged,@@global.read_only,@@global.super_read_only")
        if cursor.fetchone() != ("", "", 1, 1):
            raise RuntimeError("Replica is not the untouched read-only BLUE seed")
        cursor.execute("CHANGE REPLICATION SOURCE TO SOURCE_HOST='127.0.0.1', SOURCE_PORT=13317, "
                       "SOURCE_USER='blue_replication', SOURCE_PASSWORD=%s, SOURCE_AUTO_POSITION=1, "
                       "SOURCE_SSL=1, SOURCE_SSL_CA=%s, SOURCE_SSL_VERIFY_SERVER_CERT=1, "
                       "SOURCE_CONNECT_RETRY=5, SOURCE_RETRY_COUNT=86400 FOR CHANNEL 'blue_test_main'",
                       (credentials()["blue_replication"], str(ROOT / "secrets/blue-primary-ca.pem")))
        cursor.execute("START REPLICA FOR CHANNEL 'blue_test_main'")
    return {"status": "replication-start-requested", "channel": CHANNEL, "tls_verify_identity": True}


def enable_primary() -> dict[str, object]:
    require_main()
    if not (ROOT / "state/blue-test-prepared.json").is_file():
        raise RuntimeError("BLUE preparation missing")
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid")
        validate_primary(cursor.fetchone())
        cursor.execute("SET PERSIST super_read_only=OFF")
        cursor.execute("SET PERSIST read_only=OFF")
    return {"status": "fixed-primary-writable", "server_id": 101, "port": 3307,
            "automatic_failover": False, "green_changed": False}


def repair_replication_password() -> dict[str, object]:
    """Repair only the pre-write BLUE replication account's MySQL 8 length limit."""
    require_main()
    passwords = credentials()
    if len(passwords["blue_replication"]) <= 32:
        raise RuntimeError("Replication credential already fits; refusing rotation")
    password = secrets.token_urlsafe(24)
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@global.read_only,@@global.super_read_only")
        if cursor.fetchone() != (1, 1):
            raise RuntimeError("Repair is only allowed before writable activation")
        cursor.execute("SET SESSION sql_log_bin=0")
        cursor.execute("SET GLOBAL super_read_only=OFF")
        cursor.execute("SET GLOBAL read_only=OFF")
        try:
            cursor.execute("ALTER USER 'blue_replication'@'localhost' IDENTIFIED BY %s REQUIRE SSL", (password,))
            passwords["blue_replication"] = password
            (ROOT / "secrets/blue-test-credentials.dpapi").write_bytes(
                blue_node.protect_secret(json.dumps(passwords).encode()))
        finally:
            cursor.execute("SET GLOBAL super_read_only=ON")
    return {"status": "replication-credential-repaired", "length": len(password)}


def import_replication_password() -> dict[str, object]:
    if blue_node.config()["node"] != "laptop":
        raise RuntimeError("Import is laptop-only")
    import sys

    password = json.loads(sys.stdin.read())["blue_replication"]
    if not isinstance(password, str) or len(password) != 32:
        raise RuntimeError("Unexpected replication credential")
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SHOW REPLICA STATUS")
        if cursor.fetchone() is not None:
            raise RuntimeError("Existing replication must not be reconfigured")
    passwords = credentials()
    passwords["blue_replication"] = password
    (ROOT / "secrets/blue-test-credentials.dpapi").write_bytes(
        blue_node.protect_secret(json.dumps(passwords).encode()))
    return {"status": "replication-credential-imported"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "prepare-main", "import-laptop", "start-replica", "enable-primary",
                                          "repair-replication-password", "import-replication-password"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    blue_node.config()
    if not args.apply:
        print(json.dumps({"apply": False, "action": args.action, "root": str(ROOT)}))
        return
    actions = {"snapshot": snapshot, "prepare-main": prepare_main, "import-laptop": import_laptop,
               "start-replica": start_replica, "enable-primary": enable_primary,
               "repair-replication-password": repair_replication_password,
               "import-replication-password": import_replication_password}
    try:
        print(json.dumps(actions[args.action]()))
    except Exception as exc:
        raise SystemExit(f"BLUE setup failed ({type(exc).__name__}); inspect scoped state, no green fallback") from None


if __name__ == "__main__":
    main()
