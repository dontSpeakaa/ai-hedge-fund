"""CNClient tests — recorded A-share responses, no network.

The fixtures under fixtures/cn/ are trimmed copies of real Tencent and
Eastmoney payloads (see the module docstring in cn_client.py). A fake
session routes by URL so the tests exercise the same parsing and mapping
code the live client runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hedge_fund.data.cn_client import CNClient, CNClientError

FIXTURES = Path(__file__).parent / "fixtures" / "cn"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _payload(name: str) -> dict:
    return json.loads(_fixture(name))


@dataclass
class FakeResponse:
    content: bytes = b""
    status_code: int = 200


class FakeSession:
    """Routes GETs by URL; records what was asked for."""

    def __init__(self, router):
        self._router = router
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        params = params or {}
        self.calls.append((url, params))
        result = self._router(url, params)
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result if isinstance(result, bytes) else result.encode())

    def close(self) -> None:
        pass


def _router(metrics: dict | None = None, facts: dict | None = None, news: bytes | None = None):
    metrics = metrics or _payload("eastmoney_metrics.json")
    facts = facts or _payload("eastmoney_orginfo.json")
    news = news or _fixture("eastmoney_news.jsonp")

    def route(url: str, params: dict):
        if "ifzq.gtimg.cn" in url:
            return _kline(params)
        if "datacenter-web.eastmoney.com" in url:
            if params.get("reportName") == "RPT_F10_BASIC_ORGINFO":
                return json.dumps(facts)
            return json.dumps(metrics)
        if "search-api-web.eastmoney.com" in url:
            return news
        if "qt.gtimg.cn" in url:
            return _fixture("tencent_quote_stock.txt")
        return FakeResponse(status_code=404)

    return route


def _kline(params: dict) -> str:
    """A one-bar K-line at the requested start, with a fixed close."""
    code, _period, start, _end, _count, _fq = params["param"].split(",")
    return json.dumps({"data": {code: {"qfqday": [[start, "1500", "1500", "1500", "1500", "1000"]]}}})


def _client(router=None) -> CNClient:
    return CNClient(session=FakeSession(router or _router()))


def test_prices_parse_hloc_and_convert_volume_from_lots():
    client = _client(lambda url, params: _fixture("tencent_kline_day.json"))
    bars = client.get_prices("600519.SH", "2024-09-02", "2024-09-03")
    assert len(bars) == 2
    assert bars[0].time == "2024-09-02T00:00:00"
    assert bars[0].open == pytest.approx(1326.537)
    assert bars[0].close == pytest.approx(1291.537)
    assert bars[0].high == pytest.approx(1334.537)
    assert bars[0].low == pytest.approx(1291.537)
    assert bars[0].volume == 3413500  # 34,135 手 -> shares


def test_prices_filter_to_the_requested_window():
    client = _client(lambda url, params: _fixture("tencent_kline_day.json"))
    bars = client.get_prices("600519.SH", "2024-09-03", "2024-09-03")
    assert [bar.time[:10] for bar in bars] == ["2024-09-03"]


def test_index_uses_the_unadjusted_key():
    client = _client(lambda url, params: _fixture("tencent_kline_index_day.json"))
    bars = client.get_prices("000300.SH", "2026-09-30", "2026-10-31")
    assert bars and bars[0].time[:10] == "2026-09-30"
    assert bars[0].close == pytest.approx(4357.62)


def test_shorthands_are_normalized_before_the_request():
    session = FakeSession(lambda url, params: _fixture("tencent_kline_day.json"))
    client = CNClient(session=session)
    client.get_prices("sh600519", "2024-09-02", "2024-09-02")
    assert "sh600519,day" in session.calls[0][1]["param"]


def test_snapshot_parses_the_tencent_quote():
    client = _client()
    snap = client.get_snapshot("600519.SH")
    assert snap is not None
    assert snap.ticker == "600519.SH"
    assert snap.price == pytest.approx(1310.23)
    assert snap.time.endswith("+08:00")
    assert snap.day_change_percent == pytest.approx(0.04)


def test_snapshot_is_none_for_an_unknown_symbol():
    client = _client(lambda url, params: "v_pv_none_match=1")
    assert client.get_snapshot("600519.SH") is None


def test_metrics_are_point_in_time_and_ttm():
    client = _client()
    metrics = client.get_financial_metrics("600519.SH", "2026-10-22", limit=4)
    assert [m.report_period for m in metrics] == [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    ]
    latest = metrics[0]
    assert latest.filing_date == "2026-08-15"  # NOTICE_DATE, not REPORT_DATE
    assert latest.currency == "CNY"
    assert latest.period == "ttm"
    assert latest.price_to_earnings_ratio and latest.price_to_earnings_ratio > 0
    assert latest.return_on_equity and latest.return_on_equity > 0
    assert latest.gross_margin == pytest.approx(0.886, abs=0.01)
    assert latest.net_margin == pytest.approx(0.47, abs=0.01)
    assert latest.debt_to_equity == pytest.approx(0.179, abs=0.01)
    assert latest.current_ratio == pytest.approx(5.59, abs=0.01)
    # TTM EPS is annual + H1 - prior H1, not the 35.57 YTD figure.
    assert latest.earnings_per_share > 60


def test_metrics_before_a_notice_date_are_not_visible():
    client = _client()
    metrics = client.get_financial_metrics("600519.SH", "2026-04-30", limit=10)
    assert metrics
    assert all(m.filing_date <= "2026-04-30" for m in metrics)
    assert "2026-06-30" not in [m.report_period for m in metrics]


def test_metrics_keeps_ytd_when_history_cannot_build_ttm():
    client = _client()
    metrics = client.get_financial_metrics("600519.SH", "2026-10-22", limit=8)
    oldest = metrics[-1]  # 2024 三季报: no 2023 三季报/年报 in the fixture
    assert oldest.period == "ytd"
    assert oldest.price_to_earnings_ratio is None


def test_company_facts_map_the_eastmoney_orginfo():
    client = _client()
    facts = client.get_company_facts("600519.SH")
    assert facts is not None
    assert facts.name == "贵州茅台酒股份有限公司"
    assert facts.sector == "食品饮料"
    assert facts.exchange == "上海证券交易所"
    assert facts.location == "贵州"


def test_news_strips_markup_and_filters_by_date():
    client = _client()
    news = client.get_news("600519.SH", "2026-10-21", start_date="2026-10-21", limit=3)
    assert len(news) == 1
    assert "<em>" not in news[0].title
    assert news[0].title.startswith("贵州茅台")
    assert news[0].date.startswith("2026-10-21T")
    assert news[0].ticker == "600519.SH"
    # The same article is outside a window that starts later.
    assert client.get_news("600519.SH", "2026-10-22", start_date="2026-10-22") == []


def test_earnings_history_carries_announcement_dates_and_no_surprise():
    client = _client()
    records = client.get_earnings_history("600519.SH", limit=3)
    assert len(records) == 3
    assert records[0].filing_date == "2026-08-15"
    assert records[0].source_type == "公告"
    assert records[0].quarterly is not None
    assert records[0].quarterly.eps_surprise is None


def test_missing_data_is_empty_not_an_error():
    client = _client(lambda url, params: FakeResponse(status_code=404))
    assert client.get_prices("600519.SH", "2024-01-01", "2024-01-31") == []
    assert client.get_snapshot("600519.SH") is None
    assert client.get_company_facts("600519.SH") is None
    assert client.get_financial_metrics("600519.SH", "2026-10-22") == []
    assert client.get_news("600519.SH", "2026-10-21") == []


def test_forbidden_raises_instead_of_looking_like_no_data():
    client = _client(lambda url, params: FakeResponse(status_code=403))
    with pytest.raises(CNClientError, match="403"):
        client.get_snapshot("600519.SH")


def test_minute_and_multiplier_intervals_fail_loudly():
    client = _client()
    with pytest.raises(ValueError, match="minute"):
        client.get_prices("600519.SH", "2024-01-01", "2024-01-31", interval="minute")
    with pytest.raises(ValueError, match="interval_multiplier"):
        client.get_prices("600519.SH", "2024-01-01", "2024-01-31", interval_multiplier=5)
    with pytest.raises(ValueError, match="unsupported"):
        client.get_prices("600519.SH", "2024-01-01", "2024-01-31", interval="hour")


def test_get_prices_pages_backwards_past_the_response_cap(monkeypatch):
    client = CNClient(session=FakeSession(lambda *_: b""))
    pages = [
        [["2023-01-03", "1", "1", "1", "1", "10"],
         ["2023-01-04", "1", "1", "1", "1", "10"],
         ["2023-01-05", "1", "1", "1", "1", "10"]],
        [["2020-01-02", "1", "1", "1", "1", "10"],
         ["2020-01-03", "1", "1", "1", "1", "10"]],
    ]
    asked: list[str] = []

    def fake_json(url, params=None):
        asked.append(params["param"])
        page = pages[len(asked) - 1] if len(asked) <= len(pages) else pages[-1]
        return {"data": {"sh600519": {"qfqday": page}}}

    monkeypatch.setattr(client, "_json", fake_json)
    bars = client.get_prices("600519.SH", "2020-01-01", "2024-12-31")
    assert [bar.time[:10] for bar in bars] == [
        "2020-01-02", "2020-01-03", "2023-01-03", "2023-01-04", "2023-01-05",
    ]
    assert len(asked) == 3  # the last page is empty once the cursor passes start
