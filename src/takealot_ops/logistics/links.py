"""Candidate matching and durable operator confirmation for logistics relationships."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from itertools import combinations
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    ErpStore,
    LogisticsShipmentLink,
    LogisticsShipmentLinkAudit,
    OfferCurrent,
    OfferSnapshot,
)
from takealot_ops.storage.store_context import current_store_code


HIGH_CONFIDENCE_WINDOW_DAYS = 30
REVIEW_CANDIDATE_WINDOW_DAYS = 60
LOW_CONFIDENCE_MIN_OVERLAP = 0.5
LOW_CONFIDENCE_LIMIT = 100
SPLIT_GROUP_LIMIT = 50


class LogisticsLinkError(RuntimeError):
    """Raised when a candidate can no longer be confirmed or a link cannot be changed."""


def load_offer_sku_map(engine: Engine) -> dict[str, str]:
    """Resolve each Takealot offer ID to one stable seller SKU across current/history rows."""
    values: defaultdict[str, set[str]] = defaultdict(set)
    with Session(engine) as session:
        for model in (OfferCurrent, OfferSnapshot):
            rows = session.execute(select(model.offer_id, model.sku).distinct())
            for offer_id, sku in rows:
                normalized_offer = _identity(offer_id)
                normalized_sku = _identity(sku)
                if normalized_offer and normalized_sku:
                    values[normalized_offer].add(normalized_sku)
    return {
        offer_id: next(iter(skus))
        for offer_id, skus in values.items()
        if len(skus) == 1
    }


def build_high_confidence_candidates(
    inbound: Sequence[Mapping[str, Any]],
    shipments: Sequence[Mapping[str, Any]],
    offer_skus: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Return only mutually unique exact SKU/quantity matches inside a calendar-date window."""
    return build_logistics_candidates(inbound, shipments, offer_skus)["high"]


