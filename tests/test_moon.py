"""The Moon: positions against Meeus's worked examples and USNO's published times, and the picture's geometry."""

from __future__ import annotations

import math
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pdwx import moon

APRIL_1992 = datetime(1992, 4, 12, tzinfo=UTC)  # the date of Meeus's examples 47.a, 48.a and 53.a
CAPE_TOWN = (-33.92, 18.42)
HOUSTON = (29.76, -95.36)


def angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


class PositionTests(unittest.TestCase):
    def test_meeus_worked_examples(self):
        t = (moon.julian_day(APRIL_1992) - 2451545) / 36525
        lon, lat, dist, f, node = moon._moon(t)
        self.assertAlmostEqual(lon, 133.162655, delta=0.001)  # 47.a
        self.assertAlmostEqual(lat, -3.229126, delta=0.01)
        self.assertAlmostEqual(dist, 368409.7, delta=50)
        dpsi, eps = moon._nutation(t)
        ra, dec = moon._equatorial(lon + dpsi, lat, eps)
        lib_lon, lib_lat, axis = moon._libration(lon + dpsi, lat, ra, dpsi, eps, f, node)
        self.assertAlmostEqual(lib_lon, -1.206, delta=0.01)  # 53.a
        self.assertAlmostEqual(lib_lat, 4.194, delta=0.01)
        self.assertAlmostEqual(axis, 15.08, delta=0.05)
        sky = moon.moon_sky(APRIL_1992, 0.0, 0.0)
        self.assertAlmostEqual(sky.lit, 0.6786, delta=0.001)  # 48.a
        sun_lon, _ = moon._sun(t)
        sra, sdec = moon._equatorial(sun_lon, 0.0, eps)
        self.assertAlmostEqual(moon._position_angle(ra, dec, sra, sdec) % 360, 285.0, delta=0.1)

    def test_rise_and_set_match_usno(self):
        # USNO, 26 Sep 2026: Cape Town (UTC+2) rise 18:44 set 06:07; Houston (UTC−5) rise 19:08 set 07:05
        for (lat, lon), hours, rise_at, set_at in ((CAPE_TOWN, 2, "18:44", "06:07"), (HOUSTON, -5, "19:08", "07:05")):
            rise, sett = moon.rise_set(date(2026, 9, 26), timezone(timedelta(hours=hours)), lat, lon)
            for got, want in ((rise, rise_at), (sett, set_at)):
                h, m = map(int, want.split(":"))
                self.assertLessEqual(abs(got.hour * 60 + got.minute - (h * 60 + m)), 2, (lat, want, got))

    def test_phases_match_usno(self):
        full = moon.next_phase(datetime(2026, 9, 20, tzinfo=UTC), 180)
        new = moon.next_phase(datetime(2026, 9, 1, tzinfo=UTC), 0)
        self.assertLess(abs(full - datetime(2026, 9, 26, 16, 49, tzinfo=UTC)), timedelta(minutes=3))
        self.assertLess(abs(new - datetime(2026, 9, 11, 3, 27, tzinfo=UTC)), timedelta(minutes=3))
        back = moon.next_phase(datetime(2026, 9, 20, tzinfo=UTC), 0, backwards=True)
        self.assertLess(abs(back - datetime(2026, 9, 11, 3, 27, tzinfo=UTC)), timedelta(minutes=3))

    def test_phase_name_follows_the_local_day(self):
        tokyo = ZoneInfo("Asia/Tokyo")  # full moon 16:49 UTC on 26 Sep is 1:49 am on 27 Sep there
        self.assertEqual(moon.phase_name(date(2026, 9, 26), tokyo), "Waxing Gibbous")
        self.assertEqual(moon.phase_name(date(2026, 9, 27), tokyo), "Full Moon")
        self.assertEqual(moon.phase_name(date(2026, 9, 26), ZoneInfo("America/Chicago")), "Full Moon")
        self.assertEqual(moon.phase_name(date(2026, 9, 15), tokyo), "Waxing Crescent")

    def test_orientation_flips_between_hemispheres(self):
        """At transit the Moon's north points up from the north and down from the south (± its 25° tilt)."""

        def at_transit(lat: float, lon: float, facing: float) -> moon.MoonSky:
            start = datetime(2026, 9, 20, tzinfo=UTC)
            best = min(
                (moon.moon_sky(start + timedelta(minutes=10 * i), lat, lon) for i in range(150)),
                key=lambda s: angle_gap(s.azimuth, facing) - s.altitude / 1000,
            )
            self.assertGreater(best.altitude, 0)
            return best

        self.assertLess(angle_gap(at_transit(*HOUSTON, 180).axis, 0), 30)
        self.assertLess(angle_gap(at_transit(*CAPE_TOWN, 0).axis, 180), 30)

    def test_rising_moon_tilts_towards_the_hidden_pole(self):
        """Low in the east, the Moon's north tips up-left from the north and down-left from the south."""
        for (lat, lon), expect in ((HOUSTON, 300), (CAPE_TOWN, 240)):
            start = datetime(2026, 9, 20, tzinfo=UTC)
            rising = next(
                s
                for s in (moon.moon_sky(start + timedelta(minutes=10 * i), lat, lon) for i in range(300))
                if 3 < s.altitude < 15 and 60 < s.azimuth < 120
            )
            self.assertLess(angle_gap(rising.axis, expect), 35, (lat, rising.axis))


