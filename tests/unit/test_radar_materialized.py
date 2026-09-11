"""Incremental, bounded paging is tested independently of ERP/collector writes."""
from dataclasses import fields
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from takealot_ops.erp.radar_list_query import RadarListQuery, list_index, matches_index
from takealot_ops.erp.radar_materialized import MaterializedRadar


def query(**values):
    return RadarListQuery(**({f.name: f.default.default for f in fields(RadarListQuery)} | {"page": 1} | values))


def card(plid, **values):
    return {"plid": str(plid), "来源": "competitor", "商品": f"Electric kettle {plid}",
            "采集时间": "2026-09-08T03:00:00Z", "快照ID": int(plid), "库存数量": None,
            "库存上限": "未探测", "库存精确": False, "跟卖报价": [], "跟卖发现日期": [],
            "周期销售件数": None, "周期补货量": None, "评分": None, **values}


def setup_cache(tmp_path, size=130):
    cache = MaterializedRadar(tmp_path / "projection.sqlite3", namespace="test", batch_size=32)
    records = {str(i): card(i) for i in range(1, size + 1)}
    markers = {p: "1" for p in records}
    version = ["1"]
    calls = []

    def loader(plids):
        calls.append(set(plids))
        return {"items": [records[p] for p in plids], "date_range": {
            "available_start": "2026-09-01", "available_end": "2026-09-08",
            "selected_start": "2026-09-01", "selected_end": "2026-09-08"}}

    options = dict(key="all", boundary="user1/store1/day1", version=lambda: version[0],
                   fingerprints=lambda: dict(markers), loader=loader, field="items", watchlist=set())
    return cache, records, markers, version, calls, options


def test_only_page_bodies_are_decoded_and_filters_sort_across_all_products(tmp_path, monkeypatch):
    cache, records, markers, version, calls, options = setup_cache(tmp_path)
    records["5"].update(周期销售件数=90, 周期补货量=0)
    records["6"].update(周期销售件数=10, 周期补货量=0)
    import takealot_ops.erp.radar_materialized as module
    decode = module.zlib.decompress
    decoded = []
    monkeypatch.setattr(module.zlib, "decompress", lambda body: (decoded.append(1), decode(body))[1])
    try:
        page, refreshing, _ = cache.page(**options, query=query(page=2), prefer_cached=False)
        assert not refreshing and page["pagination"]["total"] == 130
        assert len(page["items"]) == 20 and len(decoded) == 20
        assert len(calls) == 5 and max(map(len, calls)) <= 32
        page, _, _ = cache.page(**options, query=query(q="PLID5", signal="库存减少"), prefer_cached=False)
        assert [i["plid"] for i in page["items"]] == ["5"]
        page, _, _ = cache.page(**options, query=query(signal="库存减少", direction="asc"), prefer_cached=False)
        assert [i["plid"] for i in page["items"]] == ["6", "5"]
        assert len(calls) == 5
    finally:
        cache.close()


def test_incremental_changes_removal_and_disk_restart_do_not_reload_unchanged_history(tmp_path):
    cache, records, markers, version, calls, options = setup_cache(tmp_path)
    cache.page(**options, query=query(), prefer_cached=False)
    calls.clear()
    records["1"]["评分"] = 4.5
    markers["1"] = "2"
    markers.pop("2")
    version[0] = "2"
    try:
        page, _, _ = cache.page(**options, query=query(q="PLID1"), prefer_cached=False)
        assert calls == [{"1"}]
        assert page["pagination"]["source_total"] == 129
    finally:
        cache.close()
    restored = MaterializedRadar(cache.path, namespace="test")
    try:
        page, _, _ = restored.page(**options, query=query(q="PLID1", page_size=100), prefer_cached=False)
        assert calls == [{"1"}]
        assert next(i for i in page["items"] if i["plid"] == "1")["评分"] == 4.5
    finally:
        restored.close()


