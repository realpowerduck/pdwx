"""Global archive, geocoding, and short-range forecast access."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Callable

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
HISTORY_START_YEAR = 1940
USER_AGENT = "pdwx/0.2.0 (historical weather calendar)"

CORE_FIELDS = ("temperature_2m_max", "temperature_2m_min", "weather_code", "precipitation_sum")
EXTRA_FIELDS = (
    "apparent_temperature_max",
    "sunshine_duration",
    "wind_speed_10m_max",
    "relative_humidity_2m_mean",
    "snowfall_sum",
)
FORECAST_FIELDS = CORE_FIELDS + EXTRA_FIELDS + ("precipitation_probability_max",)

WMO = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm / hail",
    99: "Heavy thunderstorm / hail",
}

# Progress callback: (message, seconds_left_or_None)
Progress = Callable[[str, "float | None"], None]


class RateLimited(RuntimeError):
    """Open-Meteo's hourly or daily free quota is used up; waiting a minute will not help."""


@dataclass(frozen=True)
class Place:
    name: str
    label: str
    latitude: float
    longitude: float
    timezone: str = "UTC"

    @property
    def key(self) -> str:
        return f"{self.latitude:.3f}_{self.longitude:.3f}".replace("-", "m").replace(".", "p")

    @property
    def short(self) -> str:
        return self.label.split(",")[0].strip() or self.name


# ── cache ───────────────────────────────────────────────────────────────


def cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "pdwx"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)  # file names reveal which places you look at
    return path


def _read_cache(path: Path, max_age: timedelta | None = None) -> dict[str, Any] | None:
    try:
        if max_age is not None and datetime.now().timestamp() - path.stat().st_mtime > max_age.total_seconds():
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")))
    temp.replace(path)


# ── http ────────────────────────────────────────────────────────────────


def _url(base: str, params: dict[str, Any]) -> str:
    return base + "?" + urllib.parse.urlencode(params)


