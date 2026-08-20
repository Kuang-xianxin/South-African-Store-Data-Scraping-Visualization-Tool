"""Cross-store reads for the shared own-store follower radar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import exists, func, literal, select, union_all
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    ErpStore,
    OfferCurrent,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import current_store_code, store_scope


@dataclass(frozen=True)
class ConnectedStoreOffer:
    """One current seller offer with its real store identity."""

    store_code: str
    store_name: str
    offer: OfferCurrent


@dataclass(frozen=True, slots=True)
class ConnectedStoreOfferPoint:
    """Lightweight cross-store Seller API point used by read projections."""

    id: int
    store_code: str
    display_date: date
    offer_id: str
    productline_id: str | None
    sku: str | None
    title: str | None
    image_url: str | None
    selling_price: Decimal | None
    status: str | None
    total_stock: int | None
    takealot_available_stock: int | None
    seller_available_stock: int | None
    captured_at: datetime
    source_kind: str


@dataclass(frozen=True)
class OwnStoreOfferIdentity:
    """Exact Seller API identities that must never be labelled as followers."""

    offer_ids: frozenset[str]
    skus: frozenset[str]


def load_connected_store_offers(
    session: Session,
    *,
    plids: set[str] | None = None,
) -> list[ConnectedStoreOffer]:
    """Load current offers from every active connected store, preserving membership."""
    normalized_plids = (
        {str(plid).strip() for plid in plids if str(plid).strip()}
        if plids is not None
        else None
    )
    if normalized_plids is not None and not normalized_plids:
        return []
    result: list[ConnectedStoreOffer] = []
    for store_code, store_name in _connected_store_catalog(session):
        with store_scope(store_code):
            statement = select(OfferCurrent)
            if normalized_plids is not None:
                statement = statement.where(
                    OfferCurrent.productline_id.in_(normalized_plids)
                )
            offers = list(session.scalars(statement))
        result.extend(
            ConnectedStoreOffer(
                store_code=store_code,
                store_name=store_name,
                offer=offer,
            )
            for offer in offers
        )
    return result


def load_connected_store_baselines(session: Session) -> list[StoreOfferBaseline]:
    """Load Seller API baselines across all stores for the shared radar."""
    result: list[StoreOfferBaseline] = []
    for store_code, _ in _connected_store_catalog(session):
        with store_scope(store_code):
            result.extend(
                session.scalars(
                    select(StoreOfferBaseline).order_by(
                        StoreOfferBaseline.display_date.desc(),
                        StoreOfferBaseline.captured_at.asc(),
                        StoreOfferBaseline.offer_id.asc(),
                    )
                )
            )
    return result


def load_connected_store_offer_points(
    session: Session,
    *,
    plids: set[str] | None = None,
    store_codes: set[str] | None = None,
) -> list[ConnectedStoreOfferPoint]:
    """Load every Seller API refresh point, retaining legacy baselines as fallback."""
    normalized_plids = (
        {str(plid).strip() for plid in plids if str(plid).strip()}
        if plids is not None
        else None
    )
    if normalized_plids is not None and not normalized_plids:
        return []
    selected_store_codes = tuple(
        code
        for code, _ in _connected_store_catalog(session, store_codes=store_codes)
    )
    if not selected_store_codes:
        return []

    observation = StoreOfferObservation.__table__
    baseline = StoreOfferBaseline.__table__
    common_columns = (
        "id",
        "store_code",
        "display_date",
        "offer_id",
        "productline_id",
        "sku",
        "title",
        "image_url",
        "selling_price",
        "status",
        "total_stock",
        "takealot_available_stock",
        "seller_available_stock",
        "captured_at",
    )
    observation_statement = select(
        *(observation.c[name] for name in common_columns),
        literal(0).label("source_rank"),
    ).where(observation.c.store_code.in_(selected_store_codes))
    observation_exists = exists(
        select(observation.c.id).where(
            observation.c.store_code == baseline.c.store_code,
            observation.c.offer_id == baseline.c.offer_id,
            observation.c.captured_at == baseline.c.captured_at,
        )
    )
    baseline_statement = select(
        *(baseline.c[name] for name in common_columns),
        literal(1).label("source_rank"),
    ).where(
        baseline.c.store_code.in_(selected_store_codes),
        ~observation_exists,
    )
    if normalized_plids is not None:
        observation_statement = observation_statement.where(
            observation.c.productline_id.in_(normalized_plids)
        )
        baseline_statement = baseline_statement.where(
            baseline.c.productline_id.in_(normalized_plids)
        )

    combined = union_all(observation_statement, baseline_statement).subquery()
    rows = session.connection().execute(
        select(
            *(combined.c[name] for name in common_columns),
            combined.c.source_rank,
        ).order_by(
            combined.c.store_code.asc(),
            combined.c.source_rank.asc(),
            combined.c.captured_at.desc(),
            combined.c.offer_id.asc(),
        )
    ).mappings()
    return [
        ConnectedStoreOfferPoint(
            id=row["id"],
            store_code=row["store_code"],
            display_date=row["display_date"],
            offer_id=row["offer_id"],
            productline_id=row["productline_id"],
            sku=row["sku"],
            title=row["title"],
            image_url=row["image_url"],
            selling_price=row["selling_price"],
            status=row["status"],
            total_stock=row["total_stock"],
            takealot_available_stock=row["takealot_available_stock"],
            seller_available_stock=row["seller_available_stock"],
            captured_at=row["captured_at"],
            source_kind=(
                "observation" if int(row["source_rank"]) == 0 else "baseline"
            ),
        )
        for row in rows
    ]


def connected_store_plids(session: Session) -> set[str]:
    """Return the global deduplicated PLID set for all connected stores."""
    store_codes = tuple(code for code, _ in _connected_store_catalog(session))
    if not store_codes:
        return set()
    offer_current = OfferCurrent.__table__
    statement = (
        select(offer_current.c.productline_id)
        .where(
            offer_current.c.store_code.in_(store_codes),
            offer_current.c.productline_id.is_not(None),
        )
        .distinct()
    )
    return {
        str(plid).strip()
        for plid in session.connection().execute(statement).scalars()
        if str(plid or "").strip()
    }


def is_connected_store_plid(session: Session, plid: str) -> bool:
    """Return exact current own-store membership without loading every Offer row."""
    normalized_plid = str(plid or "").strip()
    if not normalized_plid:
        return False
    store_codes = tuple(code for code, _ in _connected_store_catalog(session))
    if not store_codes:
        return False
    offer_current = OfferCurrent.__table__
    statement = (
        select(offer_current.c.offer_id)
        .where(
            offer_current.c.store_code.in_(store_codes),
            func.trim(offer_current.c.productline_id) == normalized_plid,
        )
        .limit(1)
    )
    return session.connection().execute(statement).first() is not None


def own_store_offer_identity(
    session: Session,
    plid: str,
) -> OwnStoreOfferIdentity:
    """Load every connected store's current Offer ID/SKU for one private PLID."""
    normalized_plid = str(plid or "").strip()
    offer_ids: set[str] = set()
    skus: set[str] = set()
    for store_code, _ in _connected_store_catalog(session):
        with store_scope(store_code):
            offers = session.scalars(
                select(OfferCurrent).where(
                    OfferCurrent.productline_id == normalized_plid
                )
            )
            for offer in offers:
                offer_id = _normalized_offer_identity(offer.offer_id)
                sku = _normalized_offer_identity(offer.sku)
                if offer_id:
                    offer_ids.add(offer_id)
                if sku:
                    skus.add(sku)
    return OwnStoreOfferIdentity(
        offer_ids=frozenset(offer_ids),
        skus=frozenset(skus),
    )


def _normalized_offer_identity(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _connected_store_catalog(
    session: Session,
    *,
    store_codes: set[str] | None = None,
) -> list[tuple[str, str]]:
    normalized_store_codes = (
        {str(code).strip() for code in store_codes if str(code).strip()}
        if store_codes is not None
        else None
    )
    if normalized_store_codes is not None and not normalized_store_codes:
        return []
    statement = select(ErpStore).where(
        ErpStore.active.is_(True),
        ErpStore.data_connected.is_(True),
    )
    if normalized_store_codes is not None:
        statement = statement.where(ErpStore.code.in_(normalized_store_codes))
    stores = list(session.scalars(statement.order_by(ErpStore.code)))
    if stores:
        return [(store.code, store.display_name) for store in stores]
    code = current_store_code()
    return (
        [(code, code)]
        if normalized_store_codes is None or code in normalized_store_codes
        else []
    )
