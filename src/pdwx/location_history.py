"""Recently viewed locations for the pdwx picker."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .weather_data import Place

MAX_RECENT_LOCATIONS = 30


def _history_file() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state_home / "pdwx" / "locations.json"


def recent_locations() -> list[Place]:
    try:
        data = json.loads(_history_file().read_text())
        rows = data.get("locations", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []

    places: list[Place] = []
    seen: set[str] = set()
    for row in rows:
        try:
            place = Place(
                name=str(row["name"]),
                label=str(row["label"]),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                timezone=str(row.get("timezone") or "UTC"),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= place.latitude <= 90 and -180 <= place.longitude <= 180):
            continue
        if place.key not in seen:
            seen.add(place.key)
            places.append(place)
    return places[:MAX_RECENT_LOCATIONS]


def remember_location(place: Place) -> None:
    locations = [place] + [old for old in recent_locations() if old.key != place.key]
    _write_locations(locations)


def forget_location(place: Place) -> None:
    """Remove one place from recent history without touching its weather cache."""
    locations = [old for old in recent_locations() if old.key != place.key]
    _write_locations(locations)


def _write_locations(locations: list[Place]) -> None:
    payload = {
        "version": 1,
        "locations": [
            {
                "name": item.name,
                "label": item.label,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "timezone": item.timezone,
            }
            for item in locations[:MAX_RECENT_LOCATIONS]
        ],
    }
    path = _history_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


# ── saved dates (birthdays, anniversaries) ──────────────────────────────


def _dates_file() -> Path:
    return _history_file().with_name("dates.json")


def saved_dates() -> list[dict]:
    """[{'label': str, 'month': int, 'day': int, 'year': int | None}] in saved order."""
    try:
        data = json.loads(_dates_file().read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("dates", []) if isinstance(data, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            item = {
                "label": str(row["label"]),
                "month": int(row["month"]),
                "day": int(row["day"]),
                "year": int(row["year"]) if row.get("year") else None,
            }
        except (KeyError, TypeError, ValueError):
            continue
        out.append(item)
    return out


def save_dates(rows: list[dict]) -> None:
    path = _dates_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"version": 1, "dates": rows}, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
