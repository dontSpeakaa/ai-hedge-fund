"""Completed daily data and benchmark-observed trading sessions."""

from datetime import date, datetime, timedelta
from math import isfinite

from hedge_fund.data.protocol import DataClient
from hedge_fund.market import NEW_YORK, get_market

__all__ = ["NEW_YORK", "completed_through", "previous_day", "session_closes"]


def completed_through(market: str = "us") -> str:
    """Exclude the current date in *market*'s timezone, even after its close."""
    target = get_market(market)
    # datetime is looked up on this module so tests can pin "now".
    return target.completed_through(datetime.now(target.timezone))


def previous_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def session_closes(data: DataClient, benchmark: str, start: str, end: str, market: str = "us") -> dict[str, float]:
    end = min(end, completed_through(market))
    if start > end:
        return {}
    bars = data.get_prices(benchmark, start, end)
    closes = {bar.time[:10]: bar.close for bar in bars if start <= bar.time[:10] <= end}
    for day, close in closes.items():
        if not isfinite(close) or close <= 0:
            raise ValueError(f"{benchmark}: close on {day} must be finite and positive")
    return dict(sorted(closes.items()))
