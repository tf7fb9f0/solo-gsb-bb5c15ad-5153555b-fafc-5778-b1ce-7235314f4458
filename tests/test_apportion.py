from decimal import Decimal

from app.config import VOLUME_QUANTUM
from app.service import apportion


def test_apportion_simple_equal_split():
    shares = {"A": Decimal("10"), "B": Decimal("10")}
    out = apportion(Decimal("5"), shares)
    assert out == {"A": Decimal("2.5"), "B": Decimal("2.5")}
    assert sum(out.values(), Decimal(0)) == Decimal("5")


def test_apportion_total_exactly_conserved_under_indivisible_quantum():
    # 1/3 三份，取 1.000000001（不能被 3 整除）
    shares = {"A": Decimal("1"), "B": Decimal("1"), "C": Decimal("1")}
    out = apportion(Decimal("1.000000001"), shares)
    assert sum(out.values(), Decimal(0)) == Decimal("1.000000001")
    assert all(Decimal("0") <= v <= Decimal("1") for v in out.values())
    # 两个 333333334、一个 333333333
    assert sorted(out.values()) == [
        Decimal("0.333333333"),
        Decimal("0.333333334"),
        Decimal("0.333333334"),
    ]


def test_apportion_respects_proportions():
    shares = {"A": Decimal("7"), "B": Decimal("3")}
    out = apportion(Decimal("10"), shares)
    assert out == {"A": Decimal("7"), "B": Decimal("3")}


def test_apportion_cannot_take_more_than_share():
    shares = {"A": Decimal("0.000000001"), "B": Decimal("10")}
    out = apportion(Decimal("10"), shares)
    assert sum(out.values(), Decimal(0)) == Decimal("10")
    assert out["A"] <= shares["A"]
    assert out["B"] <= shares["B"]


def test_apportion_tiny_transfer_rounds_to_one_quantum():
    shares = {"A": Decimal("0.000000001"), "B": Decimal("1000")}
    out = apportion(VOLUME_QUANTUM, shares)
    assert sum(out.values(), Decimal(0)) == VOLUME_QUANTUM
