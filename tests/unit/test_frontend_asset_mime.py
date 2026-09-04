"""Cross-host MIME contracts for the built ERP frontend."""

from mimetypes import guess_type as default_guess_type
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from takealot_ops.erp.web import create_app


def test_css_assets_override_the_blue_windows_legacy_mime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "asset-mime.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    frontend_dist = tmp_path / "frontend" / "competitor" / "dist"
    assets = frontend_dist / "assets"
    assets.mkdir(parents=True)
    (frontend_dist / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/assets/index-contenthash.css">',
        encoding="utf-8",
    )
    (assets / "index-contenthash.css").write_text(
        ":root { color: #123456; }",
        encoding="utf-8",
    )

    def blue_windows_guess_type(path: str) -> tuple[str | None, str | None]:
        if str(path).casefold().endswith(".css"):
            return "application/x-css", None
        return default_guess_type(path)

    monkeypatch.setattr("starlette.responses.guess_type", blue_windows_guess_type)

    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/assets/index-contenthash.css")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/css; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
