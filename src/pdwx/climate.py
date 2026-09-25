"""Pure climate calculations over the merged daily record for one place."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean
from typing import Any
from collections.abc import Iterable

BASE_YEARS = (1991, 2020)  # WMO standard normal period: "normal" everywhere in pdwx
WINDOW = 7  # normals average ±7 days so single-date noise is smoothed
DRY_MM = 1.0  # a dry day has under 1 mm
SLOTS = 366


def slot(d: date) -> int:
    """Day-of-year index in a leap year, so 29 Feb has its own slot."""
    return date(2000, d.month, d.day).timetuple().tm_yday - 1


@dataclass
class Day:
    date: date
    src: str  # H = ERA5 archive, R = recent model analysis, F = forecast
    hi: float | None = None
    lo: float | None = None
    rain: float | None = None
    code: int | None = None
    feels: float | None = None
    sun: float | None = None  # hours
    wind: float | None = None  # km/h
    humidity: float | None = None
    snow: float | None = None  # cm

    @property
    def ok(self) -> bool:
        return self.hi is not None and self.lo is not None

    @property
    def mid(self) -> float | None:
        return (self.hi + self.lo) / 2 if self.ok else None  # type: ignore[operator]


@dataclass
class Normal:
    hi: float
    lo: float
    rain: float

    @property
    def mid(self) -> float:
        return (self.hi + self.lo) / 2


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _rows(data: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    daily = (data or {}).get("daily", {})
    times = daily.get("time", [])
    for i, stamp in enumerate(times):
        row = {"date": date.fromisoformat(stamp)}
        for key, seq in daily.items():
            if key != "time" and isinstance(seq, list) and i < len(seq):
                row[key] = seq[i]
        yield row


def _apply(day: Day, row: dict[str, Any]) -> None:
    pairs = (
        ("hi", "temperature_2m_max"),
        ("lo", "temperature_2m_min"),
        ("rain", "precipitation_sum"),
        ("feels", "apparent_temperature_max"),
        ("wind", "wind_speed_10m_max"),
        ("humidity", "relative_humidity_2m_mean"),
        ("snow", "snowfall_sum"),
    )
    for attr, key in pairs:
        value = _num(row.get(key))
        if value is not None:
            setattr(day, attr, value)
    sun = _num(row.get("sunshine_duration"))
    if sun is not None:
        day.sun = sun / 3600
    code = _num(row.get("weather_code"))
    if code is not None:
        day.code = int(code)


class Climate:
    def __init__(
        self,
        history: dict[str, Any] | None,
        this_year: dict[str, Any] | None,
        forecast: dict[str, Any] | None,
        today: date,
        extras: dict[str, Any] | None = None,
        latitude: float = 0.0,
    ):
        self.today = today
        self.latitude = latitude
        self.days: dict[date, Day] = {}
        for source in (history, extras, this_year):
            for row in _rows(source):
                if row["date"] >= today:
                    continue
                day = self.days.setdefault(row["date"], Day(row["date"], "H"))
                _apply(day, row)
        for row in _rows(forecast):
            d = row["date"]
            existing = self.days.get(d)
            if d < today and existing and existing.ok:
                continue
            day = Day(d, "F" if d >= today else "R")
            _apply(day, row)
            if day.ok:
                self.days[d] = day
        for d in [d for d, day in self.days.items() if not day.ok]:
            del self.days[d]
        self.observed = sorted(d for d, day in self.days.items() if day.src != "F")
        self.first_year = self.observed[0].year if self.observed else today.year
        self.last_complete = today.year - 1
        self.by_slot: dict[int, list[Day]] = {}
        for d in self.observed:
            self.by_slot.setdefault(slot(d), []).append(self.days[d])
        self._build_normals()
        self._hot: float | None = None

    # ── lookups ────────────────────────────────────────────────────────
    def day(self, d: date) -> Day | None:
        return self.days.get(d)

    def normal(self, d: date) -> Normal | None:
        return self.normals[slot(d)]

    def same_date(self, d: date, include_year: bool = False) -> list[Day]:
        """Observed values for this calendar date in every year, newest first."""
        rows = [day for day in self.by_slot.get(slot(d), []) if include_year or day.date.year != d.year]
        return sorted(rows, key=lambda day: day.date.year, reverse=True)

    def _build_normals(self) -> None:
        sums = [[0.0, 0.0, 0.0, 0] for _ in range(SLOTS)]
        for d in self.observed:
            if BASE_YEARS[0] <= d.year <= BASE_YEARS[1]:
                day = self.days[d]
                cell = sums[slot(d)]
                cell[0] += day.hi  # type: ignore[operator]
                cell[1] += day.lo  # type: ignore[operator]
                cell[2] += day.rain or 0.0
                cell[3] += 1
        if not any(cell[3] for cell in sums):  # short record: use whatever exists
            for d in self.observed:
                day = self.days[d]
                cell = sums[slot(d)]
                cell[0] += day.hi  # type: ignore[operator]
                cell[1] += day.lo  # type: ignore[operator]
                cell[2] += day.rain or 0.0
                cell[3] += 1
        self.normals: list[Normal | None] = []
        for i in range(SLOTS):
            hi = lo = rain = 0.0
            n = 0
            for off in range(-WINDOW, WINDOW + 1):
                cell = sums[(i + off) % SLOTS]
                hi, lo, rain, n = hi + cell[0], lo + cell[1], rain + cell[2], n + cell[3]
            self.normals.append(Normal(hi / n, lo / n, rain / n) if n else None)

    # ── ranking and records ────────────────────────────────────────────
    def rank(self, d: date, value: float, attr: str = "hi") -> tuple[int, int, int]:
        """(warm rank, cool rank, total) of value among this date in every other year."""
        values = [getattr(day, attr) for day in self.same_date(d) if getattr(day, attr) is not None]
        warmer = sum(1 for v in values if v > value)
        cooler = sum(1 for v in values if v < value)
        return warmer + 1, cooler + 1, len(values) + 1

    def records(self, d: date) -> dict[str, Day | None]:
        rows = self.same_date(d)
        return {
            "hi": max(rows, key=lambda r: r.hi, default=None),  # type: ignore[arg-type,return-value]
            "lo": min(rows, key=lambda r: r.lo, default=None),  # type: ignore[arg-type,return-value]
            "warm_night": max(rows, key=lambda r: r.lo, default=None),  # type: ignore[arg-type,return-value]
            "cold_day": min(rows, key=lambda r: r.hi, default=None),  # type: ignore[arg-type,return-value]
            "wet": max((r for r in rows if r.rain is not None), key=lambda r: r.rain, default=None),  # type: ignore[arg-type,return-value]
        }

    def alerts(self) -> list[tuple[Day, str, Day]]:
        """Forecast days that would set or tie a record for their calendar date."""
        found = []
        for d in sorted(d for d, day in self.days.items() if day.src == "F"):
            day = self.days[d]
            rec = self.records(d)
            if rec["hi"] and day.hi >= rec["hi"].hi:  # type: ignore[operator]
                found.append((day, "hottest", rec["hi"]))
            elif rec["warm_night"] and day.lo >= rec["warm_night"].lo:  # type: ignore[operator]
                found.append((day, "warmest night", rec["warm_night"]))
            if rec["lo"] and day.lo <= rec["lo"].lo:  # type: ignore[operator]
                found.append((day, "coldest night", rec["lo"]))
            elif rec["cold_day"] and day.hi <= rec["cold_day"].hi:  # type: ignore[operator]
                found.append((day, "coldest day", rec["cold_day"]))
        return found

    def last_time(self, d: date, value: float, warm: bool, attr: str = "hi") -> Day | None:
        """Most recent earlier day within ±7 days of this date that was at least as extreme."""
        for year in range(d.year - 1, self.first_year - 1, -1):
            try:
                centre = d.replace(year=year)
            except ValueError:
                centre = date(year, 2, 28)
            best = None
            for off in range(-WINDOW, WINDOW + 1):
                day = self.days.get(centre + timedelta(days=off))
                if day is None or day.src == "F":
                    continue
                v = getattr(day, attr)
                beyond = v is not None and (v >= value if warm else v <= value)
                if beyond and (best is None or (getattr(best, attr) < v if warm else getattr(best, attr) > v)):
                    best = day
            if best:
                return best
        return None

    def similar(self, target: Day, count: int = 5) -> list[Day]:
        """Earlier days near this time of year whose high, low and rain best match."""
        scored = []
        for year in range(self.first_year, target.date.year):
            try:
                centre = target.date.replace(year=year)
            except ValueError:
                centre = date(year, 2, 28)
            for off in range(-10, 11):
                day = self.days.get(centre + timedelta(days=off))
                if day is None or day.src == "F":
                    continue
                score = abs(day.hi - target.hi) + abs(day.lo - target.lo)  # type: ignore[operator]
                score += 0.3 * abs((day.rain or 0) - (target.rain or 0))
                scored.append((score, day))
        scored.sort(key=lambda item: item[0])
        picked: list[Day] = []
        for _, day in scored:
            if all(day.date.year != p.date.year for p in picked):
                picked.append(day)
            if len(picked) == count:
                break
        return picked

    # ── long-term views ────────────────────────────────────────────────
    def _mean_over(self, days: list[date]) -> float | None:
        values = [self.days[d].mid for d in days]
        return mean(values) if values else None  # type: ignore[arg-type]

    def annual(self) -> list[tuple[int, float, float]]:
        """(year, mean temperature, total rain) for complete years."""
        by_year: dict[int, list[Day]] = {}
        for d in self.observed:
            by_year.setdefault(d.year, []).append(self.days[d])
        out = []
        for year, rows in sorted(by_year.items()):
            if year <= self.last_complete and len(rows) >= 360:
                out.append((year, mean(r.mid for r in rows), sum(r.rain or 0 for r in rows)))  # type: ignore[misc]
        return out

    def monthly(self, month: int) -> list[tuple[int, float, float]]:
        """(year, mean temperature, rain total) for this month in every year it is fully observed."""
        by_year: dict[int, list[Day]] = {}
        for d in self.observed:
            if d.month == month:
                by_year.setdefault(d.year, []).append(self.days[d])
        out = []
        for year, rows in sorted(by_year.items()):
            if len(rows) >= 27:
                out.append((year, mean(r.mid for r in rows), sum(r.rain or 0 for r in rows)))  # type: ignore[misc]
        return out

    @staticmethod
    def baseline(series: list[tuple[int, float, float]]) -> tuple[float, float]:
        base = [s for s in series if BASE_YEARS[0] <= s[0] <= BASE_YEARS[1]] or series
        return (mean(s[1] for s in base), mean(s[2] for s in base)) if base else (0.0, 0.0)

    @staticmethod
    def decade_means(series: list[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
        groups: dict[int, list[tuple[int, float, float]]] = {}
        for row in series:
            groups.setdefault(row[0] // 10 * 10, []).append(row)
        return [
            (decade, mean(r[1] for r in rows), mean(r[2] for r in rows))
            for decade, rows in sorted(groups.items())
            if len(rows) >= 5
        ]

    @staticmethod
    def change(series: list[tuple[int, float, float]]) -> tuple[str, float] | None:
        """Last ten years against the 1950s (or the earliest ten years available)."""
        if len(series) < 25:
            return None
        early = [s for s in series if 1951 <= s[0] <= 1960] or series[:10]
        late = series[-10:]
        label = "the 1950s" if early[0][0] >= 1951 and early[-1][0] <= 1960 else f"{early[0][0]}–{early[-1][0]}"
        return label, mean(s[1] for s in late) - mean(s[1] for s in early)

    def hot_threshold(self) -> float:
        """A 'hot day' is hotter than 95% of days in the normal period."""
        if self._hot is None:
            highs = sorted(
                self.days[d].hi
                for d in self.observed  # type: ignore[type-var]
                if BASE_YEARS[0] <= d.year <= BASE_YEARS[1]
            ) or sorted(self.days[d].hi for d in self.observed)  # type: ignore[type-var]
            self._hot = highs[int(len(highs) * 0.95)] if highs else 30.0  # type: ignore[assignment]
        return self._hot  # type: ignore[return-value]

    def hot_days_by_decade(self, threshold: float) -> list[tuple[int, float]]:
        counts: dict[int, int] = {}
        years: dict[int, set[int]] = {}
        for d in self.observed:
            if d.year > self.last_complete:
                continue
            decade = d.year // 10 * 10
            years.setdefault(decade, set()).add(d.year)
            if self.days[d].hi >= threshold:  # type: ignore[operator]
                counts[decade] = counts.get(decade, 0) + 1
        return [(dec, counts.get(dec, 0) / len(ys)) for dec, ys in sorted(years.items()) if len(ys) >= 5]

    def season_start(self, year: int) -> date:
        return date(year, 7, 1) if self.latitude < 0 else date(year, 1, 1)

    def first_hot_by_decade(self, threshold: float) -> list[tuple[int, float]]:
        """Average days after the season start (1 Jul south, 1 Jan north) of the first hot day."""
        found: dict[int, list[int]] = {}
        for year in range(self.first_year, self.today.year + 1):
            start = self.season_start(year)
            end = self.season_start(year + 1) if self.latitude < 0 else date(year, 12, 31)
            if end >= self.today:
                continue
            d = start
            while d < end:
                day = self.days.get(d)
                if day and day.hi >= threshold:  # type: ignore[operator]
                    found.setdefault(year // 10 * 10, []).append((d - start).days)
                    break
                d += timedelta(days=1)
        return [(dec, mean(v)) for dec, v in sorted(found.items()) if len(v) >= 5]

    def dry_spells(self) -> dict[str, tuple[date, date, int] | None]:
        """Longest dry run ever, this year, and the one still going."""
        best = this_year = None
        run_start = prev = None
        for d in self.observed:
            rain = self.days[d].rain
            dry = rain is not None and rain < DRY_MM
            if dry and prev is not None and d - prev == timedelta(days=1) and run_start is not None:
                pass
            elif dry:
                run_start = d
            else:
                run_start = None
            if dry and run_start is not None:
                length = (d - run_start).days + 1
                if best is None or length > best[2]:
                    best = (run_start, d, length)
                if d.year == self.today.year and (this_year is None or length > this_year[2]):
                    this_year = (
                        max(run_start, date(d.year, 1, 1)),
                        d,
                        (d - max(run_start, date(d.year, 1, 1))).days + 1,
                    )
            prev = d
        current = None
        if self.observed and run_start is not None:
            last = self.observed[-1]
            current = (run_start, last, (last - run_start).days + 1)
        return {"ever": best, "year": this_year, "current": current}

    def year_anomalies(self, year: int) -> list[float | None]:
        """Daily mean-temperature departure from normal for every slot of a year."""
        out: list[float | None] = [None] * SLOTS
        start = date(year, 1, 1)
        for offset in range(366):
            d = start + timedelta(days=offset)
            if d.year != year:
                break
            day = self.days.get(d)
            normal = self.normals[slot(d)]
            if day and normal:
                out[slot(d)] = day.mid - normal.mid  # type: ignore[operator]
        return out
