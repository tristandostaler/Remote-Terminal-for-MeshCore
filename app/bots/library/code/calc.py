"""Calculator and unit converter, entirely offline.

``calc <expression>`` (or ``math``) evaluates arithmetic: + - * / // % and
powers (``^`` or ``**``), parentheses, ``15% of 80``, the constants pi, e and
tau, and common functions (sqrt, sin, cos, tan, log, ln, exp, round...).
``convert <value> <unit> to <unit>`` converts length, mass, volume, speed,
pressure, area, energy, power, temperature and fuel economy.

A tiny language model cannot do arithmetic reliably; this bot can. The
expression is parsed with ``ast`` and only numbers, the listed operators,
names and functions are evaluated -- never ``eval`` -- and the size of every
intermediate result is bounded so ``9**9**9`` cannot hang the node.
"""

import ast
import math
import operator
import re

from remoteterm import bot

BOT_META = {
    "key": "calc",
    "name": "calc",
    "category": "Utility",
    "description": "Calculator (calc 2*(3+4)) and unit converter (convert 10 mi to km)",
    "long_description": (
        "`calc <expression>` (or `math`) works out arithmetic: + - * / and powers (^), "
        "parentheses, `15% of 80`, pi and e, and functions such as sqrt, sin, log and round. "
        "`convert <value> <unit> to <unit>` converts length, mass, volume, speed, pressure, "
        "area, energy, power, temperature and fuel economy, e.g. `convert 72 f to c` or "
        "`convert 30 psi to bar`. Entirely offline — no network access."
    ),
    "version": "1.0.0",
}

MAX_EXPRESSION = 200
MAX_MAGNITUDE = 1e100
MAX_EXPONENT = 1000

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": lambda x: math.sin(math.radians(x)),
    "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "asin": lambda x: math.degrees(math.asin(x)),
    "acos": lambda x: math.degrees(math.acos(x)),
    "atan": lambda x: math.degrees(math.atan(x)),
    "log": math.log10,
    "log10": math.log10,
    "log2": math.log2,
    "ln": math.log,
    "exp": math.exp,
    "hypot": math.hypot,
    "min": min,
    "max": max,
}


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


class CalcError(ValueError):
    pass


def _check(value):
    if isinstance(value, complex) or not math.isfinite(value) or abs(value) > MAX_MAGNITUDE:
        raise CalcError("result too large or not a real number")
    return value


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return _check(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise CalcError("exponent too large")
        try:
            return _check(_BINARY[type(node.op)](left, right))
        except ZeroDivisionError:
            raise CalcError("division by zero") from None
        except OverflowError:
            raise CalcError("result too large") from None
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
        and 1 <= len(node.args) <= 2
    ):
        args = [_eval(a) for a in node.args]
        try:
            return _check(_FUNCTIONS[node.func.id](*args))
        except (ValueError, OverflowError, TypeError):
            raise CalcError(f"{node.func.id}: value out of range") from None
    raise CalcError("only numbers, + - * / ^ %, parentheses and math functions")


def calculate(expression):
    """Evaluate ``expression``; raises CalcError with a short reason."""
    text = expression.strip().lower()
    if not text:
        raise CalcError("nothing to calculate")
    if len(text) > MAX_EXPRESSION:
        raise CalcError("expression too long")
    text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
    text = _THOUSANDS.sub("", text)  # 1,000,000 -> 1000000; max(1, 2) keeps its comma
    # "15% of 80" and "15 % of 80" -> (15/100)*80
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\b", r"(\1/100)*", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise CalcError("could not read that expression") from None
    return _eval(tree)


