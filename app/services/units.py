"""Unit conversion utilities — shared between recipes and sub-recipes."""


def to_display(amount: float, unit: str) -> tuple[float, str]:
    """Convert storage units (kg/L) to display units (g/ml) for recipes."""
    if unit == 'kg':
        return round(amount * 1000, 1), 'g'
    elif unit == 'L':
        return round(amount * 1000, 1), 'ml'
    return amount, unit


def from_display(display_amount: float, base_unit: str) -> float:
    """Convert display units (g/ml) back to storage units (kg/L)."""
    if base_unit in ('kg', 'L'):
        return display_amount / 1000
    return display_amount
