"""A deliberately failing test for the action demo workflow (.github/workflows/action-demo.yml).

The bug: a discount meant for orders of 10 or more items starts at 11.
"""


def bulk_discount(quantity: int, unit_price: float) -> float:
    total = quantity * unit_price
    if quantity > 10:
        return round(total * 0.9, 2)
    return total


def test_small_order_pays_full_price() -> None:
    assert bulk_discount(3, 5.0) == 15.0


def test_ten_items_get_the_bulk_discount() -> None:
    assert bulk_discount(10, 5.0) == 45.0
