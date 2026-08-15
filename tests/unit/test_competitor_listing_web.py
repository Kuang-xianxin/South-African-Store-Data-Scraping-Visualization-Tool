from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.erp.web import create_app
from takealot_ops.storage.models import CompetitorTarget, ErpStore, OfferCurrent
from takealot_ops.storage.store_context import store_scope


def _listing_payload() -> dict[str, object]:
    sorts = (
        "Relevance",
        "Price Descending",
        "Price Ascending",
        "Rating Descending",
        "ReleaseDate Descending",
    )
    return {
        "sections": {
            "products": {
                "results": [
                    {
                        "type": "product_views",
                        "product_views": {
                            "core": {
                                "id": str(plid),
                                "title": f"Listing Product {plid}",
                                "slug": f"listing-product-{plid}",
                            }
                        },
                    }
                    for plid in range(1, 26)
                ],
                "paging": {"total_num_found": 25, "next_is_after": None},
            },
            "sort_options": {
                "results": [
                    {
                        "type": "sort_option",
                        "sort_option": {
                            "param_value": value,
                            "display_value": value,
                        },
                    }
                    for value in sorts
                ]
            },
        }
    }


class _FakePublicClient:
    started = 0

    def __init__(self, **_: object) -> None:
        self.ready = False

    async def start(self) -> None:
        type(self).started += 1
        self.ready = True

    async def close(self) -> None:
        self.ready = False

    async def fetch_listing_first_page(
        self,
        source_url: str,
    ) -> tuple[str, dict[str, Any]]:
        return (
            "https://api.takealot.com/rest/v-1-18-0/searches/products,filters"
            "?sellers=29853614",
            _listing_payload(),
        )

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        raise AssertionError((request_url, after))


