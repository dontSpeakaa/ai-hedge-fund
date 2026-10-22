"""A-share market data: Tencent for prices/quotes, Eastmoney for fundamentals.

Both are the public JSON endpoints the consumer sites use; neither needs a
key. The A-share data layer differs from Financial Datasets in ways the
mapping here makes explicit:

- Tickers are dotted (`600519.SH`), because a bare six-digit code does not
  say which exchange it belongs to. `normalize_cn_ticker` accepts shorthands.
- Volume from Tencent is in 手 (lots of 100 shares); `Price.volume` is shares.
- Disclosures are cumulative year-to-date (一季报/中报/三季报/年报), not TTM.
  `get_financial_metrics` therefore computes TTM figures where the history
  allows it — annual + current YTD - prior-year YTD — and marks the row
  `period="ytd"` when it cannot.
- There are no SEC filing types and no analyst consensus, so earnings rows
  carry their announcement date with `source_type="公告"` and no
  BEAT/MISS surprise. The PEAD model consequently abstains on A-shares.
- Insider-trade filings have no equivalent feed wired here, so that method
  returns empty; the engine does not consume it.

Failure contract mirrors `FDClient`: 404 is "no such data" and returns
empty/None; network, auth, rate-limit and malformed-payload failures raise
`CNClientError`. Currency is CNY.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from hedge_fund.market import SHANGHAI, normalize_cn_ticker

logger = logging.getLogger(__name__)

_EMPTY_QUOTE = "v_pv_none_match"
_TAG = re.compile(r"<[^>]+>")


class CNClientError(Exception):
    """An A-share data request failed for infrastructure reasons.

    Distinct from "no data exists" — that returns empty. A backtest must
    crash on this, never treat it as no-data.
    """

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class CNClient:
    """A-share DataClient backed by Tencent quotes and Eastmoney fundamentals.

    Usage::

        with CNClient() as cn:
            prices = cn.get_prices("600519.SH", "2024-01-01", "2024-12-31")
    """

    QUOTE_URL = "https://qt.gtimg.cn/q={symbol}"
    KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    }
    _RETRY_DELAYS = (2, 5, 10)
    _PERIODS = {"day": ("day", "qfqday"), "week": ("week", "qfqweek"), "month": ("month", "qfqmonth")}

    def __init__(self, timeout: float = 30.0, session: requests.Session | None = None) -> None:
        self._timeout = timeout
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()

    def __enter__(self) -> CNClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
    ) -> list[Price]:
        """Daily/weekly/monthly bars, adjusted for splits and dividends (前复权).

        Tencent's minute feed needs a different endpoint and the A-share
        session has a lunch break; intraday funds are rejected at the mandate
        level, so asking for minute bars here fails loudly.
        """
        if interval == "minute":
            raise ValueError("A-share minute bars are not supported; use a daily mandate")
        if interval_multiplier != 1:
            raise ValueError(f"interval_multiplier {interval_multiplier} is not supported for A-shares")
        period_key = self._PERIODS.get(interval)
        if period_key is None:
            raise ValueError(f"unsupported A-share interval {interval!r}; use day, week or month")
        period, response_key = period_key
        start = _iso(start_date)
        end = _iso(end_date)
        if start > end:
            return []
        code = _tencent_code(ticker)
        # Tencent caps one response at ~640 bars and returns the *latest*
        # bars inside the requested range when the range is longer. Page
        # backwards until the requested start is covered, otherwise a
        # multi-year backtest would silently begin years late.
        bars: dict[str, Price] = {}
        cursor = end
        for _ in range(20):
            body = self._json(self.KLINE_URL, {
                "param": f"{code},{period},{start},{cursor},640,qfq",
            })
            node = ((body or {}).get("data") or {}).get(code) or {}
            rows = node.get(response_key) or node.get(period) or []
            page = []
            for row in rows:
                if len(row) < 6:
                    continue
                day = str(row[0])[:10]
                if not (start <= day <= cursor):
                    continue
                open_, close, high, low, volume = (float(v) for v in row[1:6])
                page.append(Price(
                    open=open_, close=close, high=high, low=low,
                    volume=int(round(volume * 100)),  # 手 -> shares
                    time=f"{day}T00:00:00",
                ))
            if not page:
                break
            for bar in page:
                bars.setdefault(bar.time[:10], bar)
            oldest = min(bar.time[:10] for bar in page)
            if oldest <= start:
                break
            cursor = (date.fromisoformat(oldest) - timedelta(days=1)).isoformat()
        return sorted(bars.values(), key=lambda bar: bar.time)

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        """The latest quote, or None for an unknown or unquoted symbol."""
        text = self._request(self.QUOTE_URL.format(symbol=_tencent_code(ticker)))
        if text is None or _EMPTY_QUOTE in text:
            return None
        body = text.split('="', 1)
        if len(body) < 2:
            return None
        fields = body[1].rsplit('"', 1)[0].split("~")
        if len(fields) < 33:
            return None
        try:
            price = float(fields[3])
        except ValueError:
            return None
        if price <= 0:
            return None
        return PriceSnapshot(
            ticker=normalize_cn_ticker(ticker),
            price=price,
            time=_quote_time(fields[30]),
            day_change=_float(fields[31]),
            day_change_percent=_float(fields[32]),
        )

    # ------------------------------------------------------------------
    # Financial metrics
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Metrics for filings publicly announced by *end_date*, newest first.

        Point-in-time on NOTICE_DATE (the disclosure date), never REPORT_DATE
        (the fiscal period end, which is public weeks later). A-share reports
        are cumulative YTD; TTM figures are derived here, and rows without
        enough history to derive them carry `period="ytd"`.
        """
        cutoff = _iso(end_date)
        history = [
            row for row in self._main_indicators(ticker, limit + 8)
            if _row_notice(row) and _row_notice(row) <= cutoff
        ]
        rows = history[:limit]
        if not rows:
            return []
        # The index keeps the rows beyond *limit*: a TTM figure needs last
        # year's annual report and the same period a year ago.
        index = {(_row_year(row), _row_type(row)): row for row in history}
        closes = self._filing_closes(ticker, rows)
        return [self._metric(ticker, row, index, closes) for row in rows]

    def _main_indicators(self, ticker: str, limit: int) -> list[dict]:
        payload = self._json(self.DATACENTER_URL, {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": f'(SECUCODE="{normalize_cn_ticker(ticker)}")',
            "pageNumber": 1,
            "pageSize": max(1, min(50, limit)),
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        })
        result = (payload or {}).get("result") or {}
        return list(result.get("data") or [])

    def _metric(self, ticker: str, row: dict, index: dict, closes: dict[str, float | None]) -> FinancialMetrics:
        total_share = _float(row.get("TOTAL_SHARE"))
        report_period = _iso(row.get("REPORT_DATE"))
        filing_date = _row_notice(row)
        close = closes.get(filing_date)
        market_cap = close * total_share if close and total_share else None

        revenue = _ttm(row, "TOTALOPERATEREVE", index)
        profit = _ttm(row, "PARENTNETPROFIT", index)
        gross_profit = _ttm(row, "MLR", index)
        operating_profit = _ttm(row, "OPERATE_PROFIT_PK", index)
        operating_revenue = _ttm(row, "OPERATE_INCOME_PK", index)
        eps = _ttm(row, "EPSJB", index)
        bps = _float(row.get("BPS"))

        pe = market_cap / profit if market_cap and profit and profit > 0 else None
        roe = profit / (bps * total_share) if profit and bps and total_share and bps > 0 else None
        return FinancialMetrics(
            ticker=normalize_cn_ticker(ticker),
            report_period=report_period,
            period="ttm" if revenue is not None else "ytd",
            currency=row.get("CURRENCY") or "CNY",
            filing_date=filing_date,
            market_cap=market_cap,
            price_to_earnings_ratio=pe,
            return_on_equity=roe,
            gross_margin=_ratio(gross_profit, revenue),
            operating_margin=_ratio(operating_profit, operating_revenue),
            net_margin=_ratio(profit, revenue),
            debt_to_equity=_debt_to_equity(row),
            current_ratio=_float(row.get("LD")),
            revenue_growth=_growth(row, "TOTALOPERATEREVE", index, fallback_pct=row.get("TOTALOPERATEREVETZ")),
            earnings_growth=_growth(row, "PARENTNETPROFIT", index, fallback_pct=row.get("PARENTNETPROFITTZ")),
            earnings_per_share=eps if eps is not None else _float(row.get("EPSJB")),
            book_value_per_share=bps,
        )

    def _filing_closes(self, ticker: str, rows: list[dict]) -> dict[str, float | None]:
        """Each row's closing price on its filing date (or the prior session)."""
        notices = sorted({_row_notice(row) for row in rows if _row_notice(row)})
        if not notices:
            return {}
        start = (date.fromisoformat(notices[0]) - timedelta(days=10)).isoformat()
        end = notices[-1]
        bars = self.get_prices(ticker, start, end)
        by_day = {bar.time[:10]: bar.close for bar in bars}
        result: dict[str, float | None] = {}
        for notice in notices:
            earlier = [day for day in by_day if day <= notice]
            result[notice] = by_day[max(earlier)] if earlier else None
        return result

    # ------------------------------------------------------------------
    # Company facts
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        payload = self._json(self.DATACENTER_URL, {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "ALL",
            "filter": f'(SECUCODE="{normalize_cn_ticker(ticker)}")',
            "pageNumber": 1,
            "pageSize": 1,
            "source": "HSF10",
            "client": "PC",
        })
        rows = (((payload or {}).get("result") or {}).get("data") or [])
        if not rows:
            return None
        row = rows[0]
        industry = row.get("EM2016") or None
        sector = industry.split("-")[0] if industry else None
        return CompanyFacts(
            ticker=normalize_cn_ticker(ticker),
            is_active=True,
            name=row.get("ORG_NAME") or row.get("SECURITY_NAME_ABBR"),
            sector=sector,
            industry=industry,
            category=row.get("SECURITY_TYPE"),
            exchange=row.get("TRADE_MARKET"),
            location=row.get("PROVINCE"),
        )

    # ------------------------------------------------------------------
    # Earnings
    # ------------------------------------------------------------------

    def get_earnings(self, ticker: str) -> Earnings | None:
        rows = self._main_indicators(ticker, 1)
        if not rows:
            return None
        row = rows[0]
        data = _earnings_data(row)
        annual = _row_type(row) == "年报"
        return Earnings(
            ticker=normalize_cn_ticker(ticker),
            report_period=_iso(row.get("REPORT_DATE")),
            fiscal_period=_row_type(row),
            currency=row.get("CURRENCY") or "CNY",
            quarterly=None if annual else data,
            annual=data if annual else None,
        )

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        """Disclosures as a flat list.

        `source_type` is 公告 (announcement) and there is no consensus
        estimate, so `eps_surprise` stays None. PEAD, which keys on 8-K
        surprises, therefore finds no events on A-shares.
        """
        rows = self._main_indicators(ticker, limit + 4)[:limit]
        records = []
        for row in rows:
            data = _earnings_data(row)
            annual = _row_type(row) == "年报"
            records.append(EarningsRecord(
                ticker=normalize_cn_ticker(ticker),
                report_period=_iso(row.get("REPORT_DATE")),
                source_type="公告",
                filing_date=_row_notice(row),
                fiscal_period=_row_type(row),
                currency=row.get("CURRENCY") or "CNY",
                quarterly=None if annual else data,
                annual=data if annual else None,
            ))
        return records

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        symbol = normalize_cn_ticker(ticker)
        query = {
            "uid": "",
            "keyword": symbol.split(".")[0],
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "default", "pageIndex": 1,
                "pageSize": max(1, min(100, limit)), "preTag": "<em>", "postTag": "</em>",
            }},
        }
        text = self._request(self.SEARCH_URL, {"cb": "jQuery", "param": json.dumps(query, ensure_ascii=False)})
        if text is None:
            return []
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end <= start:
            raise CNClientError("eastmoney news: response was not JSONP", path=self.SEARCH_URL)
        try:
            payload = json.loads(text[start + 1:end])
        except json.JSONDecodeError as exc:
            raise CNClientError(f"eastmoney news: invalid JSON: {exc}", path=self.SEARCH_URL) from exc
        articles = ((payload.get("result") or {}).get("cmsArticleWebOld") or [])
        earliest = _iso(start_date) if start_date else None
        latest = _iso(end_date)
        news = []
        for article in articles:
            when = str(article.get("date") or "")
            day = when[:10]
            if not day or day > latest or (earliest and day < earliest):
                continue
            news.append(CompanyNews(
                ticker=symbol,
                title=_clean(article.get("title")),
                source=article.get("mediaName") or "东方财富",
                date=when.replace(" ", "T"),
                url=article.get("url"),
            ))
        return news[:limit]

    # ------------------------------------------------------------------
    # Not available from the public A-share feeds we use
    # ------------------------------------------------------------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        """Empty: no insider-filing feed is wired. The engine does not read it."""
        return []

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        metrics = self.get_financial_metrics(ticker, end_date, limit=1)
        return metrics[0].market_cap if metrics else None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _json(self, url: str, params: dict | None = None) -> dict | None:
        text = self._request(url, params)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CNClientError(f"invalid JSON from {url}: {exc}", path=url) from exc

    def _request(self, url: str, params: dict | None = None, encoding: str = "utf-8") -> str | None:
        """GET with retry on rate limits and 5xx; 404 means "no such data".

        Raises on everything else so a broken feed can never look like a
        company that merely has no news.
        """
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None)):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout, headers=self.HEADERS)
            except requests.RequestException as exc:
                raise CNClientError(f"GET {url} failed: {exc}", path=url) from exc

            if resp.status_code == 404:
                return None
            if (resp.status_code == 429 or resp.status_code >= 500) and delay is not None:
                logger.info("A-share feed returned %s; retrying in %ss", resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise CNClientError(
                    f"GET {url} returned {resp.status_code}", status_code=resp.status_code, path=url,
                )
            return resp.content.decode(encoding, "replace")

        raise CNClientError(f"GET {url} kept failing after {len(self._RETRY_DELAYS)} retries", path=url)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _tencent_code(ticker: str) -> str:
    code, exchange = normalize_cn_ticker(ticker).split(".")
    return f"{exchange.lower()}{code}"


def _iso(value: Any) -> str:
    return str(value)[:10]


def _float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _debt_to_equity(row: dict) -> float | None:
    """CQBL is already total liabilities / equity (0.18 = 18%); when it is
    missing, derive the ratio from ZCFZL, which is debt/assets in percent."""
    ratio = _float(row.get("CQBL"))
    if ratio is not None:
        return ratio
    debt_assets = _float(row.get("ZCFZL"))
    if debt_assets is None or debt_assets >= 100:
        return None
    return debt_assets / (100 - debt_assets)


def _row_year(row: dict) -> str:
    return str(row.get("REPORT_YEAR") or _iso(row.get("REPORT_DATE"))[:4])


def _row_type(row: dict) -> str:
    return str(row.get("REPORT_TYPE") or "")


def _row_notice(row: dict) -> str | None:
    value = row.get("NOTICE_DATE")
    return _iso(value) if value else None


def _ttm(row: dict, field: str, index: dict) -> float | None:
    """Trailing-twelve-month value from cumulative YTD disclosures.

    Annual report: the year itself. Otherwise the prior annual report plus
    the current year-to-date minus the same period a year ago — the standard
    construction. None when any leg is missing, so the caller can fall back
    to the reported (YTD) figure rather than invent one.
    """
    if _row_type(row) == "年报":
        return _float(row.get(field))
    year = int(_row_year(row))
    annual = index.get((str(year - 1), "年报"))
    prior = index.get((str(year - 1), _row_type(row)))
    if annual is None or prior is None:
        return None
    annual_value = _float(annual.get(field))
    prior_value = _float(prior.get(field))
    current_value = _float(row.get(field))
    if annual_value is None or prior_value is None or current_value is None:
        return None
    return annual_value + current_value - prior_value


def _growth(row: dict, field: str, index: dict, *, fallback_pct: Any) -> float | None:
    """TTM year-over-year growth, else the reported YTD growth rate."""
    year = int(_row_year(row))
    prior = index.get((str(year - 1), _row_type(row)))
    current_ttm = _ttm(row, field, index)
    prior_ttm = _ttm(prior, field, index) if prior else None
    if current_ttm is not None and prior_ttm is not None and prior_ttm > 0:
        return current_ttm / prior_ttm - 1
    reported = _float(fallback_pct)
    return reported / 100 if reported is not None else None


def _earnings_data(row: dict) -> EarningsData:
    total_share = _float(row.get("TOTAL_SHARE"))
    bps = _float(row.get("BPS"))
    return EarningsData(
        revenue=_float(row.get("TOTALOPERATEREVE")),
        earnings_per_share=_float(row.get("EPSJB")),
        net_income=_float(row.get("PARENTNETPROFIT")),
        gross_profit=_float(row.get("MLR")),
        operating_income=_float(row.get("OPERATE_PROFIT_PK")),
        weighted_average_shares=total_share,
        shareholders_equity=bps * total_share if bps and total_share else None,
        total_assets=_float(row.get("TOTAL_ASSETS_PK")),
        net_cash_flow_from_operations=_float(row.get("NETCASH_OPERATE_PK")),
    )


def _quote_time(stamp: str) -> str:
    """`20261022140848` -> an aware Shanghai ISO timestamp."""
    try:
        moment = datetime.strptime(stamp, "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return stamp
    return moment.replace(tzinfo=SHANGHAI).isoformat()


def _clean(text: Any) -> str:
    return _TAG.sub("", str(text or "")).strip()

