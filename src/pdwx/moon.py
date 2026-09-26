"""The Moon for any place and moment: where it is, how much is lit, how it is turned, and its face.

Positions follow Jean Meeus, "Astronomical Algorithms" (2nd ed.): the Sun from chapter 25 (low
precision), the Moon from the main terms of chapter 47, nutation and sidereal time from chapters
22 and 12, the parallactic angle from chapter 14, the bright limb and lit fraction from chapter 48,
and the optical libration and the tilt of the Moon's axis from chapter 53. Times are treated as
UT; the ~70 s difference from dynamical time moves the Moon by half an arcminute, far below a cell.

The face is USGS's Clementine 750 nm albedo mosaic (public domain), cut down by
scripts/build_moon_map.py. It is lit here from the real direction of the Sun with the
Lommel–Seeliger law, the usual simple model for the Moon's dusty surface: a full Moon comes out
flat and bright to the edge, a crescent fades softly into the terminator.

Screen angles ("bearings") are measured clockwise from straight up, as the observer sees the sky.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import cache
from pathlib import Path

RGB = tuple[int, int, int]
SYNODIC = 29.530589  # days, new moon to new moon
EARTH_RADIUS = 6378.14  # km


def _sin(d: float) -> float:
    return math.sin(math.radians(d))


def _cos(d: float) -> float:
    return math.cos(math.radians(d))


def _atan2(y: float, x: float) -> float:
    return math.degrees(math.atan2(y, x))


def _asin(v: float) -> float:
    return math.degrees(math.asin(max(-1.0, min(1.0, v))))


def julian_day(moment: datetime) -> float:
    return moment.astimezone(UTC).timestamp() / 86400.0 + 2440587.5


# ── Sun and Moon along the ecliptic ─────────────────────────────────────
def _nutation(t: float) -> tuple[float, float]:
    """(nutation in longitude, true obliquity) in degrees, to about half an arcsecond (ch. 22)."""
    node = 125.04452 - 1934.136261 * t
    ls, lm = 280.4665 + 36000.7698 * t, 218.3165 + 481267.8813 * t
    dpsi = -17.20 * _sin(node) - 1.32 * _sin(2 * ls) - 0.23 * _sin(2 * lm) + 0.21 * _sin(2 * node)
    deps = 9.20 * _cos(node) + 0.57 * _cos(2 * ls) + 0.10 * _cos(2 * lm) - 0.09 * _cos(2 * node)
    return dpsi / 3600, 23.4392911 - 0.0130042 * t + deps / 3600


def _sun(t: float) -> tuple[float, float]:
    """Apparent ecliptic longitude (deg) and distance (AU) of the Sun (ch. 25)."""
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t * t
    e = 0.016708634 - 0.000042037 * t
    c = (1.914602 - 0.004817 * t) * _sin(m) + (0.019993 - 0.000101 * t) * _sin(2 * m) + 0.000289 * _sin(3 * m)
    r = 1.000001018 * (1 - e * e) / (1 + e * _cos(m + c))
    return (l0 + c - 0.00569 - 0.00478 * _sin(125.04 - 1934.136 * t)) % 360, r


# (D, M, M', F, longitude in 1e-6 deg, distance in m) — Meeus table 47.A, terms above 0.002°
_LON = (
    (0, 0, 1, 0, 6288774, -20905355),
    (2, 0, -1, 0, 1274027, -3699111),
    (2, 0, 0, 0, 658314, -2955968),
    (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888),
    (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158),
    (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733),
    (2, -1, 0, 0, 45758, -204586),
    (0, 1, -1, 0, -40923, -129620),
    (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755),
    (2, 0, 0, -2, 15327, 10321),
    (0, 0, 1, 2, -12528, 0),
    (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782),
    (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636),
    (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824),
    (1, 0, -1, 0, -5163, -8379),
    (1, 1, 0, 0, 4987, -16675),
    (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445),
    (4, 0, 0, 0, 3861, -11650),
    (2, 0, -3, 0, 3665, 14403),
    (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0),
    (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322),
    (2, -2, 0, 0, 2236, -9884),
)
# (D, M, M', F, latitude in 1e-6 deg) — Meeus table 47.B, terms above 0.0017°
_LAT = (
    (0, 0, 0, 1, 5128122),
    (0, 0, 1, 1, 280602),
    (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237),
    (2, 0, -1, 1, 55413),
    (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573),
    (0, 0, 2, 1, 17198),
    (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822),
    (2, -1, 0, -1, 8216),
    (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200),
    (2, 1, 0, -1, -3359),
    (2, -1, -1, 1, 2463),
    (2, -1, 0, 1, 2211),
    (2, -1, -1, -1, 2065),
    (0, 1, -1, -1, -1870),
    (4, 0, -1, -1, 1828),
    (0, 1, 0, 1, -1794),
)


def _moon(t: float) -> tuple[float, float, float, float, float]:
    """Geometric ecliptic longitude and latitude (deg), distance (km), F and the node (ch. 47)."""
    lp = 218.3164477 + 481267.88123421 * t - 0.0015786 * t * t
    d = 297.8501921 + 445267.1114034 * t - 0.0018819 * t * t
    m = 357.5291092 + 35999.0502909 * t - 0.0001536 * t * t
    mp = 134.9633964 + 477198.8675055 * t + 0.0087414 * t * t
    f = 93.2720950 + 483202.0175233 * t - 0.0036539 * t * t
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    a1, a2, a3 = 119.75 + 131.849 * t, 53.09 + 479264.290 * t, 313.45 + 481266.484 * t
    sl = sr = sb = 0.0
    for kd, km, kmp, kf, cl, cr in _LON:
        arg = kd * d + km * m + kmp * mp + kf * f
        scale = e ** abs(km)
        sl += cl * scale * _sin(arg)
        sr += cr * scale * _cos(arg)
    for kd, km, kmp, kf, cb in _LAT:
        sb += cb * e ** abs(km) * _sin(kd * d + km * m + kmp * mp + kf * f)
    sl += 3958 * _sin(a1) + 1962 * _sin(lp - f) + 318 * _sin(a2)
    sb += -2235 * _sin(lp) + 382 * _sin(a3) + 175 * _sin(a1 - f) + 175 * _sin(a1 + f)
    sb += 127 * _sin(lp - mp) - 115 * _sin(lp + mp)
    node = 125.0445479 - 1934.1362891 * t
    return (lp + sl / 1e6) % 360, sb / 1e6, 385000.56 + sr / 1000, f % 360, node % 360


def _equatorial(lon: float, lat: float, eps: float) -> tuple[float, float]:
    ra = _atan2(_sin(lon) * _cos(eps) - math.tan(math.radians(lat)) * _sin(eps), _cos(lon)) % 360
    return ra, _asin(_sin(lat) * _cos(eps) + _cos(lat) * _sin(eps) * _sin(lon))


def _position_angle(ra: float, dec: float, ra_to: float, dec_to: float) -> float:
    """Direction from one point of the sky towards another, from north through east (eq. 48.5)."""
    return _atan2(
        _cos(dec_to) * _sin(ra_to - ra), _sin(dec_to) * _cos(dec) - _cos(dec_to) * _sin(dec) * _cos(ra_to - ra)
    )


def _libration(
    lon: float, lat: float, ra: float, dpsi: float, eps: float, f: float, node: float
) -> tuple[float, float, float]:
    """Optical libration in longitude and latitude, and the position angle of the Moon's axis (ch. 53).

    The physical libration (a few hundredths of a degree) is left out.
    """
    inc = 1.54242  # the lunar equator's tilt to the ecliptic
    w = lon - dpsi - node
    lib_lon = (_atan2(_sin(w) * _cos(lat) * _cos(inc) - _sin(lat) * _sin(inc), _cos(w) * _cos(lat)) - f + 180) % 360
    lib_lat = _asin(-_sin(w) * _cos(lat) * _sin(inc) - _sin(lat) * _cos(inc))
    v = node + dpsi
    x = _sin(inc) * _sin(v)
    y = _sin(inc) * _cos(v) * _cos(eps) - _cos(inc) * _sin(eps)
    axis = _asin(math.hypot(x, y) * _cos(ra - _atan2(x, y)) / _cos(lib_lat))
    return lib_lon - 180, lib_lat, axis


# ── one moment, one place ───────────────────────────────────────────────
@dataclass
class MoonSky:
    lit: float  # fraction of the disc lit, 0–1
    elongation: float  # Moon minus Sun in ecliptic longitude, 0 new → 180 full → 360
    phase_angle: float  # Sun–Moon–Earth angle in degrees: 0 full, 180 new
    altitude: float  # degrees above the horizon, from where the observer stands
    azimuth: float  # degrees from north through east
    distance: float  # km, centre to centre
    limb: float  # screen bearing of the bright limb
    axis: float  # screen bearing of the Moon's north pole
    lib_lon: float  # optical libration: the selenographic point facing the observer
    lib_lat: float
    parallactic: float  # how far celestial north is turned from the observer's straight up


def elongation(moment: datetime) -> float:
    """Moon minus Sun in ecliptic longitude, degrees: 0 new, 90 first quarter, 180 full, 270 last."""
    t = (julian_day(moment) - 2451545.0) / 36525
    return (_moon(t)[0] - _sun(t)[0]) % 360


def moon_sky(moment: datetime, latitude: float, longitude: float) -> MoonSky:
    jd = julian_day(moment)
    t = (jd - 2451545.0) / 36525
    dpsi, eps = _nutation(t)
    lon, lat, dist, f, node = _moon(t)
    app_lon = (lon + dpsi) % 360
    ra, dec = _equatorial(app_lon, lat, eps)
    sun_lon, sun_r = _sun(t)
    sra, sdec = _equatorial(sun_lon, 0.0, eps)
    # lit fraction from the phase angle (ch. 48)
    cos_psi = _sin(sdec) * _sin(dec) + _cos(sdec) * _cos(dec) * _cos(sra - ra)
    psi = math.degrees(math.acos(max(-1.0, min(1.0, cos_psi))))
    au = 149597870.7
    phase = _atan2(sun_r * au * _sin(psi), dist - sun_r * au * _cos(psi)) % 360
    # where it stands in the local sky (ch. 12, 13, 14)
    theta0 = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t**3 / 38710000
    hour = (theta0 + dpsi * _cos(eps) + longitude - ra) % 360
    alt = _asin(_sin(latitude) * _sin(dec) + _cos(latitude) * _cos(dec) * _cos(hour))
    az = (_atan2(_sin(hour), _cos(hour) * _sin(latitude) - math.tan(math.radians(dec)) * _cos(latitude)) + 180) % 360
    parallax = _asin(EARTH_RADIUS / dist)
    parallactic = _atan2(_sin(hour), math.tan(math.radians(latitude)) * _cos(dec) - _sin(dec) * _cos(hour))
    lib_lon, lib_lat, axis_pa = _libration(app_lon, lat, ra, dpsi, eps, f, node)
    limb_pa = _position_angle(ra, dec, sra, sdec)
    # position angles run north through east, anticlockwise on the sky; the parallactic angle turns
    # celestial north away from the observer's straight up
    return MoonSky(
        lit=(1 + _cos(phase)) / 2,
        elongation=(lon - sun_lon) % 360,
        phase_angle=phase,
        altitude=alt - parallax * _cos(alt),
        azimuth=az,
        distance=dist,
        limb=(parallactic - limb_pa) % 360,
        axis=(parallactic - axis_pa) % 360,
        parallactic=parallactic,
        lib_lon=lib_lon,
        lib_lat=lib_lat,
    )


# ── events ──────────────────────────────────────────────────────────────
def _crossing(fn, t0: datetime, t1: datetime) -> datetime:
    """Bisect for the moment fn changes sign between t0 and t1, to within 20 seconds."""
    v0 = fn(t0)
    while (t1 - t0).total_seconds() > 20:
        mid = t0 + (t1 - t0) / 2
        vm = fn(mid)
        if (vm < 0) == (v0 < 0):
            t0, v0 = mid, vm
        else:
            t1 = mid
    return t0 + (t1 - t0) / 2


def _above_horizon(moment: datetime, latitude: float, longitude: float) -> float:
    """Degrees the Moon's upper limb stands above the horizon, refraction included (Meeus ch. 15).

    The limb meets the horizon when the centre's geocentric altitude is 0.7275 × parallax − 34′.
    """
    jd = julian_day(moment)
    t = (jd - 2451545.0) / 36525
    dpsi, eps = _nutation(t)
    lon, lat, dist, _f, _node = _moon(t)
    ra, dec = _equatorial((lon + dpsi) % 360, lat, eps)
    theta0 = 280.46061837 + 360.98564736629 * (jd - 2451545.0)
    hour = (theta0 + longitude - ra) % 360
    alt = _asin(_sin(latitude) * _sin(dec) + _cos(latitude) * _cos(dec) * _cos(hour))
    return alt - (0.7275 * _asin(EARTH_RADIUS / dist) - 0.5667)


def _horizon(moment: datetime, latitude: float, longitude: float, direction: int) -> datetime | None:
    """The first moonrise or moonset after (direction 1) or before (−1) a moment, within a day."""
    step = timedelta(minutes=10) * direction
    t0, v0 = moment, _above_horizon(moment, latitude, longitude)
    for _ in range(144):
        t1 = t0 + step
        v1 = _above_horizon(t1, latitude, longitude)
        if (v0 < 0) != (v1 < 0):
            return _crossing(lambda t: _above_horizon(t, latitude, longitude), *sorted((t0, t1)))
        t0, v0 = t1, v1
    return None


def as_seen_below(sky: MoonSky, moment: datetime, latitude: float, longitude: float) -> MoonSky:
    """A Moon that is down, turned smoothly from the way it set to the way it will rise.

    Passing beneath the observer, the parallactic angle can swing 100° in an hour. Nobody sees that,
    so the picture keeps the Moon's phase and face for this moment but eases its tilt from the last
    moonset to the next moonrise, which is continuous with the real Moon at both ends.
    """
    set_at, rise_at = _horizon(moment, latitude, longitude, -1), _horizon(moment, latitude, longitude, 1)
    if not set_at or not rise_at:
        return sky
    q_set = moon_sky(set_at, latitude, longitude).parallactic
    q_rise = moon_sky(rise_at, latitude, longitude).parallactic
    f = (moment - set_at) / (rise_at - set_at)
    q = q_set + ((q_rise - q_set + 180) % 360 - 180) * f
    turn = q - sky.parallactic
    return replace(sky, limb=(sky.limb + turn) % 360, axis=(sky.axis + turn) % 360, parallactic=q)


def rise_set(day: date, zone: tzinfo, latitude: float, longitude: float) -> tuple[datetime | None, datetime | None]:
    """Moonrise and moonset on one local calendar day; either can be missing, as it is once a month."""

    def above(moment: datetime) -> float:
        return _above_horizon(moment, latitude, longitude)

    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    rise = sett = None
    step = timedelta(minutes=10)
    t, v = start, above(start)
    while t < end:
        t2 = min(end, t + step)
        v2 = above(t2)
        if (v < 0) != (v2 < 0):
            when = _crossing(above, t, t2)
            if v < 0 and rise is None:
                rise = when
            elif v >= 0 and sett is None:
                sett = when
        t, v = t2, v2
    return rise, sett


def next_phase(after: datetime, target: float, backwards: bool = False) -> datetime:
    """The next moment the elongation reaches target (0 new, 90, 180 full, 270)."""

    def offset(moment: datetime) -> float:
        return (elongation(moment) - target + 180) % 360 - 180

    step = timedelta(hours=-6 if backwards else 6)
    t, v = after, offset(after)
    for _ in range(130):
        t2 = t + step
        v2 = offset(t2)
        if abs(v2 - v) < 90 and ((v < 0 <= v2) if not backwards else (v2 < 0 <= v)):
            return _crossing(offset, *(sorted((t, t2))))
        t, v = t2, v2
    return after + timedelta(days=SYNODIC * ((target - elongation(after)) % 360) / 360)


PHASES = ("New Moon", "First Quarter", "Full Moon", "Last Quarter")


def phase_name(day: date, zone: tzinfo) -> str:
    """The principal phase if it happens on this local day; otherwise crescent or gibbous."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    a, b = elongation(start), elongation(start + timedelta(days=1))
    span = (b - a) % 360
    for i, name in enumerate(PHASES):
        if (i * 90 - a) % 360 < span:
            return name
    mid = (a + span / 2) % 360
    waxing = mid < 180
    shape = "Crescent" if mid < 90 or mid > 270 else "Gibbous"
    return f"{'Waxing' if waxing else 'Waning'} {shape}"