def format_number(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


# unit -> (dimension, factor to the dimension's base unit)
_UNITS = {
    # length, metres
    "mm": ("length", 0.001),
    "cm": ("length", 0.01),
    "m": ("length", 1.0),
    "km": ("length", 1000.0),
    "in": ("length", 0.0254),
    "ft": ("length", 0.3048),
    "yd": ("length", 0.9144),
    "mi": ("length", 1609.344),
    "nmi": ("length", 1852.0),
    # mass, kilograms
    "mg": ("mass", 1e-6),
    "g": ("mass", 0.001),
    "kg": ("mass", 1.0),
    "t": ("mass", 1000.0),
    "oz": ("mass", 0.028349523125),
    "lb": ("mass", 0.45359237),
    "st": ("mass", 6.35029318),
    # volume, litres (cup, pint, quart, gallon and fluid ounce are US)
    "ml": ("volume", 0.001),
    "cl": ("volume", 0.01),
    "dl": ("volume", 0.1),
    "l": ("volume", 1.0),
    "m3": ("volume", 1000.0),
    "tsp": ("volume", 0.00492892159375),
    "tbsp": ("volume", 0.01478676478125),
    "floz": ("volume", 0.0295735295625),
    "cup": ("volume", 0.2365882365),
    "pt": ("volume", 0.473176473),
    "qt": ("volume", 0.946352946),
    "gal": ("volume", 3.785411784),
    "impgal": ("volume", 4.54609),
    # speed, metres per second
    "m/s": ("speed", 1.0),
    "km/h": ("speed", 1 / 3.6),
    "mph": ("speed", 0.44704),
    "kn": ("speed", 1852 / 3600),
    "ft/s": ("speed", 0.3048),
    # pressure, pascals
    "pa": ("pressure", 1.0),
    "hpa": ("pressure", 100.0),
    "kpa": ("pressure", 1000.0),
    "mbar": ("pressure", 100.0),
    "bar": ("pressure", 100000.0),
    "psi": ("pressure", 6894.757293168),
    "atm": ("pressure", 101325.0),
    "mmhg": ("pressure", 133.322387415),
    "inhg": ("pressure", 3386.389),
    # area, square metres
    "m2": ("area", 1.0),
    "km2": ("area", 1e6),
    "ha": ("area", 1e4),
    "acre": ("area", 4046.8564224),
    "ft2": ("area", 0.09290304),
    "mi2": ("area", 2589988.110336),
    # energy, joules
    "j": ("energy", 1.0),
    "kj": ("energy", 1000.0),
    "cal": ("energy", 4.184),
    "kcal": ("energy", 4184.0),
    "wh": ("energy", 3600.0),
    "kwh": ("energy", 3.6e6),
    # power, watts
    "w": ("power", 1.0),
    "kw": ("power", 1000.0),
    "hp": ("power", 745.69987158227),
}
_ALIASES = {
    "millimeter": "mm",
    "millimetre": "mm",
    "centimeter": "cm",
    "centimetre": "cm",
    "meter": "m",
    "metre": "m",
    "kilometer": "km",
    "kilometre": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "mile": "mi",
    "nauticalmile": "nmi",
    "gram": "g",
    "kilogram": "kg",
    "kilo": "kg",
    "tonne": "t",
    "ounce": "oz",
    "pound": "lb",
    "lbs": "lb",
    "stone": "st",
    "milliliter": "ml",
    "millilitre": "ml",
    "liter": "l",
    "litre": "l",
    "teaspoon": "tsp",
    "tablespoon": "tbsp",
    "fl.oz": "floz",
    "pint": "pt",
    "quart": "qt",
    "gallon": "gal",
    "kmh": "km/h",
    "kph": "km/h",
    "knot": "kn",
    "kt": "kn",
    "ms": "m/s",
    "fps": "ft/s",
    "pascal": "pa",
    "millibar": "mbar",
    "atmosphere": "atm",
    "hectare": "ha",
    "sqm": "m2",
    "m²": "m2",
    "km²": "km2",
    "sqft": "ft2",
    "ft²": "ft2",
    "joule": "j",
    "calorie": "cal",
    "watt": "w",
    "kilowatt": "kw",
    "horsepower": "hp",
    "celsius": "c",
    "°c": "c",
    "fahrenheit": "f",
    "°f": "f",
    "kelvin": "k",
    "l/100": "l/100km",
}
_TEMPERATURES = {"c", "f", "k"}
_FUEL = {"mpg", "l/100km"}
_CONVERT = re.compile(r"^\s*(-?[\d.,]+)\s*([^\s]+(?:\s+[^\s]+)?)\s+(?:to|in|into|->)\s+(.+?)\s*$")


def _unit(name):
    name = name.strip().lower().replace(" ", "")
    if name in _UNITS or name in _TEMPERATURES or name in _FUEL:
        return name
    if name in _ALIASES:
        return _ALIASES[name]
    if name.endswith("s") and name[:-1] in _ALIASES:
        return _ALIASES[name[:-1]]
    if name.endswith("s") and name[:-1] in _UNITS:
        return name[:-1]
    raise CalcError(f"unknown unit: {name}")


def _to_kelvin(value, unit):
    return {"c": value + 273.15, "f": (value - 32) * 5 / 9 + 273.15, "k": value}[unit]


def _from_kelvin(value, unit):
    return {"c": value - 273.15, "f": (value - 273.15) * 9 / 5 + 32, "k": value}[unit]


def convert(value, source, target):
    """Convert ``value`` from ``source`` to ``target`` units."""
    source, target = _unit(source), _unit(target)
    if source in _TEMPERATURES and target in _TEMPERATURES:
        return _from_kelvin(_to_kelvin(value, source), target)
    if source in _FUEL and target in _FUEL:
        if value == 0:
            raise CalcError("fuel economy cannot be zero")
        # mpg (US) and L/100 km are reciprocal: 235.214583... = 100 * 3.785411784 / 1.609344
        return value if source == target else 235.2145833 / value
    if source in _UNITS and target in _UNITS:
        dimension, factor = _UNITS[source]
        target_dimension, target_factor = _UNITS[target]
        if dimension == target_dimension:
            return value * factor / target_factor
    raise CalcError(f"cannot convert {source} to {target}")


@bot.on_keyword()
@bot.on_keyword("calc", "math")
async def calc(ctx, msg):
    expression = msg.arg_text.strip()
    if not expression:
        await ctx.reply(
            "usage: calc <expression> — e.g. calc 2*(3+4), calc 15% of 80, calc sqrt(2)"
        )
        return
    try:
        result = calculate(expression)
    except CalcError as exc:
        await ctx.reply(f"calc: {exc}")
        return
    await ctx.reply(f"{expression} = {format_number(result)}")


@bot.on_keyword("convert", "conv")
async def convert_units(ctx, msg):
    match = _CONVERT.match(msg.arg_text)
    if not match:
        await ctx.reply("usage: convert <value> <unit> to <unit> — e.g. convert 10 mi to km")
        return
    raw, source, target = match.groups()
    try:
        value = float(_THOUSANDS.sub("", raw))
        result = convert(value, source, target)
    except ValueError as exc:
        await ctx.reply(f"convert: {exc}")
        return
    await ctx.reply(f"{format_number(value)} {source} = {result:.6g} {target}")