class FaceTests(unittest.TestCase):
    def sky(self, lit_towards: float, phase_angle: float) -> moon.MoonSky:
        return moon.MoonSky(
            lit=(1 + math.cos(math.radians(phase_angle))) / 2,
            elongation=180 - phase_angle,
            phase_angle=phase_angle,
            altitude=30,
            azimuth=90,
            distance=384400,
            limb=lit_towards,
            axis=0,
            lib_lon=0,
            lib_lat=0,
        )

    def brightness(self, grid) -> list[list[float]]:
        return [[sum(p[0]) / 765 * p[1] if p else 0.0 for p in row] for row in grid]

    def test_full_disc_is_round_whatever_the_cell_shape(self):
        for aspect in (1.0, 1.25):
            grid = moon.face(self.sky(0, 0), 20, (255, 255, 255), (0, 0, 0), aspect)
            covered = sum(p[1] for row in grid for p in row if p) * aspect
            self.assertAlmostEqual(covered / (math.pi * 20 * 20), 1.0, delta=0.05)

    def test_the_lit_side_faces_the_bright_limb(self):
        for bearing in (0, 90, 200, 290):
            b = self.brightness(moon.face(self.sky(bearing, 90), 20, (255, 255, 255), (0, 0, 0)))
            mid = len(b) / 2
            sx = sum(v * (x + 0.5 - mid) for row in b for x, v in enumerate(row))
            sy = sum(v * (mid - y - 0.5) for y, row in enumerate(b) for v in row)
            self.assertLess(angle_gap(math.degrees(math.atan2(sx, sy)), bearing), 10, bearing)

    def test_new_moon_is_dark_and_maria_are_darker_than_highlands(self):
        new = self.brightness(moon.face(self.sky(0, 179), 20, (255, 255, 255), (0, 0, 0)))
        self.assertLess(max(max(row) for row in new), 0.1)
        self.assertLess(moon._albedo(17, 59), 0.75 * moon._albedo(-10, 20))  # Mare Crisium vs southern highlands
        self.assertLess(moon._albedo(33, -16), 0.75 * moon._albedo(-10, 20))  # Mare Imbrium


class MoonViewTests(unittest.TestCase):
    def test_hours_days_and_back_to_now(self):
        import os
        import tempfile

        os.environ["XDG_STATE_HOME"] = os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
        from test_render import TODAY, make_ui

        ui, _term = make_ui(20, 60)
        ui.view = "moon"
        self.assertIsNone(ui.moon_hour)  # live
        ui.handle("left")
        self.assertEqual(ui._moon_moment().hour, 21)  # another day opens at 9 pm
        ui.handle("down")
        ui.handle("down")
        ui.handle("down")
        moment = ui._moon_moment()
        self.assertEqual((moment.date(), moment.hour), (TODAY, 0))  # past midnight moves the day
        ui.handle("g")
        self.assertIsNone(ui.moon_hour)
        self.assertEqual(ui.sel, TODAY)