# ── the face ────────────────────────────────────────────────────────────
@cache
def _surface(level: int = 0) -> tuple[int, int, float, list[float]]:
    """(rows, cols, degrees per cell, brightness 0–1) of the bundled albedo map, halved `level` times.

    A small disc samples a coarser level, so each screen pixel averages the ground it covers
    instead of picking one crater at random.
    """
    if level:
        rows, cols, grid, px = _surface(level - 1)
        half_r, half_c = rows // 2, cols // 2
        out = [
            (
                px[2 * r * cols + 2 * c]
                + px[2 * r * cols + 2 * c + 1]
                + px[(2 * r + 1) * cols + 2 * c]
                + px[(2 * r + 1) * cols + 2 * c + 1]
            )
            / 4
            for r in range(half_r)
            for c in range(half_c)
        ]
        return half_r, half_c, grid * 2, out
    raw = zlib.decompress((Path(__file__).with_name("moon_map.bin")).read_bytes())
    if raw[:4] != b"PDMN":
        raise ValueError("not a pdwx moon map")
    rows, cols = struct.unpack("<HH", raw[4:8])
    return rows, cols, 180 / rows, [v / 255 for v in raw[8:]]


def _albedo(lat: float, lon: float, level: int = 0) -> float:
    """Surface brightness at a selenographic point, 1 for the bright highlands (bilinear)."""
    rows, cols, grid, px = _surface(level)
    span = cols * grid / 2
    u = (lon + span) / grid - 0.5
    v = (90 - lat) / grid - 0.5
    x0, y0 = math.floor(u), math.floor(v)
    fx, fy = u - x0, v - y0
    x0, x1 = max(0, min(cols - 1, x0)), max(0, min(cols - 1, x0 + 1))
    y0, y1 = max(0, min(rows - 1, y0)), max(0, min(rows - 1, y0 + 1))
    top = px[y0 * cols + x0] * (1 - fx) + px[y0 * cols + x1] * fx
    bottom = px[y1 * cols + x0] * (1 - fx) + px[y1 * cols + x1] * fx
    return top * (1 - fy) + bottom * fy


