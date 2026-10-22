"""Point-in-time fundamentals snapshot — the shared input for LLM analysts.

A `FundamentalsSnapshot` is everything an investor agent is allowed to know
about a company as of a given date: a history of financial metrics (each row
provably public by `as_of` — the data layer filters on filing_date, not
report_period) plus a few derived aggregates computed here in Python so the
LLM reasons over facts instead of re-deriving arithmetic.

The snapshot is pure data: build it once, hash it, feed it to any persona.
`content_hash` is the cache key for LLM calls — an agent only re-reasons
when a new filing changes its snapshot. Both the hash and `render()` exclude
`as_of`: two dates between filings see identical data, and identical data
must produce an identical prompt (a cache hit), not two paid LLM calls.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from hedge_fund.data.protocol import DataClient
from hedge_fund.features.breakpoints import MEBreakpoints

# An agent can't say anything defensible about a company with less history
# than this (one year of ttm rows).
MIN_PERIODS = 4


class InsufficientData(ValueError):
    """Not enough point-in-time history to build a snapshot."""


class PeriodFundamentals(BaseModel):
    """One reporting period's key metrics, compacted for prompting."""

    report_period: str
    filing_date: str | None = None
    market_cap: float | None = None
    price_to_earnings_ratio: float | None = None
    return_on_equity: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    revenue_growth: float | None = None
    earnings_per_share: float | None = None
    book_value_per_share: float | None = None
    free_cash_flow_per_share: float | None = None


class FundamentalsSnapshot(BaseModel):
    """What an analyst may know about *ticker* as of *as_of*. Newest first."""

    ticker: str
    as_of: str
    sector: str | None = None
    industry: str | None = None
    periods: list[PeriodFundamentals]

    # Derived aggregates (computed in build_snapshot, not by the LLM)
    roe_avg: float | None = None
    net_margin_avg: float | None = None
    gross_margin_trend: float | None = None  # latest minus oldest
    bvps_cagr: float | None = None
    debt_to_equity_latest: float | None = None
    market_cap_latest: float | None = None
    # t-0 EPS versus t-4 (one year of quarter-spaced ttm rows). None when
    # either print is missing or the year-ago EPS is not positive.
    eps_growth_yoy: float | None = None
    # Size as a market-cap percentile of US stocks (steps of 5) as of the t-0
    # filing. Set only when build_snapshot is given a breakpoints table; None
    # means the blind prompt shows no size at all.
    size_percentile: int | None = None

    @property
    def content_hash(self) -> str:
        """Stable hash of the fundamentals content — the LLM cache key.

        Excludes `as_of` so two dates between filings share one hash: an
        unchanged snapshot must be free, not a fresh LLM call per date.
        """
        canonical = self.model_dump_json(exclude={"as_of"})
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def render(self, blind: bool = False, language: str = "en") -> str:
        """Compact text block for the LLM prompt.

        Deliberately date-free (no `as_of`): the prompt cache keys on exact
        prompt text, so the same fundamentals must render identically on any
        date. It also keeps the LLM from anchoring on a calendar date it
        could associate with post-date world events.

        `blind=True` goes one step further, and is what backtests use: the
        ticker, industry, calendar dates, absolute size, and per-share dollar
        values are withheld (the sector stays). Periods are labelled t-0
        (latest), t-1, ..., per-share series are indexed to the oldest
        period shown (= 100), and size appears only as a market-cap
        percentile when a breakpoints table was supplied. Without that, a
        model that remembers how a named company did after a given quarter
        can recall the outcome it is being scored on. Blind mode reduces
        that recall rather than removing it (distinctive ratio profiles can
        still give a large company away), and the personas lose
        company-specific knowledge. Live runs render unblinded.

        `language` translates the labels ("zh"); the numbers and the layout
        are identical, and the English default is byte-for-byte unchanged so
        existing prompt caches keep hitting.
        """
        if blind:
            return self._render_blind(language)
        t = _labels(language)
        lines = [
            t["company"] + f": {self.ticker}"
            + (f"  |  {t['sector']}: {self.sector}" if self.sector else "")
            + (f"  |  {t['industry']}: {self.industry}" if self.industry else ""),
            t["filed_note"],
            "",
            t["summary"],
            f"  {t['market_cap']}: {_fmt(self.market_cap_latest)}",
            f"  {t['roe_avg']}: {_fmt(self.roe_avg)}  |  {t['net_margin_avg']}: {_fmt(self.net_margin_avg)}",
            f"  {t['gross_trend']}: {_fmt(self.gross_margin_trend)}",
            f"  {t['bvps_cagr']}: {_fmt(self.bvps_cagr)}",
            f"  {t['debt_equity']}: {_fmt(self.debt_to_equity_latest)}",
            "",
            t["history"],
            t["header"],
        ]
        for p in self.periods:
            lines.append(
                f"{p.report_period} | {p.filing_date or '?'} | {_fmt(p.market_cap)} "
                f"| {_fmt(p.price_to_earnings_ratio)} | {_fmt(p.return_on_equity)} "
                f"| {_fmt(p.gross_margin)} | {_fmt(p.operating_margin)} "
                f"| {_fmt(p.net_margin)} | {_fmt(p.debt_to_equity)} "
                f"| {_fmt(p.current_ratio)} | {_fmt(p.revenue_growth)} "
                f"| {_fmt(p.earnings_per_share)} | {_fmt(p.book_value_per_share)} "
                f"| {_fmt(p.free_cash_flow_per_share)}"
            )
        return "\n".join(lines)

    def _render_blind(self, language: str = "en") -> str:
        """Backtest prompt: ratios and indexed trends, no identifying dollars."""
        t = _labels(language)
        oldest = self.periods[-1]
        lines = [
            t["company"] + f": ({t['withheld']})"
            + (f"  |  {t['sector']}: {self.sector}" if self.sector else ""),
            t["blind_note"],
            "",
            t["summary"],
        ]
        if self.size_percentile is not None:
            lines.append(f"  {t['size']}: {_size_label(self.size_percentile, language)}")
        lines += [
            f"  {t['roe_avg']}: {_fmt(self.roe_avg)}  |  {t['net_margin_avg']}: {_fmt(self.net_margin_avg)}",
            f"  {t['gross_trend']}: {_fmt(self.gross_margin_trend)}",
            f"  {t['bvps_cagr']}: {_fmt(self.bvps_cagr)}",
            f"  {t['debt_equity']}: {_fmt(self.debt_to_equity_latest)}",
            "",
            t["history"],
            t["blind_header"],
        ]
        for i, p in enumerate(self.periods):
            lines.append(
                f"t-{i} | {_fmt(p.price_to_earnings_ratio)} | {_fmt(p.return_on_equity)} "
                f"| {_fmt(p.gross_margin)} | {_fmt(p.operating_margin)} "
                f"| {_fmt(p.net_margin)} | {_fmt(p.debt_to_equity)} "
                f"| {_fmt(p.current_ratio)} | {_fmt(p.revenue_growth)} "
                f"| {_per_share_index(p.earnings_per_share, oldest.earnings_per_share)} "
                f"| {_per_share_index(p.book_value_per_share, oldest.book_value_per_share)} "
                f"| {_per_share_index(p.free_cash_flow_per_share, oldest.free_cash_flow_per_share)} "
                f"| {_fmt(self.eps_growth_yoy) if i == 0 else '-'}"
            )
        return "\n".join(lines)


