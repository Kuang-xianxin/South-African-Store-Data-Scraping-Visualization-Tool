from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

from takealot_ops.erp.read_cache import ReadProjectionCache


def test_read_projection_cache_reuses_value_until_ttl_or_clear() -> None:
    now = [100.0]
    loads: list[int] = []
    cache = ReadProjectionCache(
        ttl_seconds=20,
        max_entries=4,
        clock=lambda: now[0],
    )

    def load() -> dict[str, int]:
        loads.append(len(loads) + 1)
        return {"revision": loads[-1]}

    assert cache.get_or_load(("summary", ("current",)), load) == {"revision": 1}
    assert cache.get_or_load(("summary", ("current",)), load) == {"revision": 1}
    assert cache.get_or_load(("summary", ("store-02",)), load) == {"revision": 2}

    now[0] += 21
    assert cache.get_or_load(("summary", ("current",)), load) == {"revision": 3}
    cache.clear()
    assert cache.get_or_load(("summary", ("current",)), load) == {"revision": 4}


def test_read_projection_cache_coalesces_same_key_loads() -> None:
    cache = ReadProjectionCache(ttl_seconds=20, max_entries=4)
    callers = 6
    barrier = Barrier(callers)
    load_lock = Lock()
    load_count = 0

    def load() -> dict[str, bool]:
        nonlocal load_count
        with load_lock:
            load_count += 1
        return {"ready": True}

    def read() -> dict[str, bool]:
        barrier.wait()
        return cache.get_or_load(("competitors", ("current", "store-02")), load)

    with ThreadPoolExecutor(max_workers=callers) as executor:
        values = list(executor.map(lambda _: read(), range(callers)))

    assert values == [{"ready": True}] * callers
    assert load_count == 1
