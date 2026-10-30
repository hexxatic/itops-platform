"""Build dates from serial numbers: an age when no purchase or warranty date is known."""

from __future__ import annotations

import datetime as dt

from itops_assets.dating import built_from_serial, processor_year

TODAY = dt.date(2026, 10, 20)


def test_an_hp_serial_carries_its_build_week() -> None:
    # 4th character: the year's last digit; 5th and 6th: the week (its Monday).
    assert built_from_serial("Hewlett-Packard", "MXL0042ABC", None, TODAY) == dt.date(2020, 1, 20)
    assert built_from_serial("HP", "5cg4514abc", None, TODAY) == dt.date(2024, 12, 16)
    assert built_from_serial("HP", "2UA6201ABC", None, TODAY) == dt.date(2026, 5, 11)


def test_the_decade_is_the_latest_one_that_fits() -> None:
    # Week 50 of 2026 hasn't come yet: 2016.
    assert built_from_serial("HP", "MXL6501ABC", None, TODAY) == dt.date(2016, 12, 12)
    # A 2015 processor in a computer "built" in 2026: built in 2016.
    old = built_from_serial("HP", "2UA6351ABC", "Intel Core i3-6100 CPU @ 3.70GHz", TODAY)
    assert old == dt.date(2016, 8, 29)
    new = built_from_serial("HP", "MXL6201ABC", "Intel Core Ultra 5 235T", TODAY)
    assert new == dt.date(2026, 5, 11)


def test_no_date_from_other_makers_or_other_serials() -> None:
    assert built_from_serial("Dell Inc.", "MXL0042ABC", None, TODAY) is None
    assert built_from_serial("HP", "MXL0992ABC", None, TODAY) is None  # no week 99
    assert built_from_serial("HP", "CZC12", None, TODAY) is None
    assert built_from_serial("HP", None, None, TODAY) is None


def test_processor_generations() -> None:
    assert processor_year("Intel Core i5-14500T") == 2023
    assert processor_year("Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz") == 2017
    assert processor_year("Intel Core i5-1135G7") == 2020  # a laptop's 11th generation
    assert processor_year("Intel Core Ultra 7 255U") == 2024
    assert processor_year("AMD Ryzen 5 PRO 4650G") == 2020
    assert processor_year("Intel Xeon W-2133 CPU @ 3.60GHz") is None
