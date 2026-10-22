"""Markets the engine can trade, and the handful of rules that differ.

Everything the engine shares between markets — the pipeline, sizing, ledger
— stays market-agnostic. A `Market` carries only what genuinely differs:
the clock that decides when a session is complete, the currency the capital
is denominated in, the benchmark a mandate measures itself against, the
board lot an order must respect, and how a ticker is spelled.

A-share symbols are kept in the same dotted form the data layer uses
(`600519.SH`, `000001.SZ`, `430047.BJ`) so a universe is unambiguous and
sorts consistently. Shorthand is accepted at the edges and normalized here:

    600519        -> 600519.SH      (exchange inferred from the code)
    sh600519      -> 600519.SH
    000001.SZ     -> 000001.SZ
    000300.SH     -> 000300.SH      (an index; a suffix is required)

Only A-share *stocks* are inferred from a bare code. Index codes overlap
with stock codes on the other exchange (000001.SH is the Shanghai Composite,
000001.SZ is Ping An Bank), so an index always needs its suffix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
SHANGHAI = ZoneInfo("Asia/Shanghai")

_US_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_CN_CODE = re.compile(r"^\d{6}$")
_CN_DOTTED = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_CN_PREFIXED = re.compile(r"^(SH|SZ|BJ)(\d{6})$")

# Code prefixes of tradeable A-share stocks. B shares (900xxx, 200xxx),
# funds, and bonds are deliberately out: the engine reasons over company
# fundamentals, which those instruments do not have.
_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301", "302")


def _cn_exchange(code: str) -> str | None:
    """The exchange a bare six-digit A-share stock code belongs to."""
    if code.startswith(_SH_PREFIXES):
        return "SH"
    if code.startswith(_SZ_PREFIXES):
        return "SZ"
    if code.startswith("430") or "831" <= code[:3] <= "839" or "870" <= code[:3] <= "873" or code.startswith("920"):
        return "BJ"
    return None


def normalize_cn_ticker(ticker: str) -> str:
    """`600519`, `sh600519`, `600519.sh` -> `600519.SH`.

    An explicit suffix is trusted as written (indices need one); a bare code
    is only accepted when it is a recognizable A-share stock code.
    """
    raw = str(ticker).strip().upper()
    dotted = _CN_DOTTED.match(raw)
    if dotted:
        return f"{dotted.group(1)}.{dotted.group(2)}"
    prefixed = _CN_PREFIXED.match(raw)
    if prefixed:
        return f"{prefixed.group(2)}.{prefixed.group(1)}"
    if _CN_CODE.match(raw):
        exchange = _cn_exchange(raw)
        if exchange is None:
            raise ValueError(
                f"{ticker!r} is not a recognizable A-share stock code; "
                "write it with an exchange suffix (e.g. 000300.SH for an index)"
            )
        return f"{raw}.{exchange}"
    raise ValueError(
        f"{ticker!r} is not an A-share ticker; use a six-digit code "
        "(600519, 000001) or code.suffix (600519.SH, 000300.SH)"
    )


@dataclass(frozen=True)
class Market:
    """One tradeable market and the rules a mandate inherits from it."""

    id: str
    name: str
    timezone: ZoneInfo
    currency: str
    currency_symbol: str
    default_benchmark: str
    default_language: str
    lot_size: int
    ticker_hint: str

    def normalize(self, ticker: str) -> str:
        """The canonical spelling of *ticker* in this market."""
        if self.id == "cn":
            return normalize_cn_ticker(ticker)
        upper = str(ticker).strip().upper()
        if not _US_TICKER.match(upper):
            raise ValueError(f"{ticker!r} is not a US ticker")
        return upper

    def completed_through(self, now: datetime | None = None) -> str:
        """The last date whose session has certainly closed.

        Yesterday in the market's own timezone, the same rule the US engine
        has always used. Holidays need no calendar: the benchmark's bars are
        the trading-day grid, so a holiday simply has no session.
        """
        moment = now.astimezone(self.timezone) if now is not None else datetime.now(self.timezone)
        return (moment.date() - timedelta(days=1)).isoformat()

    def money(self, amount: float) -> str:
        return f"{self.currency_symbol}{amount:,.2f}"

    @property
    def is_a_share(self) -> bool:
        return self.id == "cn"


US = Market(
    id="us",
    name="United States",
    timezone=NEW_YORK,
    currency="USD",
    currency_symbol="$",
    default_benchmark="SPY",
    default_language="en",
    lot_size=1,
    ticker_hint="AAPL,MSFT,NVDA",
)

CN = Market(
    id="cn",
    name="中国A股",
    timezone=SHANGHAI,
    currency="CNY",
    currency_symbol="¥",
    default_benchmark="000300.SH",
    default_language="zh",
    lot_size=100,
    ticker_hint="600519.SH,000001.SZ,300750.SZ",
)

MARKETS: dict[str, Market] = {market.id: market for market in (US, CN)}


def get_market(name: str) -> Market:
    """The market registered under *name*, case-insensitively."""
    key = str(name).strip().lower()
    if key not in MARKETS:
        raise ValueError(f"unknown market {name!r}; available: {', '.join(sorted(MARKETS))}")
    return MARKETS[key]
