"""Behaviour checks for pdwx. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from pdwx import weather_data
from pdwx.climate import Climate
from pdwx import config, style
from pdwx.style import delta, rain as rain_text
from pdwx.term import Terminal
from pdwx.ui import parse_date, rank_phrase


def daily(start: date, days: int, hi, lo, rain=0.0) -> dict:
    """Open-Meteo shaped block; hi/lo/rain may be callables of the date."""
    times = [start + timedelta(days=i) for i in range(days)]
    pick = lambda f, d: f(d) if callable(f) else f  # noqa: E731
    return {
        "daily": {
            "time": [d.isoformat() for d in times],
            "temperature_2m_max": [pick(hi, d) for d in times],
            "temperature_2m_min": [pick(lo, d) for d in times],
            "precipitation_sum": [pick(rain, d) for d in times],
            "weather_code": [0 for _ in times],
        }
    }


TODAY = date(2026, 9, 26)


def rain(d: date) -> float:
    return 5.0 if d.toordinal() % 10 == 0 else 0.0


def climate(forecast_hi: float = 25.0) -> Climate:
    # 1940–2025: highs 18–22° by year, except 26 Sep 2013 at 30°; lows 8–12°; rain every 10th day
    history = daily(
        date(1940, 1, 1),
        (date(2025, 12, 31) - date(1940, 1, 1)).days + 1,
        lambda d: 30.0 if d == date(2013, 9, 26) else 18.0 + d.year % 5,
        lambda d: 8.0 + d.year % 5,
        rain,
    )
    this_year = daily(date(2026, 1, 1), (TODAY - date(2026, 1, 1)).days, 21.0, 11.0, rain)
    forecast = daily(TODAY, 16, lambda d: forecast_hi if d == TODAY else 19.0, 10.0, rain)
    return Climate(history, this_year, forecast, TODAY, None, 40.0)


class ClimateTests(unittest.TestCase):
    def test_sources_and_normals(self):
        c = climate()
        self.assertEqual(c.day(TODAY).src, "F")
        self.assertEqual(c.day(date(2026, 5, 1)).src, "H")
        self.assertAlmostEqual(c.normal(TODAY).hi, 20.0, delta=0.1)
        self.assertEqual(c.first_year, 1940)

    def test_rank_record_and_alert(self):
        c = climate(25.0)
        warm, cool, total = c.rank(TODAY, 25.0)
        self.assertEqual((warm, total), (2, 87))  # only 2013's 30° is warmer
        self.assertEqual(c.records(TODAY)["hi"].date.year, 2013)
        self.assertIn("2nd warmest 26 Sep in 87 years", rank_phrase(c, TODAY, 25.0))
        self.assertEqual(c.alerts(), [])
        hot = climate(31.0)
        kinds = [(a.date, k) for a, k, _ in hot.alerts()]
        self.assertIn((TODAY, "hottest"), kinds)

    def test_last_time_and_similar(self):
        c = climate(25.0)
        self.assertEqual(c.last_time(TODAY, 25.0, warm=True).date, date(2013, 9, 26))
        self.assertIsNone(c.last_time(TODAY, 35.0, warm=True))
        self.assertEqual(len(c.similar(c.day(TODAY), 5)), 5)

    def test_long_term_views(self):
        c = climate()
        annual = c.annual()
        self.assertEqual((annual[0][0], annual[-1][0]), (1940, 2025))
        label, change = Climate.change(annual)
        self.assertEqual(label, "the 1950s")
        self.assertLess(abs(change), 0.5)
        self.assertEqual(c.hot_threshold(), 22.0)
        spells = c.dry_spells()
        self.assertEqual(spells["ever"][2], 9)  # rain every 10th day leaves 9 dry days
        self.assertEqual(len(c.year_anomalies(2000)), 366)


class FormattingTests(unittest.TestCase):
    def tearDown(self):
        style.configure()

    def test_numbers(self):
        self.assertEqual(rain_text(0.04), "0mm")
        self.assertEqual(rain_text(0.14), "0.1mm")
        self.assertEqual(rain_text(12.4), "12mm")
        self.assertEqual(delta(6.6), "+7°")
        self.assertEqual(delta(-2.2), "−2°")
        self.assertEqual(delta(0.2), "±0°")

    def test_imperial_units(self):
        style.configure("F", "in", "mph")
        self.assertEqual(style.deg(0), "32°")
        self.assertEqual(style.deg(29.3), "85°")
        self.assertEqual(delta(5), "+9°")  # a difference scales by 1.8 with no offset
        self.assertEqual(style.delta1(1.4), "2.5°")
        self.assertEqual(rain_text(25.4), "1.00in")
        self.assertEqual(rain_text(0.1), "0in")
        self.assertEqual(style.wind(100), "62 mph")
        style.configure("C", "mm", "m/s")
        self.assertEqual(style.wind(36), "10 m/s")

    def test_icon_sets(self):
        for mode in ("nerd", "symbols", "ascii"):
            self.assertEqual(len(style.icon(61, mode)), 1)
        self.assertEqual(style.icon(None, "ascii"), " ")
        self.assertEqual(style.icon(12345, "symbols"), " ")

    def test_parse_date(self):
        self.assertEqual(parse_date("14 Mar 1961", TODAY), (date(1961, 3, 14), True))
        self.assertEqual(parse_date("march 14", TODAY), (date(2026, 3, 14), False))
        self.assertEqual(parse_date("1990-07-02", TODAY), (date(1990, 7, 2), True))
        self.assertEqual(parse_date("2/7/1990", TODAY), (date(1990, 7, 2), True))
        self.assertIsNone(parse_date("31 Feb 2001", TODAY))
        self.assertIsNone(parse_date("soon", TODAY))


class ConfigTests(unittest.TestCase):
    def test_round_trip_and_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertIsNone(config.load())  # first run
            home = weather_data.Place("Springfield", 'Springfield, "IL", United States', 39.8, -89.6, "America/Chicago")
            config.save(
                config.Settings(temperature="F", rain="in", wind="kn", icons="nerd", week_start="sunday", home=home)
            )
            loaded = config.load()
            self.assertEqual(
                (loaded.temperature, loaded.rain, loaded.wind, loaded.icons, loaded.week_start),
                ("F", "in", "kn", "nerd", "sunday"),
            )
            self.assertEqual(loaded.home, home)
            self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o600)
            config.config_path().write_text('temperature = "K"\nicons = 3\n[home]\nname = "x"\n')
            broken = config.load()
            self.assertEqual((broken.temperature, broken.icons, broken.home), ("C", "symbols", None))


class SafetyTests(unittest.TestCase):
    def test_outside_text_cannot_emit_escapes(self):
        from pdwx.term import Canvas

        c = Canvas(1, 40)
        c.put(0, 0, "Evil\x1b]0;pwned\x07\x9b2J town")
        frame = c.render()
        self.assertNotIn("\x07", frame)
        self.assertNotIn("\x9b", frame)
        self.assertNotIn("\x1b]", frame)  # the only escapes left are our own colour codes

    def test_cache_folder_is_private(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            (Path(tmp) / "pdwx").mkdir(mode=0o755)
            self.assertEqual(weather_data.cache_root().stat().st_mode & 0o777, 0o700)


class KeyTests(unittest.TestCase):
    def keys(self, data: bytes) -> list[str]:
        term = Terminal.__new__(Terminal)
        term._pending = data
        term._read = lambda timeout: b""  # type: ignore[method-assign]
        out = []
        while term._pending:
            out.append(term.read_key(0))
        return out

    def test_decoding(self):
        self.assertEqual(self.keys(b"\x1b[A\x1b[6~\x1bOP\x1b[Z"), ["up", "pgdn", "f1", "btab"])
        self.assertEqual(self.keys(b"\x1b"), ["esc"])  # a lone Esc is immediate, not a 1 s wait
        self.assertEqual(self.keys("é\r\x7f".encode()), ["é", "enter", "backspace"])


class NetworkTests(unittest.TestCase):
    def error(self, reason: str) -> urllib.error.HTTPError:
        body = io.BytesIO(json.dumps({"error": True, "reason": reason}).encode())
        return urllib.error.HTTPError("u", 429, "Too Many Requests", {}, body)

    def test_waits_out_the_minute_limit(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value = io.BytesIO(b'{"daily": {"time": []}}')
        calls = [self.error("Minutely API request limit exceeded. Please try again in one minute."), ok]
        progress = []
        with (
            mock.patch("urllib.request.urlopen", side_effect=calls),
            mock.patch("time.monotonic", side_effect=[0.0, 0.0, 63.0]),
        ):
            data = weather_data._get_json(
                "https://example.invalid/x", progress=lambda m, s: progress.append(s), sleep=lambda s: None
            )
        self.assertEqual(data, {"daily": {"time": []}})
        self.assertTrue(progress and progress[0] > 60)

    def test_hourly_limit_fails_fast(self):
        hourly = [self.error("Hourly API request limit exceeded.")]
        with mock.patch("urllib.request.urlopen", side_effect=hourly), self.assertRaises(weather_data.RateLimited):
            weather_data._get_json("https://example.invalid/x", sleep=lambda s: None)

    def test_borrows_system_certificates_when_python_has_none(self):
        none = ssl.DefaultVerifyPaths(None, None, "SSL_CERT_FILE", "", "SSL_CERT_DIR", "")
        for paths, exists, loads in (
            (none, True, True),
            (none, False, False),
            (ssl.get_default_verify_paths(), True, None),
        ):
            with (
                mock.patch.object(weather_data, "_TLS", None),
                mock.patch("ssl.get_default_verify_paths", return_value=paths),
                mock.patch("os.path.exists", return_value=exists),
                mock.patch("ssl.SSLContext.load_verify_locations") as load,
            ):
                weather_data._tls()
            if loads is None:
                loads = paths.cafile is None and paths.capath is None
            self.assertEqual(load.called, loads)

    def test_certificate_failure_is_not_retried(self):
        bad = urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        with mock.patch("urllib.request.urlopen", side_effect=[bad]) as urlopen, self.assertRaises(RuntimeError) as ctx:
            weather_data._get_json("https://example.invalid/x", sleep=lambda s: None)
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("local issuer", str(ctx.exception))

    def test_merge_daily_prefers_later_values(self):
        a = {"daily": {"time": ["2026-01-01", "2026-01-02"], "x": [1, 2]}}
        b = {"daily": {"time": ["2026-01-02", "2026-01-03"], "x": [None, 3]}}
        merged = weather_data.merge_daily(a, b)["daily"]
        self.assertEqual(merged["time"], ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(merged["x"], [1, 2, 3])


class CacheTests(unittest.TestCase):
    def test_history_extends_only_missing_years(self):
        place = weather_data.Place("T", "T", 1.0, 2.0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            old = daily(date(1940, 1, 1), (date(2024, 12, 31) - date(1940, 1, 1)).days + 1, 20.0, 10.0)
            (weather_data.cache_root() / f"era5-{place.key}.json").write_text(json.dumps(old))
            fresh = daily(date(2025, 1, 1), 365, 21.0, 11.0)
            with mock.patch.object(weather_data, "_get_json", return_value=fresh) as get:
                result = weather_data.load_history(place, date(2026, 3, 1))
            self.assertIn("start_date=2025-01-01", get.call_args[0][0])
            self.assertEqual(result["daily"]["time"][-1], "2025-12-31")
            self.assertEqual(result["daily"]["time"][0], "1940-01-01")


if __name__ == "__main__":
    unittest.main()