def _blend(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def face(sky: MoonSky, radius: float, lit: RGB, dark: RGB, aspect: float = 1.0) -> list[list[tuple[RGB, float] | None]]:
    """The disc as a grid of (colour, coverage) pixels, 2·radius + 2 wide; None off the disc.

    Radius is in pixel widths; aspect is a pixel's height over its width, so the grid has fewer,
    taller rows and the disc still comes out round.

    Each pixel is a point on the visible hemisphere. Its lighting comes from the Sun's direction in
    screen space (towards the bright limb, tipped by the phase angle), and its surface from turning
    it into the Moon's own latitude and longitude, so both the terminator and the maria sit where
    the observer sees them.
    """
    size = int(2 * radius) + 2
    tall = int(2 * radius / aspect) + 2
    centre, middle = size / 2, tall / 2
    # a coarser map level once a pixel spans more than one map cell (57.3° per radius at the centre)
    level = max(0, min(4, int(math.log2(max(1.0, 57.3 / radius / _surface()[2])))))
    # the Sun as seen from the Moon, in screen space: x right, y up, z towards the observer
    b, i = math.radians(sky.limb), math.radians(sky.phase_angle)
    sun = (math.sin(i) * math.sin(b), math.sin(i) * math.cos(b), math.cos(i))
    # turn screen space so the Moon's north is straight up, then onto the Moon's own axes
    a = math.radians(sky.axis)
    ca, sa = math.cos(a), math.sin(a)
    l0, b0 = math.radians(sky.lib_lon), math.radians(sky.lib_lat)
    facing = (math.cos(b0) * math.cos(l0), math.cos(b0) * math.sin(l0), math.sin(b0))
    north = (-math.sin(b0) * math.cos(l0), -math.sin(b0) * math.sin(l0), math.cos(b0))
    east = (-math.sin(l0), math.cos(l0), 0.0)
    earthshine = 0.06 * (1 - sky.lit) ** 2  # the Earth is nearly full when the Moon is new
    grid: list[list[tuple[RGB, float] | None]] = []
    for row in range(tall):
        line: list[tuple[RGB, float] | None] = []
        y = (middle - row - 0.5) * aspect / radius
        for col in range(size):
            x = (col + 0.5 - centre) / radius
            r = math.hypot(x, y)
            cover = max(0.0, min(1.0, (1 - r) * radius + 0.5))  # one-pixel anti-aliased edge
            if cover <= 0:
                line.append(None)
                continue
            if r > 0.999:
                x, y = x / r * 0.999, y / r * 0.999
            z = math.sqrt(max(0.0, 1 - x * x - y * y))
            mu0 = x * sun[0] + y * sun[1] + z * sun[2]
            # Moon-north-up screen coordinates, then the Moon's own frame
            xn, yn = x * ca - y * sa, x * sa + y * ca
            p = [xn * east[k] + yn * north[k] + z * facing[k] for k in range(3)]
            albedo = _albedo(
                math.degrees(math.asin(max(-1.0, min(1.0, p[2])))), math.degrees(math.atan2(p[1], p[0])), level
            )
            light = min(1.15, 2 * mu0 / (mu0 + z)) if mu0 > 0 else 0.0  # Lommel–Seeliger, 1 at full
            glow = albedo * max(light, earthshine)
            # highlands up to white, maria kept clearly darker
            line.append((_blend(dark, lit, min(1.0, 1.3 * glow) ** 1.4), cover))
        grid.append(line)
    return grid
