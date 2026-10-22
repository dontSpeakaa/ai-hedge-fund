"""v2 data pipeline — data provider protocol, clients, and response models."""

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.cn_client import CNClient, CNClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from hedge_fund.data.protocol import DataClient
from hedge_fund.market import get_market


def raw_client(market: str = "us") -> DataClient:
    """An uncached data provider for a market."""
    return CNClient() if get_market(market).is_a_share else FDClient()


def client_for(market: str = "us", *, live: bool = False) -> CachedDataClient:
    """The disk-cached provider a mandate in *market* should run on."""
    target = get_market(market)
    return CachedDataClient(raw_client(market), timezone=target.timezone, live=live)


__all__ = [
    "CachedDataClient",
    "CNClient",
    "CNClientError",
    "client_for",
    "raw_client",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "Price",
    "PriceSnapshot",
]