def test_listing_preview_requires_count_then_commits_deduplicated_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "listing-source.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorPublicClient", _FakePublicClient)
    monkeypatch.setattr(
        "takealot_ops.erp.web._competitor_link_cooldown_seconds",
        lambda *_: 0.0,
    )
    _FakePublicClient.started = 0
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "kxx",
                "display_name": "KXX Admin",
                "password": "pass-123",
            },
        )
        assert login.status_code == 200
        headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}

        default_library = client.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "默认单链接库"},
        ).json()["library"]
        selected_library = client.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "本次店铺候选库"},
        ).json()["library"]
        default_saved = client.put(
            "/api/competitors/personal-watchlist/settings",
            headers=headers,
            json={"default_library_id": default_library["id"]},
        )
        assert default_saved.status_code == 200

        existing = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://www.takealot.com/existing/PLID1"},
        )
        assert existing.status_code == 200
        engine = create_engine(database_url)
        with store_scope("current"), Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="own-offer-2",
                    productline_id="2",
                    sku="OWN-2",
                    title="Own product 2",
                    selling_price=199,
                    total_stock=4,
                    captured_at=datetime.now(UTC),
                )
            )
            now = datetime.now(UTC)
            session.add(
                CompetitorTarget(
                    plid="4",
                    offer_group_plid="4",
                    url="https://www.takealot.com/old-product-4/PLID4",
                    title="Old Product 4",
                    active=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        engine.dispose()

        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "listing-batch",
                "client_id": "listing-client",
                "event": "start",
                "completed": 0,
                "total": 1,
                "pending": 1,
            },
        )
        assert started.status_code == 200

        needs_count = client.post(
            "/api/competitors/listing-preview",
            headers=headers,
            json={
                "source_type": "seller",
                "url": (
                    "https://www.takealot.com/seller/techitstore?sellers=29853614"
                ),
                "price_min": 100,
                "price_max": 1000,
                "sorts": ["Relevance", "Price Ascending"],
            },
        )
        assert needs_count.status_code == 200
        assert needs_count.json()["source_total"] == 25
        assert needs_count.json()["requires_limit"] is True
        assert needs_count.json()["selected_count"] == 20
        assert needs_count.json()["candidate_capacity"] == 25
        assert needs_count.json()["candidate_queue_frozen"] is True
        assert needs_count.json()["preview_token"]
        assert client.get("/api/competitors/listing-operations").json()["total"] == 0

        missing_library = client.post(
            "/api/competitors/listing-targets",
            headers=headers,
            json={"preview_token": needs_count.json()["preview_token"], "product_limit": 4},
        )
        assert missing_library.status_code == 422

        invalid_library = client.post(
            "/api/competitors/listing-targets",
            headers=headers,
            json={
                "preview_token": needs_count.json()["preview_token"],
                "library_id": 999999,
                "product_limit": 4,
            },
        )
        assert invalid_library.status_code == 422
        assert "当前账号拥有" in invalid_library.json()["detail"]
        assert client.get("/api/competitors/listing-operations").json()["total"] == 0

        committed = client.post(
            "/api/competitors/listing-targets",
            headers=headers,
            json={
                "preview_token": needs_count.json()["preview_token"],
                "library_id": selected_library["id"],
                "product_limit": 4,
            },
        )
        assert committed.status_code == 200
        committed_payload = committed.json()
        operation_id = committed_payload.pop("operation_id")
        assert isinstance(operation_id, int)
        assert operation_id > 0
        assert committed_payload == {
            "source_type": "seller",
            "source_url": (
                "https://www.takealot.com/seller/techitstore?sellers=29853614"
            ),
            "personal_library_id": selected_library["id"],
            "personal_library_name": "本次店铺候选库",
            "selected_count": 4,
            "added_target_count": 2,
            "reactivated_target_count": 1,
            "existing_target_count": 1,
            "own_store_count": 1,
            "personal_watchlist_added_count": 3,
            "queued_to_active_batch_count": 2,
        }
        reused = client.post(
            "/api/competitors/listing-targets",
            headers=headers,
            json={
                "preview_token": needs_count.json()["preview_token"],
                "library_id": selected_library["id"],
                "product_limit": 4,
            },
        )
        assert reused.status_code == 409
        assert _FakePublicClient.started == 1

        targets = client.get("/api/competitors/targets").json()["items"]
        assert {item["plid"] for item in targets} == {"1", "3", "4"}
        assert next(item for item in targets if item["plid"] == "3")["title"] == (
            "Listing Product 3"
        )
        personal = client.get("/api/competitors/personal-watchlist").json()["items"]
        assert {item["plid"] for item in personal} == {"1", "2", "3", "4"}
        personal_by_plid = {item["plid"]: item for item in personal}
        assert all(
            selected_library["id"] in personal_by_plid[plid]["library_ids"]
            for plid in {"1", "2", "3", "4"}
        )
        assert default_library["id"] in personal_by_plid["1"]["library_ids"]
        assert all(
            default_library["id"] not in personal_by_plid[plid]["library_ids"]
            for plid in {"2", "3", "4"}
        )
        batch = client.get("/api/competitors/batch-status").json()
        assert [item["plid"] for item in batch["queued_targets"]] == ["3", "4"]
        assert batch["total"] == 3
        assert batch["pending"] == 3

        operations = client.get(
            "/api/competitors/listing-operations",
            params={"source_type": "seller", "page": 1, "page_size": 10},
        )
        assert operations.status_code == 200
        operations_payload = operations.json()
        assert operations_payload["total"] == 1
        operation = operations_payload["items"][0]
        assert operation == {
            "id": operation_id,
            "source_type": "seller",
            "source_url": (
                "https://www.takealot.com/seller/techitstore?sellers=29853614"
            ),
            "source_label": "techitstore",
            "personal_library_id": selected_library["id"],
            "personal_library_name": "本次店铺候选库",
            "price_min": 100,
            "price_max": 1000,
            "sorts": ["Relevance", "Price Ascending"],
            "selection_rule": "balanced_rank_fusion_then_plid_deduplicate",
            "product_limit": 4,
            "selected_count": 4,
            "added_target_count": 2,
            "reactivated_target_count": 1,
            "existing_target_count": 1,
            "own_store_count": 1,
            "personal_watchlist_added_count": 3,
            "actor_username": "kxx",
            "actor_display_name": "KXX Admin",
            "committed_at": operation["committed_at"],
        }
        operation_items = client.get(
            f"/api/competitors/listing-operations/{operation_id}/items",
            params={"page": 1, "page_size": 2},
        )
        assert operation_items.status_code == 200
        first_item_page = operation_items.json()
        assert first_item_page["total"] == 4
        assert [item["position"] for item in first_item_page["items"]] == [1, 2]
        assert [item["result"] for item in first_item_page["items"]] == [
            "existing_target",
            "own_store",
        ]
        assert first_item_page["items"][0]["url"] == (
            "https://www.takealot.com/listing-product-1/PLID1"
        )
        assert first_item_page["items"][0]["sort_ranks"] == {
            "Relevance": 1,
            "Price Ascending": 1,
        }
        second_item_page = client.get(
            f"/api/competitors/listing-operations/{operation_id}/items",
            params={"page": 2, "page_size": 2},
        ).json()
        assert [item["result"] for item in second_item_page["items"]] == [
            "added_target",
            "reactivated_target",
        ]


