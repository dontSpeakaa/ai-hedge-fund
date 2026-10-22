"""build_orders tests — pure diffing math."""

import pytest

from hedge_fund.brokers.models import Position
from hedge_fund.pipeline.execution import build_orders


def _positions(**shares):
    return {t: Position(ticker=t, shares=s) for t, s in shares.items()}


def test_floor_sizing_never_overshoots():
    orders = build_orders({"AAPL": 0.25}, {}, {"AAPL": 300.0}, equity=10_000.0)
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].quantity == 8  # 2500 / 300 = 8.33 -> 8


def test_delta_against_existing_position():
    orders = build_orders(
        {"AAPL": 0.25}, _positions(AAPL=5), {"AAPL": 250.0}, equity=10_000.0,
    )
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].quantity == 5  # target 10, held 5


def test_held_name_missing_from_targets_is_closed():
    orders = build_orders({}, _positions(AAPL=7), {"AAPL": 100.0}, equity=10_000.0)
    assert len(orders) == 1
    assert orders[0].side == "sell"
    assert orders[0].quantity == 7


def test_subshare_delta_emits_nothing():
    orders = build_orders({"AAPL": 0.005}, {}, {"AAPL": 100.0}, equity=10_000.0)
    assert orders == []  # 50 dollars / 100 = 0.5 shares -> floor 0


def test_a_share_orders_round_down_to_whole_board_lots():
    # 25% of 100,000 at 33 = 757.57 shares -> 700 (7 lots of 100).
    orders = build_orders({"600519.SH": 0.25}, {}, {"600519.SH": 33.0}, equity=100_000.0, lot_size=100)
    assert [(o.ticker, o.side, o.quantity) for o in orders] == [("600519.SH", "buy", 700)]


def test_sub_lot_target_emits_no_order():
    orders = build_orders({"600519.SH": 0.001}, {}, {"600519.SH": 100.0}, equity=100_000.0, lot_size=100)
    assert orders == []


def test_odd_lots_can_still_be_sold_in_full():
    # A position that is not a multiple of the board lot can be closed.
    orders = build_orders({}, _positions(**{"600519.SH": 150}), {"600519.SH": 10.0}, equity=100_000.0, lot_size=100)
    assert [(o.side, o.quantity) for o in orders] == [("sell", 150)]


def test_invalid_lot_size_is_rejected():
    with pytest.raises(ValueError, match="lot_size"):
        build_orders({"AAPL": 0.1}, {}, {"AAPL": 100.0}, equity=10_000.0, lot_size=0)


def test_sells_before_buys_alphabetical():
    orders = build_orders(
        {"AAPL": 0.2, "MSFT": 0.0, "NVDA": 0.2, "AMZN": 0.0},
        _positions(MSFT=10, NVDA=1, AMZN=5),
        {"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0, "AMZN": 100.0},
        equity=10_000.0,
    )
    assert [(o.ticker, o.side) for o in orders] == [
        ("AMZN", "sell"), ("MSFT", "sell"),
        ("AAPL", "buy"), ("NVDA", "buy"),
    ]


def test_short_target_sells_past_zero():
    orders = build_orders({"AAPL": -0.2}, {}, {"AAPL": 100.0}, equity=10_000.0)
    assert len(orders) == 1
    assert orders[0].side == "sell"
    assert orders[0].quantity == 20
