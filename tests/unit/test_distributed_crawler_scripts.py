from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HA_SCRIPTS = PROJECT_ROOT / "scripts" / "ha"


def _script(name: str) -> str:
    return (HA_SCRIPTS / name).read_text(encoding="utf-8")


def test_laptop_secret_protector_is_host_guarded_and_dpapi_protected() -> None:
    source = _script("protect_distributed_crawler_secret.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "DataProtectionScope]::LocalMachine" in source
    assert "SetAccessRuleProtection($true, $false)" in source
    assert "Use -Rotate only for an intentional rotation" in source
    assert "password = $Password" not in source


def test_account_provisioning_is_main_guarded_tls_only_and_minimal() -> None:
    source = _script("provision_distributed_crawler_account.ps1")

    assert "$ExpectedComputerName = 'DESKTOP-NTRMANG'" in source
    assert 'CREATE USER \'$CrawlerUser\'@\'$CrawlerHost\' IDENTIFIED BY' in source
    assert "'$CrawlerPassword' REQUIRE SSL" in source
    assert "GRANT SELECT, INSERT, UPDATE ON takealot_ops.competitor_collection_jobs" in source
    assert "GRANT SELECT, INSERT ON takealot_ops.competitor_snapshots" in source
    assert "GRANT SELECT ON takealot_ops.erp_stores" in source
    assert "GRANT SELECT ON takealot_ops.offer_current" in source
    assert "GRANT DELETE" not in source
    assert "takealot_ops.*" not in source
    assert "grantable_required_dml" in source
    assert "required_dml = 13" in source
    assert "verified unused orphan" in source
    assert "execute_will_restart_mysql = $false" in source
    assert "execute_will_restart_erp = $false" in source


def test_laptop_worker_can_only_use_expected_primary_and_loopback_proxy() -> None:
    source = _script("run_laptop_distributed_worker.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "$PrimaryHost = '100.70.103.11'" in source
    assert "$PrimaryPort = 13306" in source
    assert "$ProxyHost = '127.0.0.1'" in source
    assert "$ProxyPort = 7897" in source
    assert "TAKEALOT_DISTRIBUTED_DATABASE_URL" in source
    assert "password_recorded = $false" in source


def test_laptop_file_installer_refuses_an_unverified_models_baseline() -> None:
    source = _script("install_laptop_distributed_crawler_files.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "$ExpectedOriginalModelsNormalizedSha256" in source
    assert "Laptop models.py is not the verified pre-deployment baseline" in source
    assert "Refusing to overwrite unexpected laptop file" in source
    assert "models.py.before-distributed-crawler" in source
    assert "Expected exactly one Takealot project on D:" in source
    assert "D:\\TakealotHA\\bin" in source


def test_temporary_canary_credentials_are_stdin_only_and_bounded() -> None:
    laptop = _script("run_laptop_distributed_canary.ps1")
    main = _script("invoke_laptop_distributed_canary.ps1")

    for source in (laptop, main):
        assert "distributed-canary-[0-9]{8}-[0-9]{6}" in source
        assert "[ValidateRange(0, 10)]" in source
    assert "[Console]::In.ReadToEnd()" in laptop
    assert "$ExpectedPrimaryHost = '100.70.103.11'" in laptop
    assert "$ExpectedDatabaseUser = 'takealot_app'" in laptop
    assert "database_url_persisted = $false" in laptop
    assert "StandardInput.Write($RemoteDatabaseUrl)" in main
    assert "$RemoteRunner = 'D:\\TakealotHA\\bin" in main
    assert "password_recorded" not in main


def test_canary_selector_prefers_recent_successful_healthy_targets() -> None:
    source = _script("prepare_distributed_crawler_canary.py")

    assert "func.max(CompetitorSnapshot.collected_at)" in source
    assert 'CompetitorLinkHealth.status == "healthy"' in source
    assert '"selection": "recent-successful-healthy-targets"' in source


def test_replica_endpoint_switch_requires_a_caught_up_explicit_position() -> None:
    source = _script("switch_laptop_replication_endpoint.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "$currentStatus.Read_Source_Log_Pos -eq $currentStatus.Exec_Source_Log_Pos" in source
    assert "$currentStatus.Source_Log_File -eq $currentStatus.Relay_Source_Log_File" in source
    assert "safe_to_switch = $safeToSwitch" in source
    assert "RESET REPLICA;" in source
    assert "SOURCE_LOG_FILE=$safeLogFileLiteral" in source
    assert "SOURCE_LOG_POS=$safeLogPos" in source
    assert "Replica is not at a fully caught-up switch point" in source


def test_replica_seed_is_consistent_positioned_and_hash_verified() -> None:
    source = _script("create_laptop_replica_seed.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "'--single-transaction'" in source
    assert "'--source-data=2'" in source
    assert "'--set-gtid-purged=OFF'" in source
    assert "Get-FileHash -Algorithm SHA256" in source
    assert "^-- Dump completed on " in source
    assert "SOURCE|MASTER" in source
    assert "seed-validated" in source


def test_replica_reseed_is_laptop_scoped_and_fails_closed() -> None:
    source = _script("reseed_laptop_replica.ps1")

    assert "$ExpectedComputerName = 'LAPTOP-2T5MN8EU'" in source
    assert "$ExpectedDataDirectory = 'D:\\TakealotMySQLReplica\\data\\'" in source
    assert "Get-FileHash -Algorithm SHA256" in source
    assert "Wait-BluePortClosed" in source
    assert "DROP DATABASE IF EXISTS takealot_ops" in source
    assert "RESET REPLICA;" in source
    assert "SOURCE_LOG_FILE=$sourceLogFileLiteral" in source
    assert "SOURCE_LOG_POS=$sourceLogPos" in source
    assert "SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON" in source
    assert "Keep blue stopped" in source
    assert "DROP DATABASE IF EXISTS mysql" not in source
