"""Cached pint unit registry and QUANTITY-port unit semantics."""

import functools
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import pint

DIMENSIONLESS = "dimensionless"


@functools.lru_cache(maxsize=1)
def get_unit_registry() -> "pint.UnitRegistry":
    """The process-wide pint registry. Lazily built, as construction is expensive (~100ms)."""
    import pint

    return pint.UnitRegistry()


def _render_dimensionality(dims: Mapping[str, float]) -> str:
    """Deterministic string form of a pint dimensionality mapping.

    ``str(UnitsContainer)`` is not order-stable (it depends on the registry's cache
    history within the process), so exact-string comparison and cross-process
    persistence need our own canonical rendering: terms sorted alphabetically,
    positive exponents as the numerator, negative ones appended as divisions.
    The result is itself parseable by ``get_dimensionality``.
    """
    positive = sorted((dim, exp) for dim, exp in dims.items() if exp > 0)
    negative = sorted((dim, exp) for dim, exp in dims.items() if exp < 0)
    if not positive and not negative:
        return DIMENSIONLESS

    def term(dim: str, exp: float) -> str:
        exp = abs(exp)
        return dim if exp == 1 else f"{dim} ** {exp:g}"

    rendered = " * ".join(term(dim, exp) for dim, exp in positive) if positive else "1"
    for dim, exp in negative:
        rendered += f" / {term(dim, exp)}"
    return rendered


def dimensionality_of(expression: str) -> str:
    """Canonical dimensionality string for a unit or dimension expression.

    Accepts unit names ("mV", "volt"), dimensionality expressions
    ("[mass] * [length] ** 2 / [time] ** 3 / [current]") and the special
    "dimensionless" sentinel, returning the identical canonical string for
    equal dimensions. Raises ValueError on anything pint cannot parse, so
    pydantic surfaces it as a normal validation error.
    """
    import pint

    if expression.strip() == DIMENSIONLESS:
        # pint's parser cannot resolve the sentinel of an empty dimensionality.
        return DIMENSIONLESS
    try:
        dims = get_unit_registry().get_dimensionality(expression)
    except (pint.PintError, KeyError, ValueError, TypeError) as e:
        raise ValueError(f"Unknown or unparseable unit '{expression}': {e}") from e
    # pint types an exponent as any Scalar, complex included; a dimension's is real.
    return _render_dimensionality(cast("Mapping[str, float]", dims))