def test_listing_commit_blocks_unauthorized_own_store_candidate_without_rescan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "listing-source-store-scope.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorPublicClient", _FakePublicClient)
    _FakePublicClient.started = 0
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        bootstrap = admin.post(
            "/api/auth/bootstrap",
            json={
                "username": "kxx",
                "display_name": "KXX Admin",
                "password": "pass-123",
            },
        )
        assert bootstrap.status_code == 200
        admin_headers = {"X-CSRF-Token": str(bootstrap.json()["csrf_token"])}
        engine = create_engine(database_url)
        now = datetime(2026, 8, 11, 5, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            current_store = session.scalar(
                select(ErpStore).where(ErpStore.code == "current")
            )
            assert current_store is not None
            current_store_id = current_store.id
            session.add(
                ErpStore(
                    code="store-02",
                    display_name="Restricted Store",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        with store_scope("store-02"), Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="restricted-own-offer",
                    productline_id="2",
                    sku="RESTRICTED-2",
                    title="Restricted Own Product",
                    selling_price=199,
                    total_stock=4,
                    captured_at=now,
                )
            )
        engine.dispose()

        created_user = admin.post(
            "/api/auth/users",
            headers=admin_headers,
            json={
                "username": "listing.operator",
                "display_name": "Listing Operator",
                "password": "operator-password-123",
                "role": "operator",
                "permissions": ["competitors.view", "competitors.collect"],
                "all_stores": False,
                "store_ids": [current_store_id],
            },
        )
        assert created_user.status_code == 200

        with TestClient(app, client=("192.168.1.8", 50001)) as operator:
            login = operator.post(
                "/api/auth/login",
                json={
                    "username": "listing.operator",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
            library = operator.post(
                "/api/competitors/personal-watchlist/libraries",
                headers=headers,
                json={"name": "Authorized Products"},
            )
            assert library.status_code == 200
            library_id = int(library.json()["library"]["id"])
            preview = operator.post(
                "/api/competitors/listing-preview",
                headers=headers,
                json={
                    "source_type": "seller",
                    "url": (
                        "https://www.takealot.com/seller/techitstore?sellers=29853614"
                    ),
                    "sorts": ["Relevance"],
                },
            )
            assert preview.status_code == 200
            preview_token = preview.json()["preview_token"]

            denied = operator.post(
                "/api/competitors/listing-targets",
                headers=headers,
                json={
                    "preview_token": preview_token,
                    "library_id": library_id,
                    "product_limit": 4,
                },
            )
            assert denied.status_code == 403
            assert "PLID2" in denied.json()["detail"]
            assert "无权查看店铺的自有商品" in denied.json()["detail"]
            assert "Restricted Store" not in denied.json()["detail"]
            assert admin.get(
                "/api/competitors/listing-operations"
            ).json()["total"] == 0
            assert operator.get(
                "/api/competitors/personal-watchlist"
            ).json()["count"] == 0
            assert operator.get("/api/competitors/targets").json()["items"] == []

            retried = operator.post(
                "/api/competitors/listing-targets",
                headers=headers,
                json={
                    "preview_token": preview_token,
                    "library_id": library_id,
                    "product_limit": 1,
                },
            )
            assert retried.status_code == 200
            assert retried.json()["selected_count"] == 1
            assert retried.json()["added_target_count"] == 1
            assert retried.json()["own_store_count"] == 0

    assert _FakePublicClient.started == 1


def test_listing_preview_rejects_wrong_link_type_before_starting_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'wrong-type.db').as_posix()}",
    )
    _FakePublicClient.started = 0
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorPublicClient", _FakePublicClient)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "kxx",
                "display_name": "KXX Admin",
                "password": "pass-123",
            },
        )
        response = client.post(
            "/api/competitors/listing-preview",
            headers={"X-CSRF-Token": str(login.json()["csrf_token"])},
            json={
                "source_type": "category",
                "url": (
                    "https://www.takealot.com/seller/techitstore?sellers=29853614"
                ),
            },
        )

    assert response.status_code == 422
    assert "只接受 Takealot 类目链接" in response.json()["detail"]
    assert _FakePublicClient.started == 0
