"""Market rules — spelling, clocks, currency and board lots."""

from datetime import datetime, timezone

import pytest

from hedge_fund.market import CN, US, get_market, normalize_cn_ticker


def test_bare_a_share_codes_gain_their_exchange():
    assert normalize_cn_ticker("600519") == "600519.SH"
    assert normalize_cn_ticker("688981") == "688981.SH"
    assert normalize_cn_ticker("000001") == "000001.SZ"
    assert normalize_cn_ticker("300750") == "300750.SZ"
    assert normalize_cn_ticker("430047") == "430047.BJ"


def test_a_share_shorthands_and_suffixes_are_accepted():
    assert normalize_cn_ticker("sh600519") == "600519.SH"
    assert normalize_cn_ticker("600519.sh") == "600519.SH"
    assert normalize_cn_ticker("000001.SZ") == "000001.SZ"
    assert normalize_cn_ticker("bj430047") == "430047.BJ"
    # An index keeps the suffix it was given (000001.SH is the SSE Composite).
    assert normalize_cn_ticker("000001.SH") == "000001.SH"


@pytest.mark.parametrize("ticker", ["AAPL", "12345", "900901", "600519.XX", ""])
def test_non_a_share_tickers_are_rejected(ticker):
    with pytest.raises(ValueError):
        normalize_cn_ticker(ticker)


def test_us_tickers_are_uppercased_and_validated():
    assert US.normalize(" aapl ") == "AAPL"
    assert US.normalize("BRK.B") == "BRK.B"
    with pytest.raises(ValueError):
        US.normalize("600519")
    with pytest.raises(ValueError):
        US.normalize("not a ticker")


def test_markets_carry_currency_and_board_lot():
    assert (US.currency, US.currency_symbol, US.lot_size, US.default_benchmark) == ("USD", "$", 1, "SPY")
    assert (CN.currency, CN.currency_symbol, CN.lot_size, CN.default_benchmark) == ("CNY", "¥", 100, "000300.SH")
    assert US.money(1234.5) == "$1,234.50"
    assert CN.money(1234.5) == "¥1,234.50"


def test_get_market_is_case_insensitive_and_rejects_unknown_names():
    assert get_market("CN") is CN
    assert get_market(" us ") is US
    with pytest.raises(ValueError, match="unknown market"):
        get_market("hk")


def test_completed_through_uses_each_market_timezone():
    """00:30 UTC is 08:30 in Shanghai (today has begun) but 20:30 the
    previous evening in New York — the two markets are on different days."""
    now = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
    assert CN.completed_through(now) == "2026-10-21"
    assert US.completed_through(now) == "2026-10-20"