def test_old_complete_preview_is_available_after_long_idle_and_day_loader_is_replaced(tmp_path):
    cache, records, markers, version, calls, options = setup_cache(tmp_path, 2)
    original, _, _ = cache.page(**options, query=query(), prefer_cached=False)
    with cache._db() as db:
        db.execute("UPDATE scopes SET generated=generated-86400")
    started, finish = Event(), Event()
    old_loader = options["loader"]
    def tomorrow(plids):
        started.set()
        assert finish.wait(4)
        payload = old_loader(plids)
        for item in payload["items"]:
            item["评分"] = 4.5
        return payload
    options["loader"] = tomorrow
    version[0] = "tomorrow"
    try:
        page, refreshing, _ = cache.page(**options, query=query(), prefer_cached=True)
        assert refreshing and page == original and started.wait(2)
        finish.set()
        page, refreshing, _ = cache.page(**options, query=query(), prefer_cached=False)
        assert not refreshing and all(item["评分"] == 4.5 for item in page["items"])
    finally:
        finish.set()
        cache.close()


def test_a_running_generation_keeps_one_loader_when_new_request_arrives(tmp_path):
    cache, records, markers, version, calls, options = setup_cache(tmp_path, 65)
    cache.page(**options, query=query(), prefer_cached=False)
    version[0] = "2"
    started, finish = Event(), Event()
    original = options["loader"]
    old_calls, new_calls = [], []
    def yesterday(plids):
        old_calls.append(plids)
        started.set()
        assert finish.wait(4)
        return original(plids)
    def today(plids):
        new_calls.append(plids)
        return original(plids)
    try:
        cache.page(**(options | {"loader": yesterday}), query=query(), prefer_cached=True)
        assert started.wait(2)
        cache.page(**(options | {"loader": today}), query=query(), prefer_cached=True)
        finish.set()
        next(iter(cache._works.values())).future.result(timeout=4)
        assert len(old_calls) == 3 and not new_calls
        version[0] = "3"
        cache.page(**(options | {"loader": today}), query=query(), prefer_cached=False)
        assert len(new_calls) == 3
    finally:
        finish.set()
        cache.close()


def test_failed_generation_rolls_back_and_authorization_cannot_reuse_preview(tmp_path):
    cache, records, markers, version, calls, options = setup_cache(tmp_path, 2)
    original, _, _ = cache.page(**options, query=query(), prefer_cached=False)
    version[0] = "2"
    markers["1"] = "2"
    started, finish = Event(), Event()
    def broken(_):
        started.set()
        assert finish.wait(4)
        raise RuntimeError("database unavailable")
    # A work registration deliberately holds its loader. Mutate its source closure.
    good_loader = options["loader"]
    options["loader"] = broken
    with cache._lock:
        for work in cache._works.values():
            work.loader = broken
    try:
        preview, refreshing, _ = cache.page(**options, query=query(), prefer_cached=True)
        assert refreshing and preview == original and started.wait(2)
        with ThreadPoolExecutor() as pool:
            waiting = pool.submit(cache.page, **options, query=query(), prefer_cached=False)
            finish.set()
            with pytest.raises(RuntimeError, match="database unavailable"):
                waiting.result(timeout=4)
        with pytest.raises(RuntimeError, match="database unavailable"):
            cache.page(**(options | {"boundary": "other-user"}), query=query(), prefer_cached=True)
        assert cache._metadata(next(iter(cache._works)))[0] == "1"
        options["loader"] = good_loader
        assert cache.page(**options, query=query(), prefer_cached=False)[0]["items"] == original["items"]
    finally:
        finish.set()
        cache.close()


def test_search_typo_seller_followers_and_same_own_offer_constraints():
    item = card(1, 商品="Café Electric Kettle", 跟卖报价=[
        {"卖家ID": "M123", "卖家": "Shop Alpha", "是否变体主报价": True, "是否跟卖": False}])
    index = list_index(item)
    assert matches_index(index, query(q="cafe eletric"), set())
    assert matches_index(index, query(seller="sellers=123"), set())
    assert matches_index(index, query(follower="未发现跟卖"), set())
    assert not matches_index(index, query(follower="现在被跟卖"), set())
    assert not matches_index(index, query(stock="没货"), set())
    assert not matches_index(index, query(watchlist=True), set())
    assert matches_index(index, query(watchlist=True), {"1"})
    own = list_index(card(2, 来源="own_store", 对比报价=[
        {"报价来源": "seller_api", "最新Offer状态": "buyable", "最新Offer库存状态": "有货"},
        {"报价来源": "seller_api", "最新Offer状态": "disabled_by_seller", "最新Offer库存状态": "没货"}]))
    assert matches_index(own, query(status="buyable", stock="有货"), set())
    assert not matches_index(own, query(status="buyable", stock="没货"), set())


