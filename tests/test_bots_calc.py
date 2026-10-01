"""The calc library bot: arithmetic and unit conversion, offline and safe."""

import uuid

import pytest

from app.bots.engine import BotEngine
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import BotTestRequest


def _ns():
    return load_bot_code(get_library_entry("calc")["code"]).namespace


class TestCalculate:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("2*(3+4)", "14"),
            ("2^10", "1024"),
            ("7/2", "3.5"),
            ("15% of 80", "12"),
            ("sqrt(2)", "1.414213562"),
            ("max(1, 2)", "2"),
            ("1,000,000 / 4", "250000"),
            ("sin(30)", "0.5"),
            ("round(pi, 2)", "3.14"),
            ("-3 + 5", "2"),
        ],
    )
    def test_evaluates(self, expression, expected):
        ns = _ns()
        assert ns["format_number"](ns["calculate"](expression)) == expected

    @pytest.mark.parametrize(
        "expression",
        [
            "9**9**9",
            "10**1000 * 10**1000",
            "1/0",
            "__import__('os').system('id')",
            "().__class__",
            "open('x')",
            "a" * 300,
            "sqrt(-1)",
            "(-8) ** 0.5",
        ],
    )
    def test_refuses_without_hanging(self, expression):
        ns = _ns()
        with pytest.raises(ns["CalcError"]):
            ns["calculate"](expression)


class TestConvert:
    @pytest.mark.parametrize(
        ("value", "source", "target", "expected"),
        [
            (10, "mi", "km", 16.09344),
            (72, "f", "c", 22.2222),
            (100, "celsius", "fahrenheit", 212),
            (30, "psi", "bar", 2.06843),
            (1, "gallon", "liters", 3.78541),
            (10, "l/100km", "mpg", 23.5215),
            (5, "nautical miles", "km", 9.26),
            (12, "in", "cm", 30.48),
            (1, "kwh", "kcal", 860.421),
        ],
    )
    def test_converts(self, value, source, target, expected):
        assert _ns()["convert"](value, source, target) == pytest.approx(expected, rel=1e-4)

    def test_refuses_mismatched_dimensions(self):
        ns = _ns()
        with pytest.raises(ns["CalcError"]):
            ns["convert"](1, "km", "kg")


class TestCommands:
    async def _run(self, text):
        from app.repository.bots import BotRepository

        entry = get_library_entry("calc")
        bot = await BotRepository.create(name=f"calc-{uuid.uuid4().hex[:8]}", code=entry["code"])
        response = await BotEngine().test_run(bot, BotTestRequest(text=text, is_dm=True))
        assert response.error is None, response.error
        return [r["text"] for r in response.replies]

    async def test_calc_command(self, test_db):
        assert await self._run("calc 2*(3+4)") == ["2*(3+4) = 14"]

    async def test_convert_command(self, test_db):
        assert await self._run("convert 10 mi to km") == ["10 mi = 16.0934 km"]

    async def test_bad_input_explains_itself(self, test_db):
        assert (await self._run("calc 1/0")) == ["calc: division by zero"]
        assert (await self._run("convert ten miles"))[0].startswith("usage: convert")
