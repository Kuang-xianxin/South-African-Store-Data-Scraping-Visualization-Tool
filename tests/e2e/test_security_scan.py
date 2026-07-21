from __future__ import annotations

from pathlib import Path

from takealot_ops.cli import main

from .release_support import FIXTURE_API_KEY, ReleaseFixture


def test_no_output_contains_api_key_or_auth_header(
    release_fixture: ReleaseFixture, monkeypatch
) -> None:
    monkeypatch.setenv("TAKEALOT_PROJECT_ROOT", str(release_fixture.root))
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", release_fixture.settings.database_url)
    monkeypatch.setenv("TAKEALOT_API_KEY", FIXTURE_API_KEY)
    main(["verify", "--date", release_fixture.report_date.isoformat()])

    targets = [release_fixture.html_path, release_fixture.excel_path]
    targets.extend((release_fixture.root / "logs").glob("*.log"))
    forbidden = (FIXTURE_API_KEY.encode(), b"X-API-Key")
    for target in targets:
        content = Path(target).read_bytes()
        assert all(secret not in content for secret in forbidden), target
