"""BLUE full-roster helpers; no scheduler, database promotion or green writes."""
from __future__ import annotations

from dataclasses import replace
import re

from sqlalchemy import select

from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.storage.models import CompetitorLinkHealth, CompetitorTarget, ErpStore, OfferCurrent


FULL_WORKER_VERSION = "full-v1"
ACTIVE_JOB_STATES = ("pending", "retry", "leased")


def normalized_identity(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def own_contexts(session, *, store_codes: set[str] | None = None) -> dict[str, list[str]]:
    """Read only PLID/Offer/SKU, across enabled connected stores in the given scope."""
    stores = ErpStore.__table__
    statement = select(stores.c.code).where(stores.c.active.is_(True), stores.c.data_connected.is_(True))
    if store_codes is not None:
        statement = statement.where(stores.c.code.in_(store_codes))
    codes = list(session.connection().scalars(statement))
    if not codes:
        return {}
    offers = OfferCurrent.__table__
    rows = session.connection().execute(select(
        offers.c.productline_id, offers.c.offer_id, offers.c.sku,
    ).where(offers.c.store_code.in_(codes), offers.c.productline_id.is_not(None)))
    result: dict[str, set[str]] = {}
    for plid, offer_id, sku in rows:
        plid = str(plid or "").strip()
        if re.fullmatch(r"[0-9]{1,30}", plid):
            scopes = result.setdefault(plid, set())
            scopes.update(value for raw in (offer_id, sku) if (value := normalized_identity(raw)))
    return {plid: sorted(scopes) for plid, scopes in result.items()}


def full_targets(session, *, store_codes: set[str]) -> list[dict]:
    """Freeze all true competitors plus authorized own PLIDs, own identity wins."""
    all_own = own_contexts(session)
    allowed_own = own_contexts(session, store_codes=store_codes)
    targets = CompetitorTarget.__table__
    rows = session.connection().execute(select(targets.c.plid, targets.c.url).where(
        targets.c.active.is_(True)).order_by(targets.c.created_at, targets.c.plid))
    result = {}
    for plid, url in rows:
        if plid not in all_own:
            result[plid] = dict(plid=plid, url=url, followers_only=False)
    for plid in sorted(allowed_own):
        result[plid] = dict(plid=plid, url=f"https://www.takealot.com/product/PLID{plid}",
                            followers_only=True)
    return list(result.values())


def valid_own_scopes(spec: dict) -> set[str]:
    values = spec.get("own_offer_scopes")
    if (not isinstance(values, list) or not values or len(values) > 10000
            or any(not isinstance(value, str) or not value or len(value) > 512 for value in values)):
        raise ValueError("Own-store identity unavailable; no follower inventory may be inferred")
    return {normalized_identity(value) for value in values}


def follower_product(product, scopes: set[str]):
    return replace(product, offers=tuple(offer for offer in product.offers
        if normalized_identity(offer.offer_id) not in scopes
        and normalized_identity(offer.sku) not in scopes))


def roster_context(session) -> tuple[dict, set[str], list[tuple[str, str]]]:
    own = own_contexts(session)
    invalid = set(session.scalars(select(CompetitorLinkHealth.plid).where(
        CompetitorLinkHealth.status == "confirmed_invalid")))
    controls = CompetitorRepository(session).recent_control_products(exclude_plid="", limit=4)
    return own, invalid, controls
