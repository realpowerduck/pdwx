"""Palette, icons and number formatting shared by every screen."""

from __future__ import annotations

import os
from datetime import date

from .term import RGB, Theme, mix, stops


class Palette:
    """Colours derived from the terminal's own theme, so the app matches whatever palette is in use."""

    def __init__(self, theme: Theme):
        bg, fg, a = theme.bg, theme.fg, theme.ansi
        self.bg, self.text = bg, fg
        self.muted = mix(fg, bg, 0.42)
        self.dim = mix(fg, bg, 0.6)
        self.rail = mix(fg, bg, 0.8)
        self.panel = mix(bg, fg, 0.06)
        self.select = mix(bg, fg, 0.13)
        self.red, self.green, self.yellow = a[1], a[2], a[3]
        self.blue, self.magenta, self.cyan = a[4], a[5], a[6]
        self.accent = a[4]
        self.title = a[3]
        self.rain = a[4]
        self.normal_band = mix(fg, bg, 0.55)
        # one theme colour every 6°: blue at freezing, green around 12°, yellow in the mid-20s, red in the high 30s
        self._temp = [
            (-12.0, mix(a[4], a[5], 0.5)),
            (0.0, a[4]),
            (6.0, mix(a[4], a[6], 0.5)),
            (12.0, a[2]),
            (18.0, mix(a[2], a[3], 0.5)),
            (24.0, a[3]),
            (30.0, mix(a[3], a[1], 0.5)),
            (36.0, a[1]),
            (42.0, mix(a[1], a[5], 0.6)),
        ]
        neutral = mix(bg, fg, 0.18)
        self._anom = [
            (-8.0, a[4]),
            (-3.0, mix(a[4], neutral, 0.45)),
            (0.0, neutral),
            (3.0, mix(a[1], neutral, 0.45)),
            (8.0, a[1]),
        ]
        self._anom_bg = [
            (-6.0, mix(bg, a[4], 0.85)),
            (-2.5, mix(bg, a[4], 0.4)),
            (0.0, mix(bg, fg, 0.05)),
            (2.5, mix(bg, a[1], 0.4)),
            (6.0, mix(bg, a[1], 0.85)),
        ]

    def temp(self, celsius: float) -> RGB:
        return stops(self._temp, celsius)

    def anomaly(self, delta: float) -> RGB:
        """Text colour for a departure from normal."""
        return stops(self._anom, delta)

    def anomaly_bg(self, delta: float, strength: float = 1.0) -> RGB:
        """Fill colour for a departure from normal (calendar cells, stripes, heatmap)."""
        full = stops(self._anom_bg, delta)
        return mix(self.bg, full, strength)

    def rain_bg(self, mm: float, top: float = 25.0) -> RGB:
        return (
            mix(self.bg, self.rain, min(1.0, 0.12 + 0.88 * (mm / top) ** 0.6))
            if mm >= 0.1
            else mix(self.bg, self.text, 0.04)
        )

    def wet_dry(self, delta_fraction: float) -> RGB:
        """Rainfall departure: yellow for dry years, blue for wet ones."""
        return stops(
            [
                (-0.5, mix(self.bg, self.yellow, 0.9)),
                (0.0, mix(self.bg, self.text, 0.08)),
                (0.5, mix(self.bg, self.blue, 0.9)),
            ],
            delta_fraction,
        )


# ── units and icons (set once from the user's settings) ─────────────────

UNITS = {"temperature": "C", "rain": "mm", "wind": "km/h"}
ICON_MODE = "symbols"


def configure(temperature: str = "C", rain: str = "mm", wind: str = "km/h", icons: str = "symbols") -> None:
    global ICON_MODE
    UNITS.update(temperature=temperature, rain=rain, wind=wind)
    ICON_MODE = os.environ.get("PDWX_ICONS", icons).lower()


