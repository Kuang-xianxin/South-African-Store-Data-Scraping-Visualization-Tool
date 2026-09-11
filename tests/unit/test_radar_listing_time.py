from datetime import date
from pathlib import Path

import pytest

from takealot_ops.erp.web import _own_store_sales_comparison_records


@pytest.mark.parametrize(
    ("sources", "expected_at"),
    [
        (["platform", "platform"], "2026-08-01T09:00:00+08:00"),
        (["first_observed", "platform"], "2026-08-02T09:00:00+08:00"),
        (["first_observed", "first_observed"], None),
        ([], None),
    ],
)
def test_own_card_listing_time_uses_only_platform_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sources: list[str],
    expected_at: str | None,
) -> None:
    database_path = tmp_path / "listing-times.db"
    database_path.touch()
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    series = [
        {
            "store_code": f"store-{index}",
            "plid": "own-plid",
            "listing_date": f"2026-08-0{index + 1}",
            "listing_at": f"2026-08-0{index + 1}T09:00:00+08:00",
            "listing_date_source": source,
            "through_date": "2026-08-03",
            "points": [],
        }
        for index, source in enumerate(sources)
    ]
    monkeypatch.setattr(
        "takealot_ops.erp.web.build_own_store_sales_series_bulk",
        lambda _session, **_kwargs: {"own-plid": list(reversed(series))},
    )
    records = _own_store_sales_comparison_records(
        tmp_path,
        [{"plid": "own-plid"}],
        own_store_codes={"store-0", "store-1"},
        through=date(2026, 8, 3),
    )
    assert records[0]["自有上架时间"] == expected_at
    assert records[0]["自有上架日期"] == (expected_at[:10] if expected_at else None)
    # Clearing the accessible store scope cannot retain another scope's dates.
    restricted = _own_store_sales_comparison_records(
        tmp_path, records, own_store_codes=set(), through=date(2026, 8, 3),
    )
    assert restricted[0]["自有上架时间"] is None
    assert restricted[0]["自有上架日期"] is None
