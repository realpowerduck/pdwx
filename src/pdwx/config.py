"""User settings: units, icons, week start and home place, stored as TOML."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .weather_data import Place

TEMPERATURE = ("C", "F")
RAIN = ("mm", "in")
WIND = ("km/h", "mph", "m/s", "kn")
ICONS = ("symbols", "nerd", "ascii")
WEEK_START = ("monday", "sunday")
IMPERIAL_COUNTRIES = ("United States", "Liberia", "Myanmar")


@dataclass(frozen=True)
class Settings:
    temperature: str = "C"
    rain: str = "mm"
    wind: str = "km/h"
    icons: str = "symbols"
    week_start: str = "monday"
    home: Place | None = None

    def imperial(self) -> Settings:
        return replace(self, temperature="F", rain="in", wind="mph")


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pdwx" / "config.toml"


def _choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def load() -> Settings | None:
    """Saved settings, or None before the first run. Unknown or bad values fall back to defaults."""
    try:
        data = tomllib.loads(config_path().read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    d = Settings()
    home = None
    raw = data.get("home")
    if isinstance(raw, dict):
        try:
            home = Place(
                str(raw["name"]),
                str(raw.get("label") or raw["name"]),
                float(raw["latitude"]),
                float(raw["longitude"]),
                str(raw.get("timezone") or "UTC"),
            )
        except (KeyError, TypeError, ValueError):
            home = None
    return Settings(
        temperature=_choice(data.get("temperature"), TEMPERATURE, d.temperature),
        rain=_choice(data.get("rain"), RAIN, d.rain),
        wind=_choice(data.get("wind"), WIND, d.wind),
        icons=_choice(data.get("icons"), ICONS, d.icons),
        week_start=_choice(data.get("week_start"), WEEK_START, d.week_start),
        home=home,
    )


def save(settings: Settings) -> Path:
    q = json.dumps  # JSON strings are valid TOML basic strings
    lines = [
        "# pdwx settings. Edit here, or press , (comma) inside pdwx.",
        f"temperature = {q(settings.temperature)}   # C or F",
        f"rain = {q(settings.rain)}          # mm or in",
        f"wind = {q(settings.wind)}        # km/h, mph, m/s or kn",
        f"icons = {q(settings.icons)}     # symbols, nerd (needs a Nerd Font) or ascii",
        f"week_start = {q(settings.week_start)}  # monday or sunday",
    ]
    if settings.home:
        h = settings.home
        lines += [
            "",
            "[home]",
            f"name = {q(h.name)}",
            f"label = {q(h.label)}",
            f"latitude = {h.latitude}",
            f"longitude = {h.longitude}",
            f"timezone = {q(h.timezone)}",
        ]
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o600)  # the home place is personal
    temporary.replace(path)
    return path
