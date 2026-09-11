from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import ErpDataRevision


def test_update_feed_is_read_only_permission_filtered_and_store_scoped(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'updates.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    engine = create_engine_for_database_url(database_url)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/erp/data-updates").status_code == 401
        bootstrap = client.post("/api/auth/bootstrap", json={
            "username": "kxx", "display_name": "Admin", "password": "test-pass-123",
        })
        assert bootstrap.status_code == 200
        csrf = bootstrap.json()["csrf_token"]
        with engine.begin() as connection:
            connection.execute(insert(ErpDataRevision), [
                dict(scope="current", topic="store", revision="a"),
                dict(scope="not-assigned", topic="store", revision="b"),
            ])
        app.state.data_revisions.invalidate()
        initial = client.get("/api/erp/data-updates").json()
        assert len(initial["versions"]) == 13
        assert initial["poll_after_ms"] == 15_000
        with engine.begin() as connection:
            connection.execute(update(ErpDataRevision).where(
                ErpDataRevision.scope == "not-assigned",
            ).values(revision="changed"))
        app.state.data_revisions.invalidate()
        assert client.get("/api/erp/data-updates").json()["versions"] == initial["versions"]
        assert client.get("/api/erp/data-updates", headers={"X-Store-Code": "not-assigned"}).status_code == 403
        with engine.connect() as connection:
            before = connection.execute(select(ErpDataRevision)).all()
        for _ in range(3):
            assert client.get("/api/erp/data-updates").status_code == 200
        with engine.connect() as connection:
            assert connection.execute(select(ErpDataRevision)).all() == before
        created = client.post("/api/auth/users", headers={"X-CSRF-Token": csrf}, json={
            "username": "selection-only", "display_name": "Selection", "password": "test-pass-456",
            "role": "selection", "permissions": ["competitors.view"],
            "all_stores": False, "store_ids": [],
        })
        assert created.status_code == 200
        assert client.post("/api/auth/login", json={
            "username": "selection-only", "password": "test-pass-456",
        }).status_code == 200
        result = client.get("/api/erp/data-updates")
        assert result.status_code == 200
        assert set(result.json()["versions"]) == {
            "competitors", "competitors:current", "competitors:all", "competitors:operating",
        }
        assert result.json()["freshness"] == {"last_collection_at": None, "latest_metric_date": None}
    engine.dispose()