def _reason(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
        return str(body.get("reason") or err.reason)
    except Exception:
        return str(err.reason)


_TLS: ssl.SSLContext | None = None


def _tls() -> ssl.SSLContext:
    """Default TLS context; python.org's macOS build ships without CA certificates, so borrow the system's."""
    global _TLS
    if _TLS is None:
        _TLS = ssl.create_default_context()
        paths = ssl.get_default_verify_paths()
        if paths.cafile is None and paths.capath is None and os.path.exists("/etc/ssl/cert.pem"):
            _TLS.load_verify_locations("/etc/ssl/cert.pem")
    return _TLS


def _get_json(
    url: str,
    timeout: int = 25,
    progress: Progress | None = None,
    label: str = "Loading",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch JSON, waiting out Open-Meteo's per-minute limit and retrying brief network failures."""
    network_failures = 0
    minute_waits = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=_tls()) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise RuntimeError(f"Open-Meteo {err.code}: {_reason(err)}") from None
            reason = _reason(err)
            if "minute" not in reason.lower() or minute_waits >= 3:
                raise RateLimited(reason) from None
            minute_waits += 1
            deadline = time.monotonic() + 62
            while (left := deadline - time.monotonic()) > 0:
                if progress:
                    progress(f"{label} · Open-Meteo asks us to wait a minute", left)
                sleep(min(1.0, left))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                detail = getattr(reason, "verify_message", None) or reason
                raise RuntimeError(f"certificate check failed ({detail})") from None
            network_failures += 1
            if network_failures > 2:
                raise RuntimeError(f"Network error: {getattr(err, 'reason', err)}") from None
            if progress:
                progress(f"{label} · network hiccup, retrying", None)
            sleep(2)
    if not isinstance(data, dict) or data.get("error"):
        reason = data.get("reason", "unexpected response") if isinstance(data, dict) else "unexpected response"
        raise RuntimeError(str(reason))
    return data


# ── places ──────────────────────────────────────────────────────────────


def _place_label(row: dict[str, Any]) -> str:
    return ", ".join(str(row[k]) for k in ("name", "admin1", "country") if row.get(k))


def _place(row: dict[str, Any], fallback: str) -> Place:
    return Place(
        name=str(row.get("name", fallback)),
        label=_place_label(row),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        timezone=str(row.get("timezone") or "UTC"),
    )


# "Austin, TX" / "Perth, WA": state and province abbreviations (some are shared between countries)
_US = dict(
    al="alabama",
    ak="alaska",
    az="arizona",
    ar="arkansas",
    ca="california",
    co="colorado",
    ct="connecticut",
    de="delaware",
    fl="florida",
    ga="georgia",
    hi="hawaii",
    id="idaho",
    il="illinois",
    ia="iowa",
    ks="kansas",
    ky="kentucky",
    la="louisiana",
    me="maine",
    md="maryland",
    ma="massachusetts",
    mi="michigan",
    mn="minnesota",
    ms="mississippi",
    mo="missouri",
    mt="montana",
    ne="nebraska",
    nv="nevada",
    nh="new hampshire",
    nj="new jersey",
    nm="new mexico",
    ny="new york",
    nc="north carolina",
    nd="north dakota",
    oh="ohio",
    ok="oklahoma",
    pa="pennsylvania",
    ri="rhode island",
    sc="south carolina",
    sd="south dakota",
    tn="tennessee",
    tx="texas",
    ut="utah",
    vt="vermont",
    va="virginia",
    wv="west virginia",
    wi="wisconsin",
    wy="wyoming",
    dc="district of columbia",
)
_CA = dict(
    ab="alberta",
    bc="british columbia",
    mb="manitoba",
    nb="new brunswick",
    nl="newfoundland",
    ns="nova scotia",
    on="ontario",
    pe="prince edward island",
    qc="quebec",
    sk="saskatchewan",
    yt="yukon",
)
_AU = dict(
    nsw="new south wales",
    vic="victoria",
    qld="queensland",
    wa="western australia",
    sa="south australia",
    tas="tasmania",
    nt="northern territory",
    act="australian capital territory",
)
REGION_ALIASES: dict[str, tuple[str, ...]] = {}
for _table in (_US, _CA, _AU):
    for _abbr, _name in _table.items():
        REGION_ALIASES[_abbr] = REGION_ALIASES.get(_abbr, ()) + (_name,)


def search_places(query: str) -> list[Place]:
    query = query.strip()
    if len(query) < 2:
        return []
    data = _get_json(_url(GEOCODE_URL, {"name": query, "count": 10, "language": "en", "format": "json"}), 5)
    places: list[Place] = []
    seen: set[str] = set()
    for row in data.get("results") or []:
        place = _place(row, query)
        if place.key not in seen:
            seen.add(place.key)
            places.append(place)
    return places


def geocode(query: str) -> Place:
    query = query.strip()
    if not query:
        raise ValueError("Choose a location in the picker or pass --location.")
    coord = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", query)
    if coord:
        lat, lon = map(float, coord.groups())
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Coordinates are outside the valid latitude/longitude range.")
        return Place(query, query, lat, lon)
    data = _get_json(_url(GEOCODE_URL, {"name": query, "count": 10, "language": "en", "format": "json"}), 10)
    results = data.get("results") or []
    parts = [part.strip() for part in query.split(",") if part.strip()]
    if len(parts) > 1 and (
        not results or not any(r.get("name", "").casefold() == parts[0].casefold() for r in results)
    ):
        data = _get_json(_url(GEOCODE_URL, {"name": parts[0], "count": 10, "language": "en", "format": "json"}), 10)
        results = data.get("results") or []
    if not results:
        raise RuntimeError(f"No matching place found for {query!r}.")
    query_key = query.casefold()
    selected = next((r for r in results if _place_label(r).casefold() == query_key), None)
    if selected is None and parts:
        city = parts[0].casefold()
        candidates = [r for r in results if str(r.get("name", "")).casefold() == city]
        suffix = parts[1].casefold() if len(parts) > 1 else ""
        regions = REGION_ALIASES.get(suffix, (suffix,))
        selected = next(
            (r for r in candidates if suffix and any(g in str(r.get("admin1", "")).casefold() for g in regions)),
            None,
        )
        if selected is None:
            selected = candidates[0] if candidates else results[0]
    return _place(selected or results[0], query)


# ── weather ─────────────────────────────────────────────────────────────


def _archive_params(place: Place, start: date, end: date, fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "latitude": place.latitude,
        "longitude": place.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(fields),
        "models": "era5",
        "timezone": "auto",
    }


def merge_daily(*datasets: dict[str, Any] | None) -> dict[str, Any]:
    """Merge Open-Meteo daily blocks by date; later datasets fill in or override earlier ones."""
    rows: dict[str, dict[str, Any]] = {}
    fields: list[str] = []
    base: dict[str, Any] = {}
    for dataset in datasets:
        if not dataset:
            continue
        base = base or {k: v for k, v in dataset.items() if k != "daily"}
        daily = dataset.get("daily", {})
        for key, value in daily.items():
            if key != "time" and isinstance(value, list) and key not in fields:
                fields.append(key)
        for index, stamp in enumerate(daily.get("time", [])):
            row = rows.setdefault(stamp, {})
            for key in fields:
                seq = daily.get(key)
                if isinstance(seq, list) and index < len(seq) and seq[index] is not None:
                    row[key] = seq[index]
    result = dict(base)
    result["daily"] = {"time": sorted(rows)}
    for key in fields:
        result["daily"][key] = [rows[stamp].get(key) for stamp in result["daily"]["time"]]
    return result


def _archive_through(
    place: Place,
    name: str,
    fields: tuple[str, ...],
    last_year: int,
    refresh: bool,
    progress: Progress | None,
    label: str,
) -> dict[str, Any] | None:
    """Complete-year ERA5 archive, cached once and extended only by the missing years."""
    if last_year < HISTORY_START_YEAR:
        return None
    root = cache_root()
    path = root / f"{name}-{place.key}.json"
    cached = None if refresh else _read_cache(path)
    times = (cached or {}).get("daily", {}).get("time", [])
    have_until = date.fromisoformat(times[-1]) if times else None
    if have_until and have_until >= date(last_year, 12, 31):
        return cached
    start = have_until + timedelta(days=1) if have_until else date(HISTORY_START_YEAR, 1, 1)
    years = last_year - start.year + 1
    if progress:
        progress(f"{label} · {start.year}–{last_year} ({years} year{'s' if years != 1 else ''})", None)
    fresh = _get_json(
        _url(ARCHIVE_URL, _archive_params(place, start, date(last_year, 12, 31), fields)), 120, progress, label
    )
    merged = merge_daily(cached, fresh) if cached else fresh
    _write_cache(path, merged)
    return merged


def load_history(place: Place, today: date, refresh: bool = False, progress: Progress | None = None) -> dict[str, Any]:
    """Every complete year of ERA5 since 1940 (the heavy download)."""
    return _archive_through(place, "era5", CORE_FIELDS, today.year - 1, refresh, progress, "Downloading history") or {
        "daily": {"time": []}
    }


def load_current_year(place: Place, today: date, progress: Progress | None = None) -> dict[str, Any]:
    """This year's ERA5 so far (it trails real time by about five days)."""
    path = cache_root() / f"era5-year-{place.key}-{today.year}.json"
    cached = _read_cache(path, timedelta(hours=12))
    if cached:
        return cached
    end = today - timedelta(days=1)
    if end.year != today.year:
        return {"daily": {"time": []}}
    try:
        data = _get_json(
            _url(ARCHIVE_URL, _archive_params(place, date(today.year, 1, 1), end, CORE_FIELDS + EXTRA_FIELDS)),
            60,
            progress,
            "Loading this year",
        )
    except Exception:
        stale = _read_cache(path)
        if stale:
            return stale
        raise
    _write_cache(path, data)
    return data


def load_extras(place: Place, today: date, progress: Progress | None = None) -> dict[str, Any] | None:
    """Feels-like, sunshine, wind, humidity and snow since 1940; fetched after the core view opens."""
    return _archive_through(
        place, "era5x", EXTRA_FIELDS, today.year - 1, False, progress, "Downloading sun, wind and humidity"
    )


def cached_extras(place: Place) -> dict[str, Any] | None:
    return _read_cache(cache_root() / f"era5x-{place.key}.json")


def load_forecast(place: Place, refresh: bool = False, progress: Progress | None = None) -> dict[str, Any]:
    path = cache_root() / f"forecast-v3-{place.key}.json"
    if not refresh:
        cached = _read_cache(path, timedelta(hours=1))
        if cached:
            return cached
    params = {
        "latitude": place.latitude,
        "longitude": place.longitude,
        "daily": ",".join(FORECAST_FIELDS),
        "past_days": 92,  # also fills recent months when the archive is rate limited
        "forecast_days": 16,
        "timezone": "auto",
    }
    try:
        data = _get_json(_url(FORECAST_URL, params), 30, progress, "Loading forecast")
    except Exception:
        stale = _read_cache(path)
        if stale:
            stale["_stale"] = True
            return stale
        raise
    _write_cache(path, data)
    return data


ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"


def load_oni(refresh: bool = False) -> str | None:
    """NOAA's El Niño/La Niña index (monthly, since 1950); cached for a week, stale copy if offline."""
    path = cache_root() / "oni.txt"
    try:
        if not refresh and datetime.now().timestamp() - path.stat().st_mtime < 7 * 86400:
            return path.read_text()
    except OSError:
        pass
    try:
        req = urllib.request.Request(ONI_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20, context=_tls()) as response:
            text = response.read().decode("utf-8", "replace")
        if "SEAS" not in text:
            raise RuntimeError("unexpected ONI file")
        path.write_text(text)
        return text
    except Exception:
        try:
            return path.read_text()
        except OSError:
            return None


def has_history(place: Place) -> bool:
    return (cache_root() / f"era5-{place.key}.json").exists()


def local_today(place: Place, api_timezone: str | None = None) -> date:
    zone = api_timezone or place.timezone or "UTC"
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(zone)).date()
    except Exception:
        return datetime.now().date()


def condition(code: int | float | None) -> str:
    try:
        return WMO.get(int(code), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"