def test_cold_warming_returns_without_waiting_and_other_partition_can_finish(tmp_path):
    slow, _, _, _, _, options = setup_cache(tmp_path, 1)
    fast = MaterializedRadar(tmp_path / "own.sqlite3", namespace="own", batch_size=32)
    entered, finish = Event(), Event()
    good = options["loader"]

    def blocked(plids):
        entered.set()
        assert finish.wait(5)
        return good(plids)

    try:
        assert slow.page(**(options | {"loader": blocked}), query=query(),
                         prefer_cached=True, prepare_only=True) is None
        assert entered.wait(2)
        page, _, _ = fast.page(**options, query=query(), prefer_cached=False)
        assert page["pagination"]["total"] == 1
        assert not finish.is_set()
    finally:
        finish.set()
        slow.close()
        fast.close()


def test_full_preparation_capacity_waits_without_500_or_evicting_a_reader(tmp_path):
    cache, _, _, _, _, options = setup_cache(tmp_path, 1)
    cache.max_scopes = 1
    entered, finish = Event(), Event()
    good = options["loader"]

    def blocked(plids):
        entered.set()
        assert finish.wait(5)
        return good(plids)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.page, **(options | {"loader": blocked}),
                                query=query(), prefer_cached=False)
            assert entered.wait(2)
            assert cache.page(**(options | {"boundary": "other"}), query=query(),
                              prefer_cached=True, prepare_only=True) is None
            second = pool.submit(cache.page, **(options | {"boundary": "other"}),
                                 query=query(), prefer_cached=False)
            assert not second.done()
            finish.set()
            assert first.result(timeout=4)[0]["pagination"]["total"] == 1
            assert second.result(timeout=4)[0]["pagination"]["total"] == 1
            assert len(cache._works) == 1 and not cache._readers
    finally:
        finish.set()
        cache.close()


@pytest.mark.parametrize("window", ["7", "15", "30", "60", "90", "total"])
@pytest.mark.parametrize("source,field,prefix", [
    ("competitor", "近期观察售出", "sales"),
    ("own_store", "自有官方销量", "sales"),
    ("own_store", "跟卖近期观察售出", "follower_sales"),
])
def test_sales_sort_uses_displayed_metric_before_paging_and_preserves_missing(
    tmp_path, window, source, field, prefix,
):
    cache, records, markers, version, calls, options = setup_cache(tmp_path, 45)
    values = [10, None, 2, 100, 0, None, 2] * 6 + [None, 7, 1]
    for index, row in enumerate(records.values()):
        # Other sales sources and the selected operating signal deliberately disagree.
        row.update(来源=source, 周期销售件数=1000-index, 周期补货量=0,
                   自有官方销量={window: 9000-index},
                   跟卖近期观察售出={window: 5000-index},
                   近期观察售出={window: 3000-index})
        row[field] = {window: values[index]} if index != 1 else {}
    try:
        for direction in ("asc", "desc"):
            for signal in ("全部", "库存减少"):
                actual = []
                for number in range(1, 4):
                    result, _, _ = cache.page(**options, prefer_cached=False, query=query(
                        page=number, page_size=20, sort=f"{prefix}_{window}",
                        direction=direction, signal=signal))
                    assert result["pagination"]["total"] == 45
                    actual.extend(result["items"])
                expected = sorted(records.values(), key=lambda row: int(row["plid"]), reverse=True)
                expected.sort(key=lambda row: (
                    row[field].get(window) is None,
                    (row[field].get(window) or 0) * (1 if direction == "asc" else -1)))
                assert [row["plid"] for row in actual] == [row["plid"] for row in expected]
                assert [row[field].get(window) for row in actual] == [row[field].get(window) for row in expected]
        # Changing sort/page never reloads history, and filters remain independent.
        result, _, _ = cache.page(**options, prefer_cached=False, query=query(
            q="PLID44", sort=f"{prefix}_{window}", direction="asc"))
        assert [row["plid"] for row in result["items"]] == ["44"]
        assert len(calls) == 2
    finally:
        cache.close()
