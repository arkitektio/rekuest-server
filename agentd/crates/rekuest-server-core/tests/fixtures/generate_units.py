"""Generate units.json: what the Python server's `rekuest_core/units.py` answers, per expression.

Run with the rekuest server's venv (it pins the pint the server uses):

    /path/to/rekuest-server/.venv/bin/python generate_units.py /path/to/rekuest-server/rekuest_core/units.py

Every unit name, symbol and alias of pint's default registry, their plurals, prefixed forms,
every dimension, compound and preprocessed spellings, and invalid expressions; each with the
canonical dimensionality string or, when Python raises, an error.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pint

spec = importlib.util.spec_from_file_location("units", sys.argv[1])
units = importlib.util.module_from_spec(spec)
spec.loader.exec_module(units)
ureg = units.get_unit_registry()

expressions: list[str] = []


def add(expression: str) -> None:
    if expression not in expressions:
        expressions.append(expression)


unit_keys = sorted(ureg._units.keys())
for key in unit_keys:
    add(key)
for key in unit_keys:
    definition = ureg._units[key]
    if key == definition.name and len(key) > 1:
        add(key + "s")

prefixes = ["m", "k", "M", "G", "µ", "u", "n", "p", "f", "c", "d", "da", "h", "Ki", "Mi",
            "milli", "kilo", "micro", "nano", "mega", "centi", "mu", "q", "Q"]
bases = ["m", "s", "g", "A", "V", "W", "Hz", "F", "Ω", "ohm", "J", "N", "Pa", "L", "l", "mol", "K",
         "meter", "second", "gram", "volt", "watt", "hertz", "farad", "joule", "newton", "liter",
         "degC", "dB", "B", "bit", "byte", "eV", "cd", "rad", "sr", "T", "Wb", "S", "H", "C", "Bq"]
for prefix in prefixes:
    for base in bases:
        add(prefix + base)
for base in ["meters", "seconds", "volts", "grams", "kilometers", "millivolts", "degCs", "hertzs"]:
    add(base)

for dimension in sorted(ureg._dimensions.keys()):
    add(dimension)
for compound in [
    "[mass] * [length] ** 2 / [time] ** 3 / [current]", "[length] / [time]", "[length] ** 0.5",
    "1 / [time]", "[]", "[length] * [length]", "[velocity]", "[force] / [area]", "[foo]",
    "[length", "length]", "[length] + [time]",
]:
    add(compound)

for expression in [
    "", "dimensionless", " dimensionless ", "dimensionless * m", "1", "2", "3.5", "1e3", "0.5 * m",
    "m", "m/s", "m / s", "m*s", "m * s", "kg m", "kg m / s^2", "kg·m/s²", "m²", "m³", "s⁻¹", "m^2",
    "m**2", "m ** 2", "m**-1", "m^-2", "m ** 0.5", "m**(1/2)", "(m/s)**2", "(kg * m) / (s ** 2)",
    "meter squared", "meter cubed", "cubic meter", "square meter", "sq m", "m per s",
    "meter per second", "miles per hour", "1/s", "1 / second", "2 m", "2m", "2kg", "10 kHz",
    "m // s", "m % s", "m + s", "m - m", "-m", "+m", "m m", "m s", "°C", "°F", "°K", "degC", "degF",
    "delta_degC", "delta_degF", "Δ°C", "kelvin", "delta_kelvin", "mdegC", "kdegC", "mdB", "dBm",
    "percent", "%", "ppm", "count", "radian", "degree", "deg", "arcmin", "turn", "Å", "angstrom",
    "µm", "μm", "um", "micron", "mum", "mcm", "nm", "pF", "nF", "uF", "mV", "kV", "MV", "mA",
    "µA", "Hz", "kHz", "MHz", "GHz", "THz", "rpm", "bpm", "mmHg", "psi", "bar", "mbar", "atm",
    "Torr", "cal", "kcal", "Wh", "kWh", "eV", "keV", "MeV", "Da", "kDa", "M", "mM", "µM", "nM",
    "molar", "L", "mL", "µL", "l", "ml", "cc", "gallon", "pint", "cup", "ft", "inch", "in",
    "yard", "mile", "nmi", "au", "ly", "pc", "parsec", "lightyear", "hour", "h", "min", "minute",
    "day", "d", "week", "year", "a", "yr", "ms", "µs", "ns", "ps", "fs", "Ohm", "ohm", "Ω",
    "kΩ", "MΩ", "S", "siemens", "mS", "T", "tesla", "gauss", "G", "Wb", "H", "henry",
    "Gy", "Sv", "Bq", "Ci", "lm", "lx", "cd", "nit", "N", "kN", "dyn", "lbf", "kgf", "J", "erg",
    "W", "hp", "C", "coulomb", "Ah", "mAh", "F", "farad", "V", "volt", "A", "ampere", "amp",
    "g", "kg", "mg", "µg", "t", "tonne", "lb", "oz", "stone", "grain", "slug", "u",
    "pixel", "px", "dot", "byte", "B", "kB", "MB", "KiB", "MiB", "bit", "baud", "bps",
    "planck_constant", "h", "hbar", "c", "speed_of_light", "e", "elementary_charge", "k", "k_B",
    "N_A", "R", "G", "gravitational_constant", "g_0", "g0", "pi", "π", "ln10", "tansec",
    "mu_0", "eps_0", "alpha", "fine_structure_constant", "m_e", "m_p",
    "foo", "meterz", "m$", "m/", "*m", "()", "(m", "m)", "m**m", "2**m", "1/0", "0**-1",
    "m ** (1/3)", "m**1.5", "s**-0.5", "meter^2 / second^2", "J/(mol*K)", "V/m", "A/m²",
    "cd/m^2", "W/(m^2*sr)", "mol/L", "g/mol", "kg/m^3", "Pa*s", "N*m", "rad/s", "1/rad",
    "count/s", "Hz/V", "V/Hz**0.5", "V/sqrt(Hz)", "nan", "NaN", "inf", "m nan",
    "meter-second", "m.s", "m,s", "1,000 m", "m @ s", "3 (kg ** 2)", "(3) m", "kilo m",
    "ms**-1", "m s^-1", "m·s⁻¹", "m⁻²", "m¹·⁵", "square foot", "cubic inch", "sq ft",
    "1e3m", "07", "1_0 m", "1_", "1__0", "0x1g", "0x10", "1j", "m$", "$m", "m?s", "m's", "m # s", "2µm",
    ".5m", "1.5.2", "m&s", "m!", "m`s", "€", "∞", "m ∞ s", "m\\s", "1.e3", "m*	 s", "m  /  s",
    "meter    squared", "kg squared", "xsquared", "m ** 2 ** 2", "2 ** 3 m", "m ** -0.5", "(m)", "((m))",
    "m / (s * A)", "m / s * A", "m / s / A", "1 / 2 m", "4 m / 4", "0.5 m / 0.5", "10 % 3 m",
]:
    add(expression)

results = []
for expression in expressions:
    try:
        results.append({"expression": expression, "dimensionality": units.dimensionality_of(expression)})
    except Exception as error:  # noqa: BLE001 -- every refusal counts, whatever its type
        results.append({"expression": expression, "error": type(error).__name__})

out = Path(__file__).with_name("units.json")
out.write_text(json.dumps({"pint": pint.__version__, "cases": results}, indent=1, ensure_ascii=False) + "\n")
errors = sum(1 for r in results if "error" in r)
print(f"wrote {out}: {len(results)} cases ({errors} errors), pint {pint.__version__}")
