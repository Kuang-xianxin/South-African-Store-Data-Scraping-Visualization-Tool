"""Cross-store reads for the shared own-store follower radar."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
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
) -> list[StoreOfferBaseline | StoreOfferObservation]:
    """Load every Seller API refresh point, retaining legacy baselines as fallback."""
    normalized_plids = (
        {str(plid).strip() for plid in plids if str(plid).strip()}
        if plids is not None
        else None
    )
    if normalized_plids is not None and not normalized_plids:
        return []
    result: list[StoreOfferBaseline | StoreOfferObservation] = []
    for store_code, _ in _connected_store_catalog(session, store_codes=store_codes):
        with store_scope(store_code):
            observation_statement = select(StoreOfferObservation)
            observation_exists = (
                select(StoreOfferObservation.id)
                .where(
                    StoreOfferObservation.store_code == StoreOfferBaseline.store_code,
                    StoreOfferObservation.offer_id == StoreOfferBaseline.offer_id,
                    StoreOfferObservation.captured_at == StoreOfferBaseline.captured_at,
                )
                .exists()
            )
            baseline_statement = select(StoreOfferBaseline).where(~observation_exists)
            if normalized_plids is not None:
                observation_statement = observation_statement.where(
                    StoreOfferObservation.productline_id.in_(normalized_plids)
                )
                baseline_statement = baseline_statement.where(
                    StoreOfferBaseline.productline_id.in_(normalized_plids)
                )
            observations = list(
                session.scalars(
                    observation_statement.order_by(
                        StoreOfferObservation.captured_at.desc(),
                        StoreOfferObservation.offer_id.asc(),
                    )
                )
            )
            baselines = session.scalars(
                baseline_statement.order_by(
                    StoreOfferBaseline.captured_at.desc(),
                    StoreOfferBaseline.offer_id.asc(),
                )
            )
            result.extend(observations)
            result.extend(baselines)
    return result


def connected_store_plids(session: Session) -> set[str]:
    """Return the global deduplicated PLID set for all connected stores."""
    return {
        str(item.offer.productline_id).strip()
        for item in load_connected_store_offers(session)
        if str(item.offer.productline_id or "").strip()
    }


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