def build_logistics_candidates(
    inbound: Sequence[Mapping[str, Any]],
    shipments: Sequence[Mapping[str, Any]],
    offer_skus: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Build strict, review-only, and possible split relationship evidence."""
    inbound_profiles = [
        profile
        for row in inbound
        if (profile := _w8_profile(row)) is not None
    ]
    shipment_profiles = [
        profile
        for row in shipments
        if (profile := _takealot_profile(row, offer_skus)) is not None
    ]
    high_edges: list[tuple[int, int, int]] = []
    medium_edges: list[tuple[int, int, int, float]] = []
    low_edges: list[tuple[int, int, int, float]] = []
    for inbound_index, inbound_profile in enumerate(inbound_profiles):
        for shipment_index, shipment_profile in enumerate(shipment_profiles):
            date_gap = abs(
                (inbound_profile["created_date"] - shipment_profile["created_date"]).days
            )
            inbound_skus = {sku for sku, _ in inbound_profile["signature"]}
            shipment_skus = {sku for sku, _ in shipment_profile["signature"]}
            shared_skus = inbound_skus & shipment_skus
            if not shared_skus:
                continue
            overlap_ratio = len(shared_skus) / max(len(inbound_skus), len(shipment_skus))
            if (
                inbound_profile["signature"] == shipment_profile["signature"]
                and date_gap <= HIGH_CONFIDENCE_WINDOW_DAYS
            ):
                high_edges.append((inbound_index, shipment_index, date_gap))
            elif (
                inbound_skus == shipment_skus
                and date_gap <= REVIEW_CANDIDATE_WINDOW_DAYS
            ):
                medium_edges.append(
                    (inbound_index, shipment_index, date_gap, overlap_ratio)
                )
            elif (
                overlap_ratio >= LOW_CONFIDENCE_MIN_OVERLAP
                and date_gap <= REVIEW_CANDIDATE_WINDOW_DAYS
            ):
                low_edges.append((inbound_index, shipment_index, date_gap, overlap_ratio))

    inbound_degree = Counter(inbound_index for inbound_index, _, _ in high_edges)
    shipment_degree = Counter(shipment_index for _, shipment_index, _ in high_edges)
    high_candidates: list[dict[str, Any]] = []
    for inbound_index, shipment_index, date_gap in high_edges:
        if inbound_degree[inbound_index] != 1 or shipment_degree[shipment_index] != 1:
            continue
        high_candidates.append(
            _candidate_payload(
                inbound_profiles[inbound_index],
                shipment_profiles[shipment_index],
                confidence="high",
                method="完整卖家SKU及各SKU发送数量一致，双方候选唯一，建单日期相差不超过30天",
                date_gap=date_gap,
                overlap_ratio=1.0,
                inbound_candidate_count=1,
                shipment_candidate_count=1,
            )
        )

    medium_candidates = _review_candidates(
        inbound_profiles,
        shipment_profiles,
        medium_edges,
        confidence="medium",
        method="完整卖家SKU组合一致，但发送数量不同；建单日期相差不超过60天",
    )
    low_candidates = _review_candidates(
        inbound_profiles,
        shipment_profiles,
        low_edges,
        confidence="low",
        method="双方至少一半SKU重合，但整单SKU组合不同；建单日期相差不超过60天",
    )[:LOW_CONFIDENCE_LIMIT]
    split_groups = _split_batch_groups(inbound_profiles, shipment_profiles)
    return {
        "high": _sort_candidates(high_candidates),
        "medium": _sort_candidates(medium_candidates),
        "low": _sort_candidates(low_candidates),
        "split_groups": split_groups,
    }


def _review_candidates(
    inbound_profiles: Sequence[Mapping[str, Any]],
    shipment_profiles: Sequence[Mapping[str, Any]],
    edges: Sequence[tuple[int, int, int, float]],
    *,
    confidence: str,
    method: str,
) -> list[dict[str, Any]]:
    inbound_degree = Counter(inbound_index for inbound_index, _, _, _ in edges)
    shipment_degree = Counter(shipment_index for _, shipment_index, _, _ in edges)
    return [
        _candidate_payload(
            inbound_profiles[inbound_index],
            shipment_profiles[shipment_index],
            confidence=confidence,
            method=method,
            date_gap=date_gap,
            overlap_ratio=overlap_ratio,
            inbound_candidate_count=inbound_degree[inbound_index],
            shipment_candidate_count=shipment_degree[shipment_index],
        )
        for inbound_index, shipment_index, date_gap, overlap_ratio in edges
    ]


def _candidate_payload(
    inbound_profile: Mapping[str, Any],
    shipment_profile: Mapping[str, Any],
    *,
    confidence: str,
    method: str,
    date_gap: int,
    overlap_ratio: float,
    inbound_candidate_count: int,
    shipment_candidate_count: int,
) -> dict[str, Any]:
    inbound_signature = tuple(inbound_profile["signature"])
    shipment_signature = tuple(shipment_profile["signature"])
    inbound_skus = {sku for sku, _ in inbound_signature}
    shipment_skus = {sku for sku, _ in shipment_signature}
    inbound_quantity = sum(quantity for _, quantity in inbound_signature)
    shipment_quantity = sum(quantity for _, quantity in shipment_signature)
    return {
        "confidence": confidence,
        "method": method,
        "w8_order_no": inbound_profile["order_no"],
        "w8_headway_no": inbound_profile["headway_no"],
        "w8_shipping_mark": inbound_profile["shipping_mark"],
        "w8_status": inbound_profile["status"],
        "w8_created_at": inbound_profile["created_at"],
        "takealot_shipment_id": shipment_profile["shipment_id"],
        "takealot_purchase_order_number": shipment_profile["purchase_order_number"],
        "takealot_reference": shipment_profile["reference"],
        "takealot_state": shipment_profile["state"],
        "takealot_created_at": shipment_profile["created_at"],
        "sku_lines": len(inbound_signature),
        "w8_sku_lines": len(inbound_signature),
        "takealot_sku_lines": len(shipment_signature),
        "shared_sku_lines": len(inbound_skus & shipment_skus),
        "overlap_ratio": round(overlap_ratio, 4),
        "quantity": inbound_quantity,
        "w8_quantity": inbound_quantity,
        "takealot_quantity": shipment_quantity,
        "quantity_delta": shipment_quantity - inbound_quantity,
        "date_gap_days": date_gap,
        "w8_candidate_count": inbound_candidate_count,
        "takealot_candidate_count": shipment_candidate_count,
        "ambiguous": inbound_candidate_count > 1 or shipment_candidate_count > 1,
    }


def _sort_candidates(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda row: (str(row["w8_created_at"]), str(row["w8_order_no"])),
        reverse=True,
    )


def _split_batch_groups(
    inbound_profiles: Sequence[Mapping[str, Any]],
    shipment_profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for inbound_profile in inbound_profiles:
        inbound_quantities = dict(inbound_profile["signature"])
        inbound_skus = set(inbound_quantities)
        eligible = [
            shipment_profile
            for shipment_profile in shipment_profiles
            if set(dict(shipment_profile["signature"])).issubset(inbound_skus)
            and abs(
                (inbound_profile["created_date"] - shipment_profile["created_date"]).days
            )
            <= REVIEW_CANDIDATE_WINDOW_DAYS
        ]
        found_for_order = False
        for group_size in (2, 3):
            for shipment_group in combinations(eligible, group_size):
                totals = {
                    sku: sum(
                        dict(shipment_profile["signature"]).get(sku, 0)
                        for shipment_profile in shipment_group
                    )
                    for sku in inbound_skus
                }
                if totals != inbound_quantities:
                    continue
                groups.append(
                    {
                        "w8_order_no": inbound_profile["order_no"],
                        "w8_created_at": inbound_profile["created_at"],
                        "w8_quantity": sum(inbound_quantities.values()),
                        "sku_lines": len(inbound_quantities),
                        "takealot_shipment_ids": [
                            shipment_profile["shipment_id"]
                            for shipment_profile in shipment_group
                        ],
                        "takealot_purchase_order_numbers": [
                            shipment_profile["purchase_order_number"]
                            for shipment_profile in shipment_group
                        ],
                        "shipment_count": group_size,
                        "max_date_gap_days": max(
                            abs(
                                (
                                    inbound_profile["created_date"]
                                    - shipment_profile["created_date"]
                                ).days
                            )
                            for shipment_profile in shipment_group
                        ),
                        "method": "多个Takealot Shipment的完整SKU数量合计与一个长睿入库单一致",
                    }
                )
                found_for_order = True
                break
            if found_for_order:
                break
        if len(groups) >= SPLIT_GROUP_LIMIT:
            break
    return sorted(
        groups,
        key=lambda row: (str(row["w8_created_at"]), str(row["w8_order_no"])),
        reverse=True,
    )


def list_confirmed_links(engine: Engine) -> list[dict[str, Any]]:
    """Return active confirmations for the current connected store."""
    with Session(engine) as session:
        store_id = _current_store_id(session)
        links = session.scalars(
            select(LogisticsShipmentLink)
            .where(
                LogisticsShipmentLink.store_id == store_id,
                LogisticsShipmentLink.active.is_(True),
            )
            .order_by(
                LogisticsShipmentLink.confirmed_at.desc(),
                LogisticsShipmentLink.id.desc(),
            )
        ).all()
        return [_link_payload(link) for link in links]


def confirm_candidate_link(
    engine: Engine,
    candidate: Mapping[str, Any],
    *,
    actor_user_id: int | None,
    actor_username: str,
) -> dict[str, Any]:
    """Idempotently confirm one currently generated review candidate."""
    now = datetime.utcnow()
    order_no = str(candidate.get("w8_order_no") or "").strip()
    shipment_id = _integer(candidate.get("takealot_shipment_id"))
    if not order_no or shipment_id is None:
        raise LogisticsLinkError("物流候选缺少长睿单号或 Takealot Shipment ID")
    evidence = dict(candidate)
    with Session(engine) as session, session.begin():
        store_id = _current_store_id(session)
        link = session.scalar(
            select(LogisticsShipmentLink).where(
                LogisticsShipmentLink.store_id == store_id,
                LogisticsShipmentLink.w8_order_no == order_no,
                LogisticsShipmentLink.takealot_shipment_id == shipment_id,
            )
        )
        if link is not None and link.active:
            return _link_payload(link)
        action = "reconfirmed" if link is not None else "confirmed"
        if link is None:
            link = LogisticsShipmentLink(
                store_id=store_id,
                w8_order_no=order_no,
                takealot_shipment_id=shipment_id,
                evidence=evidence,
                active=True,
                confirmed_by_user_id=actor_user_id,
                confirmed_by_username=actor_username,
                confirmed_at=now,
            )
            session.add(link)
            session.flush()
        else:
            link.evidence = evidence
            link.active = True
            link.confirmed_by_user_id = actor_user_id
            link.confirmed_by_username = actor_username
            link.confirmed_at = now
            link.revoked_by_user_id = None
            link.revoked_by_username = None
            link.revoked_at = None
            link.revoke_note = None
        session.add(
            LogisticsShipmentLinkAudit(
                link_id=link.id,
                action=action,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                note=None,
                evidence=evidence,
                created_at=now,
            )
        )
        session.flush()
        return _link_payload(link)


def revoke_confirmed_link(
    engine: Engine,
    link_id: int,
    *,
    actor_user_id: int | None,
    actor_username: str,
    note: str,
) -> dict[str, Any]:
    """Revoke an active confirmation while retaining its row and append-only audit."""
    normalized_note = note.strip()
    if not normalized_note:
        raise LogisticsLinkError("撤销关联必须填写原因")
    now = datetime.utcnow()
    with Session(engine) as session, session.begin():
        store_id = _current_store_id(session)
        link = session.scalar(
            select(LogisticsShipmentLink).where(
                LogisticsShipmentLink.id == link_id,
                LogisticsShipmentLink.store_id == store_id,
            )
        )
        if link is None or not link.active:
            raise LogisticsLinkError("要撤销的物流关联不存在或已经撤销")
        link.active = False
        link.revoked_by_user_id = actor_user_id
        link.revoked_by_username = actor_username
        link.revoked_at = now
        link.revoke_note = normalized_note
        session.add(
            LogisticsShipmentLinkAudit(
                link_id=link.id,
                action="revoked",
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                note=normalized_note,
                evidence=None,
                created_at=now,
            )
        )
        session.flush()
        return _link_payload(link)


def _w8_profile(row: Mapping[str, Any]) -> dict[str, Any] | None:
    order_no = str(row.get("orderNo") or "").strip()
    created_at = str(row.get("createDateStr") or "").strip()
    created_date = _calendar_date(created_at)
    items = _mapping_list(row.get("items"))
    signature = _sku_quantity_signature(items, sku_field="sku", quantity_field="forecastNum")
    if not order_no or created_date is None or signature is None:
        return None
    return {
        "order_no": order_no,
        "headway_no": str(row.get("headwayNo") or "").strip(),
        "shipping_mark": str(row.get("shippingMark") or "").strip(),
        "status": str(row.get("statusName") or "未标记").strip(),
        "created_at": created_at,
        "created_date": created_date,
        "signature": signature,
    }


def _takealot_profile(
    row: Mapping[str, Any],
    offer_skus: Mapping[str, str],
) -> dict[str, Any] | None:
    shipment_id = _integer(row.get("shipment_id"))
    created_at = str(row.get("created_at") or "").strip()
    created_date = _calendar_date(created_at)
    items = _mapping_list(row.get("shipment_items"))
    counts: Counter[str] = Counter()
    for item in items:
        sku = offer_skus.get(_identity(item.get("offer_id")), "")
        quantity = _integer(item.get("quantity_sending"))
        if not sku or quantity is None:
            return None
        counts[sku] += quantity
    if shipment_id is None or created_date is None or not counts:
        return None
    return {
        "shipment_id": shipment_id,
        "purchase_order_number": str(row.get("purchase_order_number") or "").strip(),
        "reference": str(row.get("reference") or "").strip(),
        "state": str(row.get("purchase_order_state") or "").strip(),
        "created_at": created_at,
        "created_date": created_date,
        "signature": tuple(sorted(counts.items())),
    }


def _sku_quantity_signature(
    items: Sequence[Mapping[str, Any]],
    *,
    sku_field: str,
    quantity_field: str,
) -> tuple[tuple[str, int], ...] | None:
    counts: Counter[str] = Counter()
    for item in items:
        sku = _identity(item.get(sku_field))
        quantity = _integer(item.get(quantity_field))
        if not sku or quantity is None:
            return None
        counts[sku] += quantity
    return tuple(sorted(counts.items())) if counts else None


def _current_store_id(session: Session) -> int:
    store_id = session.scalar(
        select(ErpStore.id)
        .where(
            ErpStore.code == current_store_code(),
            ErpStore.active.is_(True),
            ErpStore.data_connected.is_(True),
        )
        .limit(1)
    )
    if store_id is None:
        raise LogisticsLinkError("所选已接入店铺不存在，无法保存物流关联")
    return int(store_id)


def _link_payload(link: LogisticsShipmentLink) -> dict[str, Any]:
    evidence = link.evidence if isinstance(link.evidence, Mapping) else {}
    return {
        "id": link.id,
        "w8_order_no": link.w8_order_no,
        "takealot_shipment_id": link.takealot_shipment_id,
        "takealot_purchase_order_number": str(
            evidence.get("takealot_purchase_order_number") or ""
        ),
        "takealot_reference": str(evidence.get("takealot_reference") or ""),
        "confidence": str(evidence.get("confidence") or "high"),
        "sku_lines": _integer(evidence.get("sku_lines")) or 0,
        "quantity": _integer(evidence.get("quantity")) or 0,
        "w8_quantity": _integer(evidence.get("w8_quantity"))
        or _integer(evidence.get("quantity"))
        or 0,
        "takealot_quantity": _integer(evidence.get("takealot_quantity"))
        or _integer(evidence.get("quantity"))
        or 0,
        "quantity_delta": _integer(evidence.get("quantity_delta")) or 0,
        "date_gap_days": _integer(evidence.get("date_gap_days")),
        "confirmed_by": link.confirmed_by_username,
        "confirmed_at": link.confirmed_at.isoformat(),
        "active": link.active,
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _identity(value: Any) -> str:
    return str(value or "").strip().casefold()


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _calendar_date(value: str) -> date | None:
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return datetime.strptime(normalized[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
