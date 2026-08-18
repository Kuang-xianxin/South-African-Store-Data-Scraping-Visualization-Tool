"""Durable Takealot collection workflows."""

from takealot_ops.collectors.offers import CollectionResult, collect_offers
from takealot_ops.collectors.returns import collect_returns
from takealot_ops.collectors.sales import collect_sales


__all__ = ["CollectionResult", "collect_offers", "collect_returns", "collect_sales"]