# Nerd Font "Weather Icons" glyphs (nf-weather-*); single-width Unicode symbols; plain ASCII
_NERD = {
    "sun": "\ue30d",
    "sun_cloud": "\ue30c",
    "part": "\ue302",
    "cloud": "\ue312",
    "fog": "\ue313",
    "drizzle": "\ue31b",
    "rain": "\ue318",
    "showers": "\ue319",
    "snow": "\ue31a",
    "storm": "\ue31d",
    "sleet": "\ue3ad",
}
_SYMBOLS = {
    "sun": "☀",
    "sun_cloud": "☀",
    "part": "◑",
    "cloud": "☁",
    "fog": "≡",
    "drizzle": "∴",
    "rain": "☂",
    "showers": "☂",
    "snow": "❄",
    "storm": "ϟ",
    "sleet": "☂",
}
_ASCII = {
    "sun": "*",
    "sun_cloud": "*",
    "part": "~",
    "cloud": "=",
    "fog": "#",
    "drizzle": "'",
    "rain": ",",
    "showers": ";",
    "snow": "+",
    "storm": "!",
    "sleet": ",",
}
_KIND = {
    0: "sun",
    1: "sun_cloud",
    2: "part",
    3: "cloud",
    45: "fog",
    48: "fog",
    51: "drizzle",
    53: "drizzle",
    55: "drizzle",
    56: "sleet",
    57: "sleet",
    61: "rain",
    63: "rain",
    65: "rain",
    66: "sleet",
    67: "sleet",
    71: "snow",
    73: "snow",
    75: "snow",
    77: "snow",
    85: "snow",
    86: "snow",
    80: "showers",
    81: "showers",
    82: "showers",
    95: "storm",
    96: "storm",
    99: "storm",
}
ICON_SETS = {"nerd": _NERD, "symbols": _SYMBOLS, "ascii": _ASCII}


def icon(code: int | None, mode: str | None = None) -> str:
    if code is None or code not in _KIND:
        return " "
    return ICON_SETS.get(mode or ICON_MODE, _SYMBOLS)[_KIND[code]]


def icon_colour(pal: Palette, code: int | None) -> RGB:
    if code is None:
        return pal.dim
    if code <= 1:
        return pal.yellow
    if code == 2:
        return mix(pal.yellow, pal.muted, 0.5)
    if code in (3, 45, 48):
        return pal.muted
    if code >= 95:
        return pal.yellow
    if code in (71, 73, 75, 77, 85, 86):
        return pal.text
    return pal.rain


# ── formatting (values arrive metric: °C, mm, km/h, cm) ─────────────────


def temp(celsius: float) -> float:
    return celsius * 9 / 5 + 32 if UNITS["temperature"] == "F" else celsius


def temp_diff(celsius: float) -> float:
    return celsius * 9 / 5 if UNITS["temperature"] == "F" else celsius


def deg(value: float | None) -> str:
    return "—" if value is None else f"{round(temp(value))}°"


def deg1(value: float | None) -> str:
    return "—" if value is None else f"{temp(value):.1f}°"


def delta(value: float | None) -> str:
    """Signed, rounded temperature difference: +7°, −2°, ±0°."""
    if value is None:
        return ""
    r = round(temp_diff(value))
    return "±0°" if r == 0 else f"+{r}°" if r > 0 else f"−{-r}°"


def delta1(value: float) -> str:
    """Unsigned difference to one decimal: 1.4°."""
    return f"{abs(temp_diff(value)):.1f}°"


def rain_value(mm: float) -> float:
    return mm / 25.4 if UNITS["rain"] == "in" else mm


def rain(value: float | None) -> str:
    """One rule everywhere. mm: 0 under 0.1, one decimal under 10. Inches: two decimals under 10."""
    if value is None:
        return "—"
    if UNITS["rain"] == "in":
        inches = value / 25.4
        if inches < 0.005:
            return "0in"
        return f"{inches:.2f}in" if inches < 10 else f"{inches:.1f}in"
    if value < 0.1:
        return "0mm"
    return f"{value:.1f}mm" if value < 10 else f"{value:.0f}mm"


def wind(kmh: float | None) -> str:
    if kmh is None:
        return "—"
    unit = UNITS["wind"]
    factor = {"km/h": 1.0, "mph": 0.621371, "m/s": 1 / 3.6, "kn": 0.539957}[unit]
    return f"{round(kmh * factor)} {unit}"


def snow(cm: float) -> str:
    return f"{cm / 2.54:.1f} in" if UNITS["rain"] == "in" else f"{cm:.1f} cm"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def day_month(d: date) -> str:
    return f"{d.day} {d:%b}"


def full_date(d: date) -> str:
    return f"{d.day} {d:%b %Y}"
