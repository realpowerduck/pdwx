"""Draw every screen at several terminal sizes without a real terminal, and drive the keys."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from pdwx import style
from pdwx.app import App, Loaded
from pdwx.style import Palette
from pdwx.term import Canvas, Theme
from pdwx.ui import VIEWS, WeatherUI
from pdwx.weather_data import Place

TODAY = date(2026, 9, 26)
SIZES = [(20, 60), (24, 80), (40, 120), (50, 200)]


def block(start: date, days: int, offset: float = 0.0, forecast: bool = False) -> dict:
    times = [start + timedelta(days=i) for i in range(days)]
    daily = {
        "time": [d.isoformat() for d in times],
        "temperature_2m_max": [
            22 + 6 * ((d.timetuple().tm_yday % 29) / 29) + offset + (d.year - 1980) * 0.02 for d in times
        ],
        "temperature_2m_min": [11 + 3 * ((d.timetuple().tm_yday % 17) / 17) + offset for d in times],
        "precipitation_sum": [(d.toordinal() % 7) * 1.3 if d.toordinal() % 3 == 0 else 0.0 for d in times],
        "weather_code": [(0, 2, 3, 61, 80, 95, 71)[d.toordinal() % 7] for d in times],
    }
    if forecast:
        daily["wind_speed_10m_max"] = [20.0 + d.day for d in times]
        daily["apparent_temperature_max"] = [25.0 for _ in times]
    return {"timezone": "America/Chicago", "daily": daily}


class FakeTerm:
    def __init__(self, h: int, w: int, keys: list[str] | None = None):
        self.h, self.w, self.keys, self.resized = h, w, list(keys or []), False

    def size(self) -> tuple[int, int]:
        return self.h, self.w

    def write(self, text: str) -> None:
        pass

    def read_key(self, timeout: float) -> str | None:
        return self.keys.pop(0) if self.keys else "esc"


def make_ui(h: int, w: int) -> tuple[WeatherUI, FakeTerm]:
    term = FakeTerm(h, w)
    app = App(term, Palette(Theme()))  # type: ignore[arg-type]
    place = Place("Testville", "Testville, Somewhere, Nowhere", 40.0, -100.0, "America/Chicago")
    history = block(date(1940, 1, 1), (date(2025, 12, 31) - date(1940, 1, 1)).days + 1)
    this_year = block(date(2026, 1, 1), (TODAY - date(2026, 1, 1)).days - 5)
    forecast = block(TODAY - timedelta(days=14), 30, offset=2.0, forecast=True)
    ui = WeatherUI(app, Loaded(place, TODAY, history, this_year, forecast, []))
    ui._start_extras = lambda: None  # no network in tests
    return ui, term


def text(c: Canvas) -> str:
    return "\n".join("".join(cell[0] for cell in row) for row in c.cells)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(
            os.environ,
            {"XDG_STATE_HOME": cls.tmp.name, "XDG_CACHE_HOME": cls.tmp.name, "XDG_CONFIG_HOME": cls.tmp.name},
        )
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()
        style.configure()

    def draw(self, ui: WeatherUI, term: FakeTerm) -> str:
        c = Canvas(term.h, term.w)
        ui.draw(c)
        for row in c.cells:
            self.assertEqual(len(row), term.w)
        return text(c)

    def test_every_view_every_size_both_metrics_and_units(self):
        for units in (("C", "mm", "km/h", "symbols"), ("F", "in", "mph", "ascii")):
            style.configure(*units)
            for h, w in SIZES:
                ui, term = make_ui(h, w)
                for metric in ("temp", "rain"):
                    ui.metric = metric
                    for view in VIEWS:
                        ui.view, ui.drill = view, None
                        with self.subTest(units=units[0], size=(h, w), view=view, metric=metric):
                            screen = self.draw(ui, term)
                            self.assertIn("pdwx", screen)
                    ui.view = "week"
                    for drill in ("years", "detail"):
                        ui.drill, ui.detail = drill, TODAY - timedelta(days=400)
                        with self.subTest(size=(h, w), drill=drill, metric=metric):
                            self.draw(ui, term)
                    ui.drill = None
                ui.help = True
                self.draw(ui, term)

    def test_headline_mentions_rank_and_normal(self):
        ui, term = make_ui(40, 120)
        screen = self.draw(ui, term)
        self.assertIn("Today 26 Sep", screen)
        self.assertIn("Normal", screen)
        self.assertIn("years", screen)

    def test_keys_do_not_crash(self):
        keys = [
            "down",
            "down",
            "right",
            "left",
            "up",
            "enter",
            "down",
            "s",
            "enter",
            "up",
            "left",
            "right",
            "esc",
            "esc",
            "r",
            "tab",
            "pgdn",
            "pgup",
            "right",
            "down",
            "enter",
            "esc",
            "tab",
            "down",
            "r",
            "tab",
            "left",
            "right",
            "tab",
            "up",
            "down",
            "pgup",
            "enter",
            "tab",
            "down",
            "btab",
            "g",
            "3",
            "?",
            "?",
            "home",
        ]
        for h, w in ((24, 80), (40, 120)):
            ui, term = make_ui(h, w)
            for key in keys:
                with self.subTest(size=(h, w), key=key):
                    self.assertIsNone(ui.handle(key))
                    self.draw(ui, term)
            self.assertEqual(ui.handle("esc"), "picker")
            self.assertEqual(ui.handle("q"), "quit")

    def test_go_to_date_prompt(self):
        ui, term = make_ui(40, 120)
        term.keys = list("14 Mar 1961") + ["enter"]
        ui.handle(":")
        self.assertEqual(ui.sel, date(1961, 3, 14))
        self.draw(ui, term)

    def test_settings_screen_saves(self):
        from pdwx import config

        term = FakeTerm(30, 100, ["right", "down", "right", "down", "down", "down", "down", "down", "enter"])
        app = App(term, Palette(Theme()))  # type: ignore[arg-type]
        saved = app.settings(config.Settings(), first_run=True)
        self.assertEqual((saved.temperature, saved.rain), ("F", "in"))
        self.assertEqual(config.load().temperature, "F")


if __name__ == "__main__":
    unittest.main()