def build_snapshot(
    ticker: str,
    as_of: str,
    data_client: DataClient,
    periods: int = 20,
    breakpoints: MEBreakpoints | None = None,
) -> FundamentalsSnapshot:
    """Build the point-in-time snapshot for (ticker, as_of).

    `breakpoints`, when given, places the t-0 filed market cap on the size
    distribution of US stocks as of that filing (blind prompts show the
    percentile instead of dollars). Without it `size_percentile` stays None.

    Raises InsufficientData if fewer than MIN_PERIODS filed periods exist.
    Data-layer failures propagate (fail loud) — a broken snapshot must never
    silently become a neutral view.

    *as_of* may be a bar (YYYY-MM-DDTHH:MM:SS) for a minute fund; filings
    are dated by day, so the data layer is asked by the date alone. Every
    bar of a day then shares one metrics response and one prompt.
    """
    metrics = data_client.get_financial_metrics(
        ticker, as_of[:10], period="ttm", limit=periods,
    )
    if len(metrics) < MIN_PERIODS:
        raise InsufficientData(
            f"{ticker} as of {as_of}: only {len(metrics)} filed periods "
            f"(need {MIN_PERIODS})"
        )

    # Market cap comes from the most recent FILED metrics row. Deliberately
    # NOT data_client.get_market_cap(): that prefers company_facts.market_cap,
    # which is latest-only — lookahead in a backtest.
    facts = data_client.get_company_facts(ticker)

    rows = [
        PeriodFundamentals(**m.model_dump(include=set(PeriodFundamentals.model_fields)))
        for m in metrics
    ]

    size_percentile = None
    if breakpoints is not None and rows[0].filing_date is not None:
        size_percentile = breakpoints.size_percentile(rows[0].market_cap, rows[0].filing_date)

    return FundamentalsSnapshot(
        ticker=ticker,
        as_of=as_of,
        # Sector/industry are slow-moving company attributes; using latest
        # facts here is an accepted, documented PIT approximation.
        sector=facts.sector if facts else None,
        industry=facts.industry if facts else None,
        periods=rows,
        roe_avg=_avg([m.return_on_equity for m in metrics]),
        net_margin_avg=_avg([m.net_margin for m in metrics]),
        gross_margin_trend=_trend([m.gross_margin for m in metrics]),
        bvps_cagr=_cagr([m.book_value_per_share for m in metrics]),
        debt_to_equity_latest=metrics[0].debt_to_equity,
        market_cap_latest=metrics[0].market_cap,
        eps_growth_yoy=_eps_growth_yoy(rows),
        size_percentile=size_percentile,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_LABELS = {
    "en": {
        "company": "Company",
        "sector": "Sector",
        "industry": "Industry",
        "filed_note": "All figures below were publicly filed by their filing dates. "
                      "Treat the most recent filing shown as the present.",
        "summary": "Summary:",
        "market_cap": "Market cap (latest filed)",
        "roe_avg": "ROE avg",
        "net_margin_avg": "Net margin avg",
        "gross_trend": "Gross margin trend (latest-oldest)",
        "bvps_cagr": "Book value/share CAGR",
        "debt_equity": "Debt/equity (latest)",
        "history": "History (trailing-twelve-month periods, newest first):",
        "header": "period | filed | mktcap | P/E | ROE | gross_m | op_m | net_m | D/E "
                  "| curr | rev_gr | EPS | BVPS | FCF/sh",
        "withheld": "withheld",
        "blind_note": "Periods are labelled relative to the latest filing (t-0). "
                      "Calendar dates, absolute size and per-share dollar values are withheld; "
                      "per-share figures are indexed to the oldest period shown (= 100). "
                      "Treat t-0 as the present.",
        "size": "Size",
        "blind_header": "period | P/E | ROE | gross_m | op_m | net_m | D/E "
                        "| curr | rev_gr | eps_idx | bvps_idx | fcf_idx | eps_yoy",
    },
    "zh": {
        "company": "公司",
        "sector": "板块",
        "industry": "行业",
        "filed_note": "下列数据均已在各自披露日公开。请把展示的最新一期财报视为当前时点。",
        "summary": "摘要：",
        "market_cap": "总市值（最新披露）",
        "roe_avg": "平均净资产收益率",
        "net_margin_avg": "平均净利率",
        "gross_trend": "毛利率趋势（最新-最早）",
        "bvps_cagr": "每股净资产复合增长率",
        "debt_equity": "产权比率（最新）",
        "history": "历史（滚动十二个月，最新在前）：",
        "header": "期间 | 披露日 | 总市值 | 市盈率 | ROE | 毛利率 | 营业利润率 | 净利率 | 产权比率 "
                  "| 流动比率 | 营收增速 | 每股收益 | 每股净资产 | 每股自由现金流",
        "withheld": "已隐去",
        "blind_note": "期间以最新披露为 t-0 相对标注。日历日期、绝对规模和每股金额均已隐去；"
                      "每股指标以展示的最早期间为 100 指数化。请把 t-0 视为当前时点。",
        "size": "规模",
        "blind_header": "期间 | 市盈率 | ROE | 毛利率 | 营业利润率 | 净利率 | 产权比率 "
                        "| 流动比率 | 营收增速 | 每股收益指数 | 每股净资产指数 | 自由现金流指数 | 每股收益同比",
    },
}


def _labels(language: str) -> dict[str, str]:
    return _LABELS["zh" if language == "zh" else "en"]


def _size_label(percentile: int, language: str = "en") -> str:
    if language == "zh":
        if percentile <= 0:
            return "市值低于美股第 5 百分位"
        return f"市值处于美股第 {percentile} 百分位或以上"
    if percentile <= 0:
        return "market cap below the 5th percentile of US stocks"
    return f"market cap at or above the {percentile}th percentile of US stocks"


def _per_share_index(value: float | None, base: float | None) -> str:
    """Scale a per-share series to the oldest period (= 100).

    A missing or non-positive base has no scale, so the whole column is
    dashed rather than inventing one. The sign of `value` is kept, so a
    swing through zero still shows up.
    """
    if value is None or base is None or base <= 0:
        return "-"
    return f"{value / base * 100:.1f}"


def _eps_growth_yoy(periods: list[PeriodFundamentals]) -> float | None:
    """t-0 EPS versus t-4. None when either is missing or t-4 EPS is not positive."""
    if len(periods) < 5:
        return None
    latest = periods[0].earnings_per_share
    prior = periods[4].earnings_per_share
    if latest is None or prior is None or prior <= 0:
        return None
    return round(latest / prior - 1, 4)


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:.2f}"


def _avg(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _trend(values: list[float | None]) -> float | None:
    """Latest minus oldest (values arrive newest first)."""
    xs = [v for v in values if v is not None]
    return round(xs[0] - xs[-1], 4) if len(xs) >= 2 else None


def _cagr(values: list[float | None]) -> float | None:
    """Annualized growth from oldest to latest (ttm rows are quarter-spaced)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2 or xs[-1] is None or xs[-1] <= 0 or xs[0] <= 0:
        return None
    years = (len(xs) - 1) / 4  # quarter-spaced ttm periods
    if years <= 0:
        return None
    return round((xs[0] / xs[-1]) ** (1 / years) - 1, 4)
