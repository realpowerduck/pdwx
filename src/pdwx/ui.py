"""The weather views: week, month, chart, climate, heatmap and their drill-downs."""

from __future__ import annotations

import calendar
import contextlib
import math
import queue
import re
import textwrap
import threading
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from .climate import BASE_YEARS, Climate, Day, Normal
from .style import UNITS, Palette, day_month, deg, deg1, delta, full_date, icon, icon_colour, ordinal, rain_value, temp
from .style import rain as rain_text
from .views import Views
from .term import RGB, Canvas, clip, ink_for, text_width
from .weather_data import RateLimited, cached_extras, load_current_year, load_extras, load_forecast

if TYPE_CHECKING:
    from .app import App, Loaded

STATUS_SECONDS = 10
VIEWS = ("week", "month", "chart", "climate", "heatmap")
LABELS = {
    "week": "Week",
    "month": "Month",
    "chart": "Chart",
    "climate": "Climate",
    "heatmap": "Heatmap",
}
SOURCE = {"H": "archive", "R": "recent model", "F": "forecast"}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_date(text: str, today: date) -> tuple[date, bool] | None:
    """Loose date entry. Returns (date, year_given)."""
    text = text.strip().lower().replace(",", " ")
    if text in ("today", "now", ""):
        return today, False
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return _safe(y, mo, d, True)
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{4}))?", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return _safe(int(m.group(3)) if m.group(3) else today.year, mo, d, bool(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if m:
        return _safe(today.year, int(m.group(1)), int(m.group(2)), False)
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]{3})[a-z]*(?:\s+(\d{4}))?", text) or re.fullmatch(
        r"([a-z]{3})[a-z]*\s+(\d{1,2})(?:\s+(\d{4}))?", text
    )
    if m:
        a, b, y = m.groups()
        d, mon = (a, b) if a.isdigit() else (b, a)
        if mon[:3] in MONTHS:
            return _safe(int(y) if y else today.year, MONTHS[mon[:3]], int(d), bool(y))
    return None


def _safe(y: int, m: int, d: int, given: bool) -> tuple[date, bool] | None:
    try:
        return date(y, m, d), given
    except ValueError:
        return None


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return date(d.year + years, 2, 28)


def rank_phrase(clim: Climate, d: date, high: float, dated: bool = True) -> str:
    """How this high ranks against the same calendar date in every other year."""
    warm, cool, total = clim.rank(d, high)
    if total < 5:
        return ""
    label = f" {day_month(d)}" if dated else ""
    if warm == 1:
        return f"warmest{label} in {total} years"
    if cool == 1:
        return f"coolest{label} in {total} years"
    if warm <= 10:
        return f"{ordinal(warm)} warmest{label} in {total} years"
    if cool <= 10:
        return f"{ordinal(cool)} coolest{label} in {total} years"
    share = round(100 * (cool - 1) / (total - 1))
    return f"warmer than {share}% of years" + (f" on{label}" if dated else "")


def rank_detail(clim: Climate, d: date, high: float) -> tuple[str, str]:
    """(long, short) note naming the years that beat a top-4 day, or the year it beat."""
    warm, cool, total = clim.rank(d, high)
    if total < 5 or min(warm, cool) > 4:
        return "", ""
    hot = warm <= cool
    rows = sorted((r for r in clim.same_date(d) if r.hi is not None), key=lambda r: r.hi, reverse=hot)
    if min(warm, cool) == 1:
        if not rows:
            return "", ""
        best = rows[0]
        verb = "tying" if deg1(best.hi) == deg1(high) else "beating"
        return f"{verb} {deg1(best.hi)} in {best.date.year}", f"{verb} {best.date.year}"
    beaten = rows[: min(warm, cool) - 1]
    names = [str(r.date.year) for r in beaten]
    listed = [f"{r.date.year} ({deg1(r.hi)})" for r in beaten]
    join = lambda xs: xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]  # noqa: E731
    word = "warmer" if hot else "cooler"
    return f"only {join(listed)} {'was' if len(beaten) == 1 else 'were'} {word}", f"behind {join(names)}"


class WeatherUI(Views):
    def __init__(self, app: App, loaded: Loaded, view: str | None = None):
        self.app = app
        self.pal: Palette = app.pal
        self.loaded = loaded
        self.place = loaded.place
        self.today = loaded.today
        self.extras = cached_extras(self.place)
        self.clim = self._build(loaded, self.extras)
        self.sel = self.today
        self.view = view if view in VIEWS else "week"
        self.metric = "temp"
        self.drill: str | None = None  # "years" or "detail"
        self.detail: date | None = None
        self.week_top = self.today
        self.years_index = 0
        self.years_sort = False
        self.chart_scroll = 0
        self.heat_year = self.today.year
        self.compare: tuple[Any, Climate] | None = None
        self.help = False
        self.help_scroll = 0
        self.jobs: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.status = " · ".join(loaded.notes)
        self.status_colour: RGB | None = self.pal.yellow if loaded.notes else None
        self._extras_started = False

    @property
    def status(self) -> str:
        """Footer message; fades after a few seconds so it never lingers in the corner."""
        return self._status if time.monotonic() - self._status_at < STATUS_SECONDS else ""

    @status.setter
    def status(self, value: str) -> None:
        self._status, self._status_at = value, time.monotonic()

    # ── data plumbing ──────────────────────────────────────────────────
    def _build(self, loaded: Loaded, extras: dict[str, Any] | None) -> Climate:
        return Climate(loaded.history, loaded.this_year, loaded.forecast, loaded.today, extras, loaded.place.latitude)

    def _extras_complete(self) -> bool:
        times = (self.extras or {}).get("daily", {}).get("time", [])
        return bool(times) and times[-1] >= f"{self.today.year - 1}-12-31"

    def _want_extras(self, day: Day | None) -> None:
        """Sun, wind and humidity history is a second large download; fetch it only when a past day needs it."""
        if day is not None and day.src == "H" and day.feels is None and not self._extras_started:
            self._start_extras()

    def _start_extras(self) -> None:
        self._extras_started = True
        if self._extras_complete():
            return
        self.status = "Fetching feels-like, sun, wind and humidity in the background"
        self.status_colour = self.pal.dim

        def progress(message: str, wait: float | None) -> None:
            self.jobs.put(("status", message + (f" · {int(wait)}s" if wait is not None else "")))

        def work() -> None:
            try:
                self.jobs.put(("extras", load_extras(self.place, self.today, progress)))
            except RateLimited:
                self.jobs.put(("status", "Extra details paused: Open-Meteo's free limit is used up for now"))
            except Exception as exc:
                self.jobs.put(("status", f"Extra details unavailable ({exc})"))

        threading.Thread(target=work, daemon=True).start()

    def _refresh(self) -> None:
        self.status, self.status_colour = "Refreshing forecast…", self.pal.accent

        def work() -> None:
            try:
                forecast = load_forecast(self.place, refresh=True)
                this_year = load_current_year(self.place, self.today)
                self.jobs.put(("refreshed", (forecast, this_year)))
            except Exception as exc:
                self.jobs.put(("status", f"Refresh failed: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        while not self.jobs.empty():
            kind, value = self.jobs.get()
            if kind == "status":
                self.status, self.status_colour = (
                    value,
                    self.pal.yellow
                    if "unavailable" in value or "paused" in value or "failed" in value
                    else self.pal.dim,
                )
            elif kind == "extras" and value:
                self.extras = value
                self.clim = self._build(self.loaded, self.extras)
                self.status, self.status_colour = "Sun, wind and humidity loaded", self.pal.dim
            elif kind == "refreshed":
                self.loaded.forecast, self.loaded.this_year = value
                self.clim = self._build(self.loaded, self.extras)
                self.status = "Forecast refreshed" + (" (offline copy)" if value[0].get("_stale") else "")
                self.status_colour = self.pal.dim

    # ── main loop ──────────────────────────────────────────────────────
    def run(self) -> str:
        while True:
            self._drain()
            c = self.app.canvas()
            if not self.app.too_small(c):
                self.draw(c)
            self.app.show(c)
            key = self.app.term.read_key(0.2)
            if key is None:
                continue
            result = self.handle(key)
            if result:
                return result

    def draw(self, c: Canvas) -> None:
        self._header(c)
        top = 2
        if self.drill or self.view in ("week", "month", "chart"):
            d = self.detail if self.drill == "detail" and self.detail else self.sel
            top = (self._headline_compact if c.compact else self._headline)(c, 2, d)
        bottom = c.h - 2
        if self.drill == "years":
            self._draw_years(c, top, bottom)
        elif self.drill == "detail":
            self._draw_detail(c, top, bottom)
        else:
            getattr(self, f"_draw_{self.view}")(c, top, bottom)
        self.app.footer(c, self._hints(c.compact), self.status, self.status_colour)
        if self.help:
            self._draw_help(c)

    # ── chrome ─────────────────────────────────────────────────────────
    def _header(self, c: Canvas) -> None:
        pal = self.pal
        if c.compact:
            # place on the left; the view's name and a dot per view on the right
            dx = c.w - 2 * len(VIEWS)
            for i, v in enumerate(VIEWS):
                c.put(0, dx + 2 * i, "●" if v == self.view else "○", pal.accent if v == self.view else pal.dim)
            label = LABELS[self.view]
            lx = dx - 2 - len(label)
            c.put(0, lx, label, pal.accent, bold=True)
            x = self.app.brand(c) + 2
            head = self.place.label.partition(",")[0]
            x = c.put(0, x, head, pal.text, bold=True, width=max(0, lx - x - 2))
            if self.compare:
                x = c.put(0, x, " vs ", pal.dim, width=max(0, lx - x - 2))
                c.put(0, x, self.compare[0].short, pal.magenta, bold=True, width=max(0, lx - x - 2))
            return
        tabs = [LABELS[v] for v in VIEWS]
        tabs_w = sum(len(t) + 3 for t in tabs)
        show_tabs = c.w >= tabs_w + 30
        x = self.app.brand(c)
        head, _, rest = self.place.label.partition(",")
        limit = (c.w - tabs_w - 2) if show_tabs else c.w - 2
        x = c.put(0, x + 2, head, pal.text, bold=True, width=max(0, limit - x - 2))
        if self.compare:
            x = c.put(0, x, "  vs ", pal.dim, width=max(0, limit - x))
            x = c.put(0, x, self.compare[0].short, pal.magenta, bold=True, width=max(0, limit - x))
        elif rest:
            c.put(0, x, "," + rest, pal.muted, width=max(0, limit - x))
        if show_tabs:
            tx = c.w - tabs_w
            for v in VIEWS:
                label = f" {LABELS[v]} "
                active = v == self.view
                c.put(0, tx, label, pal.accent if active else pal.dim, pal.select if active else None, bold=active)
                tx += len(label) + 1
        else:
            c.put(0, c.w - len(LABELS[self.view]) - 2, LABELS[self.view], pal.accent, bold=True)

    def _parts(
        self, c: Canvas, y: int, x: int, parts: list[tuple[str, RGB | None, bool]], limit: int | None = None
    ) -> int:
        limit = c.w - 1 if limit is None else limit
        for text, colour, bold in parts:
            if x >= limit:
                break
            x = c.put(y, x, text, colour, bold=bold, width=limit - x)
        return x

    def _fit(
        self, c: Canvas, y: int, x: int, groups: list[list[tuple[str, RGB | None, bool]]], limit: int | None = None
    ) -> int:
        """Draw whole groups of parts left to right, stopping before the first that would be cut."""
        limit = c.w - 1 if limit is None else limit
        for group in groups:
            if x + sum(text_width(t) for t, _, _ in group) > limit:
                break
            x = self._parts(c, y, x, group, limit)
        return x

    def _wrap(self, c: Canvas, y: int, x: int, text: str, colour: RGB | None, bottom: int) -> int:
        """Word-wrapped text from x to the right edge; returns the row after it."""
        for line in textwrap.wrap(text, max(10, c.w - x - 2)):
            if y >= bottom:
                break
            c.put(y, x, line, colour)
            y += 1
        return y

    def _headline(self, c: Canvas, y: int, d: date) -> int:
        pal, clim = self.pal, self.clim
        day, normal = clim.day(d), clim.normal(d)
        when = "Today" if d == self.today else "Tomorrow" if d == self.today + timedelta(days=1) else f"{d:%A}"
        parts: list[tuple[str, RGB | None, bool]] = []
        if day:
            parts += [(icon(day.code) + "  ", icon_colour(pal, day.code), False)]
        parts += [(f"{when} {day_month(d)}" + (f" {d.year}" if d.year != self.today.year else ""), pal.text, True)]
        if day:
            parts += [
                ("   ", None, False),
                (deg(day.hi), pal.temp(day.hi), True),
                (" / ", pal.dim, False),  # type: ignore[arg-type]
                (deg(day.lo), pal.temp(day.lo), False),
            ]  # type: ignore[arg-type]
            if normal:
                dv = day.hi - normal.hi  # type: ignore[operator]
                word = "above" if dv >= 0.5 else "below" if dv <= -0.5 else "near"
                parts += [
                    ("   ", None, False),
                    (delta(dv), pal.anomaly(dv), True),
                    (f" {word} normal" if word != "near" else " normal", pal.muted, False),
                ]
            rank = self._rank_text(d, day)
            parts += [("   ", None, False), (rank, pal.title, False)]
            used = sum(text_width(t) for t, _, _ in parts) + 1
            for note in rank_detail(clim, d, day.hi) if rank else ():  # type: ignore[arg-type]
                if note and used + text_width(f" · {note}") <= c.w - 2:
                    parts += [(" · ", pal.dim, False), (note, pal.muted, False)]
                    break
        elif normal:
            parts += [
                ("   normally ", pal.muted, False),
                (deg(normal.hi), pal.temp(normal.hi), True),
                (" / ", pal.dim, False),
                (deg(normal.lo), pal.temp(normal.lo), False),
            ]
        self._parts(c, y, 1, parts)
        # context line
        ctx: list[tuple[str, RGB | None, bool]] = []
        if normal:
            ctx += [("Normal ", pal.dim, False), (f"{deg(normal.hi)}/{deg(normal.lo)}", pal.muted, False)]
        rec = clim.records(d)
        if rec["hi"]:
            ctx += [
                ("   Record ", pal.dim, False),
                (deg1(rec["hi"].hi), pal.temp(rec["hi"].hi), False),  # type: ignore[arg-type]
                (f" {rec['hi'].date.year}", pal.muted, False),
            ]
        if day and normal and abs(day.hi - normal.hi) >= 3:  # type: ignore[operator]
            warm = day.hi >= normal.hi  # type: ignore[operator]
            last = clim.last_time(d, day.hi, warm)  # type: ignore[arg-type]
            word = "warm" if warm else "cool"
            if last:
                ctx += [
                    (f"   Last this {word} near {day_month(d)}: ", pal.dim, False),
                    (f"{full_date(last.date)} ({deg1(last.hi)})", pal.muted, False),
                ]
            else:
                ctx += [(f"   Never this {word} near {day_month(d)} since {clim.first_year}", pal.title, False)]
        if day and day.src != "H":
            ctx += [(f"   {SOURCE[day.src]}", pal.green if day.src == "F" else pal.dim, False)]
        elif not day and d > self.today:
            ctx += [("   beyond the 16-day forecast", pal.dim, False)]
        if self.compare:
            other = self.compare[1].day(d)
            if other:
                ctx += [
                    (f"   {self.compare[0].short} ", pal.magenta, False),
                    (f"{deg(other.hi)}/{deg(other.lo)}", pal.muted, False),
                ]
        self._parts(c, y + 1, 1, ctx)
        alerts = clim.alerts()
        if alerts:
            day_a, kind, rec_day = alerts[0]
            attr = "hi" if kind in ("hottest", "coldest day") else "lo"
            value, old = getattr(day_a, attr), getattr(rec_day, attr)
            verb = "would tie" if round(value, 1) == round(old, 1) else "would beat"
            colour = pal.red if kind in ("hottest", "warmest night") else pal.blue
            text = (
                f"▲ Record watch · {day_a.date:%a} {day_month(day_a.date)}: forecast {deg(value)} {verb} "
                f"the {kind} {day_month(day_a.date)} on record ({deg1(old)}, {rec_day.date.year})"
            )
            if len(alerts) > 1:
                text += f"  +{len(alerts) - 1} more"
            c.put(y + 2, 1, clip(text, c.w - 2), colour, bold=True)
        return y + 4

    def _headline_compact(self, c: Canvas, y: int, d: date) -> int:
        """The headline stacked on three lines: the day and its temperatures, how unusual, the context."""
        pal, clim = self.pal, self.clim
        day, normal = clim.day(d), clim.normal(d)
        when = "Today" if d == self.today else "Tomorrow" if d == self.today + timedelta(days=1) else f"{d:%A}"
        x = 1
        if day:
            x = c.put(y, x, icon(day.code) + "  ", icon_colour(pal, day.code))
        title = f"{when} {day_month(d)}" + (f" {d.year}" if d.year != self.today.year else "")
        c.put(y, x, title, pal.text, bold=True)
        if day:
            temps = [
                (deg(day.hi), pal.temp(day.hi), True),
                (" / ", pal.dim, False),
                (deg(day.lo), pal.temp(day.lo), False),
            ]
        elif normal:
            temps = [("normally ", pal.dim, False), (f"{deg(normal.hi)} / {deg(normal.lo)}", pal.muted, False)]
        else:
            temps = []
        self._parts(c, y, c.w - 2 - sum(text_width(t) for t, _, _ in temps), temps)  # type: ignore[arg-type]
        # how unusual
        line: list[list[tuple[str, RGB | None, bool]]] = []
        if day and normal:
            dv = day.hi - normal.hi  # type: ignore[operator]
            word = " above normal" if dv >= 0.5 else " below normal" if dv <= -0.5 else " normal"
            line.append([(delta(dv), pal.anomaly(dv), True), (word, pal.muted, False)])
        rank = rank_phrase(clim, d, day.hi, dated=False) if day else ""  # type: ignore[arg-type]
        if rank:
            line.append([(" · " if line else "", pal.dim, False), (rank, pal.title, False)])
            used = x + sum(text_width(t) for g in line for t, _, _ in g)
            for note in rank_detail(clim, d, day.hi):  # type: ignore[arg-type,union-attr]
                if note and used + text_width(f" · {note}") <= c.w - 2:
                    line.append([(" · ", pal.dim, False), (note, pal.muted, False)])
                    break
        self._fit(c, y + 1, x, line)
        # context, whole phrases only
        ctx: list[list[tuple[str, RGB | None, bool]]] = []
        if normal:
            ctx.append([("Normal ", pal.dim, False), (f"{deg(normal.hi)}/{deg(normal.lo)}", pal.muted, False)])
        rec = clim.records(d)
        if rec["hi"]:
            ctx.append(
                [
                    ("Record ", pal.dim, False),
                    (deg1(rec["hi"].hi), pal.temp(rec["hi"].hi), False),  # type: ignore[arg-type]
                    (f" {rec['hi'].date.year}", pal.muted, False),
                ]
            )
        if day and day.src != "H":
            ctx.append([(SOURCE[day.src], pal.green if day.src == "F" else pal.dim, False)])
        elif not day and d > self.today:
            ctx.append([("beyond the forecast", pal.dim, False)])
        if self.compare and (other := self.compare[1].day(d)):
            ctx.append(
                [
                    (f"{self.compare[0].short} ", pal.magenta, False),
                    (f"{deg(other.hi)}/{deg(other.lo)}", pal.muted, False),
                ]
            )
        if day and normal and abs(day.hi - normal.hi) >= 3:  # type: ignore[operator]
            warm = day.hi >= normal.hi  # type: ignore[operator]
            last = clim.last_time(d, day.hi, warm)  # type: ignore[arg-type]
            word = "warm" if warm else "cool"
            if last:
                ctx.append([(f"last this {word} ", pal.dim, False), (full_date(last.date), pal.muted, False)])
            else:
                ctx.append([(f"never this {word} since {clim.first_year}", pal.title, False)])
        joined = [g if i == 0 else [(" · ", pal.dim, False), *g] for i, g in enumerate(ctx)]
        self._fit(c, y + 2, x, joined)
        alerts = clim.alerts()
        if not alerts:
            return y + 4
        day_a, kind, rec_day = alerts[0]
        attr = "hi" if kind in ("hottest", "coldest day") else "lo"
        value, old = getattr(day_a, attr), getattr(rec_day, attr)
        verb = "ties" if round(value, 1) == round(old, 1) else "beats"
        colour = pal.red if kind in ("hottest", "warmest night") else pal.blue
        when_a = f"{day_a.date:%a} {day_month(day_a.date)}"
        text = f"▲ {when_a} {deg(value)} {verb} the {kind} ({deg1(old)}, {rec_day.date.year})"
        if len(alerts) > 1:
            text += f" +{len(alerts) - 1}"
        c.put(y + 3, x, clip(text, c.w - x - 1), colour, bold=True)
        return y + 5

    def _rank_text(self, d: date, day: Day) -> str:
        return rank_phrase(self.clim, d, day.hi)  # type: ignore[arg-type]

    def _hints(self, compact: bool = False) -> list[tuple[str, str]]:
        if self.help:
            return [("↑↓", "scroll"), ("?", "close help")] if compact else [("?", "close help")]
        if compact:
            short = {
                "years": [("↑↓", "year"), ("Enter", "that day"), ("Esc", "back")],
                "detail": [("↑↓", "year"), ("←→", "day"), ("Esc", "back")],
                "week": [("↑↓", "day"), ("Enter", "history")],
                "month": [("←→↑↓", "day"), ("Enter", "history")],
                "chart": [("↑↓", "day"), ("Enter", "history")],
                "climate": [("←→", "month"), ("Tab", "view")],
                "heatmap": [("↑↓", "year"), ("Enter", "open")],
            }
            return short[self.drill or self.view] + [("?", "help")]
        if self.drill == "years":
            return [
                ("↑↓", "year"),
                ("←→", "date"),
                ("Enter", "that day"),
                ("s", "sort"),
                ("r", "rain/temp"),
                ("Esc", "back"),
                ("?", "help"),
            ]
        if self.drill == "detail":
            return [("↑↓", "year"), ("←→", "day"), ("Esc", "back"), ("?", "help")]
        common = [("Tab", "view"), (",", "settings"), ("?", "help"), ("q", "quit")]
        per = {
            "week": [("↑↓", "day"), ("←→", "week"), ("Enter", "history"), ("r", "rain/temp"), ("c", "compare")],
            "month": [("←→↑↓", "day"), ("PgUp/Dn", "month"), ("Enter", "history"), ("r", "rain/temp")],
            "chart": [("↑↓", "day"), ("PgUp/Dn", "month"), ("Enter", "history"), ("r", "rain/temp")],
            "climate": [("←→", "month")],
            "heatmap": [("↑↓", "year"), ("←→", "month"), ("Enter", "open month")],
        }
        return per[self.view] + common

    # ── shared drawing ─────────────────────────────────────────────────
    def _axis(self, lows: list[float], highs: list[float]) -> tuple[float, float]:
        values = lows + highs
        if not values:
            return 0.0, 10.0
        lo = math.floor(min(values) / 5) * 5
        hi = math.ceil(max(values) / 5) * 5
        return (lo, hi) if hi > lo else (lo, lo + 5)

    def _ticks(self, c: Canvas, y: int, x: int, width: int, lo: float, hi: float, kind: str = "temp") -> None:
        """Axis labels at round numbers in the user's units; lo/hi arrive metric."""
        conv = temp if kind == "temp" else rain_value
        dlo, dhi = conv(lo), conv(hi)
        span = dhi - dlo
        if span <= 0:
            return
        suffix = "°" if kind == "temp" else UNITS["rain"]
        steps = (
            (1, 2, 5, 10, 20, 50, 100) if kind == "temp" or UNITS["rain"] == "mm" else (0.05, 0.1, 0.2, 0.5, 1, 2, 5)
        )
        step = next((s for s in steps if width / (span / s) >= 9), steps[-1])
        first = math.ceil(dlo / step - 1e-9)
        k = first
        while k * step <= dhi + 1e-9:
            t = k * step
            pos = x + round((t - dlo) / span * (width - 1))
            label = f"{round(t, 2):g}{suffix}"
            c.put(y, max(x, min(x + width - len(label), pos - len(label) // 2)), label, self.pal.dim)
            k += 1

    def _rail(
        self,
        c: Canvas,
        y: int,
        x: int,
        width: int,
        axis: tuple[float, float],
        lo: float | None,
        hi: float | None,
        normal: Normal | None,
        bg: RGB | None,
    ) -> None:
        pal = self.pal
        a_lo, a_hi = axis
        span = a_hi - a_lo

        def pos(t: float) -> int:
            return x + round((max(a_lo, min(a_hi, t)) - a_lo) / span * (width - 1))

        c.put(y, x, "─" * width, pal.rail, bg)
        if normal:
            p, q = pos(normal.lo), pos(normal.hi)
            c.put(y, p, "━" * (q - p + 1), pal.normal_band, bg)
        if lo is None or hi is None:
            return
        p, q = pos(lo), pos(hi)
        fills = []
        for col in range(p, q + 1):
            t = a_lo + (col - x) / max(1, width - 1) * span
            fills.append(pal.temp(t))
            c.put(y, col, " ", None, fills[-1])
        lo_s, hi_s = deg(lo), deg(hi)
        n = q - p + 1
        if n >= len(lo_s) + len(hi_s) + 1:
            for i, ch in enumerate(lo_s):
                c.put(y, p + i, ch, ink_for(fills[i]), fills[i], bold=True)
            for i, ch in enumerate(hi_s):
                k = n - len(hi_s) + i
                c.put(y, p + k, ch, ink_for(fills[k]), fills[k], bold=True)
        else:
            if n == 1:
                c.put(y, p, "●", pal.temp(hi), bg)
            if p - len(lo_s) - 1 >= x:
                c.put(y, p - len(lo_s) - 1, lo_s, pal.temp(lo), bg)
            if q + 2 + len(hi_s) <= x + width:
                c.put(y, q + 2, hi_s, pal.temp(hi), bg, bold=True)

    def _rain_bar(
        self,
        c: Canvas,
        y: int,
        x: int,
        width: int,
        top: float,
        value: float | None,
        normal: Normal | None,
        bg: RGB | None,
    ) -> None:
        pal = self.pal
        c.put(y, x, "─" * width, pal.rail, bg)
        if normal:
            npos = x + min(width - 1, round(normal.rain / top * (width - 1)))
            c.put(y, npos, "┃", pal.normal_band, bg)
        if value is None:
            return
        n = min(width, round(value / top * (width - 1)) + (1 if value >= 0.1 else 0))
        if n:
            c.put(y, x, "━" * n, pal.rain, bg, bold=True)
        label = rain_text(value)
        lx = x + n + 1
        if value >= 0.1 and lx + len(label) <= x + width:
            c.put(y, lx, label, pal.rain if value >= 0.1 else pal.dim, bg)

    def _rain_top(self, values: list[float]) -> float:
        peak = max(values, default=0.0)
        for top in (5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500):
            if peak <= top * 0.95:
                return float(top)
        return math.ceil(peak / 100) * 100.0

    @staticmethod
    def _layout(c: Canvas, note_w: int = 0) -> tuple[int, int, bool]:
        """Rail start, rail width, and whether the note column fits; shared by ticks and rows."""
        show_note = bool(note_w) and c.w >= 100
        if c.compact:  # short label, no source tag, badge and rain only
            return 10, max(10, c.w - 10 - 12 - 1), False
        rx = 16
        right = 14 + (note_w + 2 if show_note else 0)
        return rx, max(10, c.w - rx - right - 1), show_note

    def _row(
        self,
        c: Canvas,
        y: int,
        label: str,
        day: Day | None,
        normal: Normal | None,
        axis: tuple[float, float],
        rain_top: float,
        selected: bool,
        sub: bool = False,
        label_colour: RGB | None = None,
        note: str = "",
        note_colour: RGB | None = None,
        note_w: int = 0,
    ) -> None:
        """One date as: label · tag · icon · rail · badge · rain · note."""
        pal = self.pal
        bg = pal.select if selected else None
        if selected:
            c.fill(y, 0, c.w, pal.select)
            c.put(y, 0, "▌", pal.accent, pal.select)
        rx, width, show_note = self._layout(c, note_w)
        lw = rx - 4 if c.compact else 10  # compact: a 5-wide label, then the icon
        colour = label_colour or (pal.text if day else pal.dim)
        c.put(y, 2, label, colour, bg, bold=selected and not sub, width=lw - 1)
        if day and day.src != "H" and not c.compact:
            c.put(y, 2 + lw - 1, day.src, pal.green if day.src == "F" else pal.dim, bg)
        c.put(
            y,
            rx - 2 if c.compact else 2 + lw + 1,
            icon(day.code) if day else " ",
            icon_colour(pal, day.code if day else None),
            bg,
        )
        if self.metric == "temp":
            self._rail(c, y, rx, width, axis, day.lo if day else None, day.hi if day else None, normal, bg)
            bx = rx + width + (1 if c.compact else 2)
            if day and normal:
                dv = day.hi - normal.hi  # type: ignore[operator]
                c.put(y, bx, delta(dv).rjust(4), pal.anomaly(dv), bg, bold=abs(dv) >= 3)
            elif normal:
                c.put(y, bx, "norm", pal.dim, bg)
            wet = day is not None and (day.rain or 0) >= 0.1
            if day and (wet or not c.compact):  # compact: dry days leave the rain column empty
                c.put(y, bx + (5 if c.compact else 6), rain_text(day.rain).rjust(6), pal.rain if wet else pal.dim, bg)
        else:
            self._rain_bar(c, y, rx, width, rain_top, day.rain if day else None, normal, bg)
            bx = rx + width + (1 if c.compact else 2)
            if normal:
                c.put(y, bx, "avg", pal.dim, bg)
                c.put(y, bx + 4, rain_text(normal.rain).rjust(6), pal.dim, bg)
        if note and show_note:
            c.put(y, rx + width + 16, clip(note, note_w), note_colour or pal.dim, bg)

    # ── help ───────────────────────────────────────────────────────────
    def _draw_help(self, c: Canvas) -> None:
        pal = self.pal
        lines = [
            ("Views", ""),
            ("Tab / Shift+Tab", "next / previous view (or 1–5)"),
            ("Enter", "this date across every year · again for that day"),
            ("Esc", "back · from a view, choose another place"),
            ("", ""),
            ("Moving", ""),
            ("↑ ↓ ← →", "move the selected day (see the footer for each view)"),
            ("PgUp / PgDn", "previous / next month"),
            ("g or Home", "back to today"),
            (":", "go to any date, e.g. 14 Mar 1961"),
            ("", ""),
            ("Options", ""),
            ("r", "switch temperature / rain"),
            ("c  ·  x", "compare with another place  ·  stop comparing"),
            ("F5", "refresh the forecast"),
            (",", "settings: units, icons, week start, home place"),
            ("q", "quit"),
            ("", ""),
            ("Reading it", ""),
            ("━ grey band", f"normal range ({BASE_YEARS[0]}–{BASE_YEARS[1]} average, ±7 days)"),
            ("colour", "the temperature itself"),
            ("+3° badges", "the day's high against the normal high"),
            ("H R F", "ERA5 archive · recent model analysis · forecast"),
            ("", "ERA5 is a ~25 km modelled grid, not a weather station"),
            ("", ""),
            ("Data", ""),
            ("Open-Meteo", "weather data by Open-Meteo.com (CC BY 4.0)"),
            ("Copernicus", "contains modified Copernicus Climate Change Service information"),
        ]
        w = c.w - 2 if c.compact else min(c.w - 4, 78)  # compact: full width, nothing peeks out beside it
        kx, dx = (4, 21) if c.compact else (4, 22)
        rows: list[tuple[str, str]] = []  # descriptions wrapped to the panel
        for key, desc in lines:
            if key and not desc:
                rows.append((key, ""))
                continue
            wrapped = textwrap.wrap(desc, w - dx - 2) or [""]
            rows += [(key, wrapped[0])] + [("", more) for more in wrapped[1:]]
        h = min(c.h - 2, len(rows) + 4)
        x0, y0 = (c.w - w) // 2, max(1, (c.h - h) // 2)
        visible = h - 3
        self.help_scroll = max(0, min(self.help_scroll, len(rows) - visible))
        for r in range(h):
            c.fill(y0 + r, x0, w, pal.panel)
        c.put(y0 + 1, x0 + 2, "pdwx keys", pal.title, pal.panel, bold=True)
        if len(rows) > visible:
            more = f"{self.help_scroll + visible}/{len(rows)} ↑↓"
            c.put(y0 + 1, x0 + w - 2 - len(more), more, pal.dim, pal.panel)
        y = y0 + 2
        for key, desc in rows[self.help_scroll : self.help_scroll + visible]:
            if key and not desc:
                c.put(y, x0 + 2, key, pal.accent, pal.panel, bold=True)
            else:
                c.put(y, x0 + kx, key, pal.text, pal.panel, bold=True, width=dx - kx - 1)
                c.put(y, x0 + dx, desc, pal.muted, pal.panel, width=w - dx - 2)
            y += 1

    # ── keys ───────────────────────────────────────────────────────────
    def _move(self, days: int) -> None:
        self.sel += timedelta(days=days)

    def _move_month(self, months: int) -> None:
        ordinal_m = self.sel.year * 12 + self.sel.month - 1 + months
        y, m = divmod(ordinal_m, 12)
        m += 1
        self.sel = date(y, m, min(self.sel.day, calendar.monthrange(y, m)[1]))

    def _backdrop(self, c: Canvas) -> None:
        if not self.app.too_small(c):
            self.draw(c)

    def handle(self, key: str) -> str | None:
        if key in ("q", "ctrl-c"):
            return "quit"
        if key == ",":
            from .app import apply_settings

            new = self.app.settings(self.app.config)
            if new:
                self.app.config, self.app.home = new, new.home
                apply_settings(new)
                self.status, self.status_colour = "Settings saved", self.pal.dim
            return None
        if key in ("?", "f1"):
            self.help, self.help_scroll = not self.help, 0
            return None
        if self.help:
            if key == "esc":
                self.help = False
            elif key in ("up", "k", "down", "j", "pgup", "pgdn"):
                step = {"up": -1, "k": -1, "down": 1, "j": 1, "pgup": -10, "pgdn": 10}[key]
                self.help_scroll = max(0, self.help_scroll + step)
            return None
        if key == "esc":
            if self.drill == "detail":
                self.drill = "years"
            elif self.drill == "years":
                self.drill = None
            else:
                return "picker"
            return None
        if key in ("f5", "ctrl-r", "R"):
            self._refresh()
            return None
        if key == "r":
            self.metric = "rain" if self.metric == "temp" else "temp"
            return None
        if key in ("g", "home"):
            self.sel, self.drill, self.detail = self.today, None, None
            self.heat_year = self.today.year
            return None
        if key == ":":
            answer = self.app.prompt("Go to date:", self._backdrop)
            if answer:
                parsed = parse_date(answer, self.today)
                if parsed:
                    self.sel = parsed[0]
                    self.heat_year = self.sel.year
                    if self.drill:
                        self.drill, self.detail = None, None
                else:
                    self.status, self.status_colour = f"Couldn't read {answer!r} as a date", self.pal.yellow
            return None
        if key == "c":
            self._pick_compare()
            return None
        if key == "x" and self.compare:
            self.compare = None
            return None
        if self.drill == "years":
            return self._keys_years(key)
        if self.drill == "detail":
            return self._keys_detail(key)
        if key in ("tab", "btab"):
            i = VIEWS.index(self.view)
            self.view = VIEWS[(i + (1 if key == "tab" else -1)) % len(VIEWS)]
            return None
        if key in "12345" and len(key) == 1:
            self.view = VIEWS[int(key) - 1]
            return None
        return getattr(self, f"_keys_{self.view}")(key)

    def _enter_years(self) -> None:
        self.drill = "years"
        rows = self._years_rows()
        self.years_index = next((i for i, r in enumerate(rows) if r.date.year == self.sel.year), 0)

    def _keys_week(self, key: str) -> None:
        moves = {"up": -1, "k": -1, "down": 1, "j": 1, "left": -7, "h": -7, "right": 7, "l": 7, "pgup": -7, "pgdn": 7}
        if key in moves:
            self._move(moves[key])
            if key in ("left", "right", "h", "l", "pgup", "pgdn"):
                self.week_top += timedelta(days=moves[key])
        elif key == "enter":
            self._enter_years()

    def _keys_month(self, key: str) -> None:
        moves = {"left": -1, "h": -1, "right": 1, "l": 1, "up": -7, "k": -7, "down": 7, "j": 7}
        if key in moves:
            self._move(moves[key])
        elif key == "pgup":
            self._move_month(-1)
        elif key == "pgdn":
            self._move_month(1)
        elif key == "enter":
            self._enter_years()

    def _keys_chart(self, key: str) -> None:
        moves = {"up": -1, "k": -1, "down": 1, "j": 1, "left": -1, "h": -1, "right": 1, "l": 1}
        if key in moves:
            self._move(moves[key])
        elif key == "pgup":
            self._move_month(-1)
        elif key == "pgdn":
            self._move_month(1)
        elif key == "enter":
            self._enter_years()

    def _keys_climate(self, key: str) -> None:
        if key in ("left", "h", "pgup"):
            self._move_month(-1)
        elif key in ("right", "l", "pgdn"):
            self._move_month(1)

    def _keys_heatmap(self, key: str) -> None:
        if key in ("up", "k"):
            self.heat_year -= 1
        elif key in ("down", "j"):
            self.heat_year += 1
        elif key in ("pgup",):
            self.heat_year -= 10
        elif key in ("pgdn",):
            self.heat_year += 10
        elif key in ("left", "h"):
            self._move_month(-1)
        elif key in ("right", "l"):
            self._move_month(1)
        elif key == "enter":
            year = max(self.clim.first_year, min(self.today.year, self.heat_year))
            self.sel = date(year, self.sel.month, min(self.sel.day, calendar.monthrange(year, self.sel.month)[1]))
            self.view = "chart"

    def _keys_years(self, key: str) -> None:
        rows = self._years_rows()
        if key in ("up", "k"):
            self.years_index = max(0, self.years_index - 1)
        elif key in ("down", "j"):
            self.years_index = min(len(rows) - 1, self.years_index + 1)
        elif key == "pgup":
            self.years_index = max(0, self.years_index - 15)
        elif key == "pgdn":
            self.years_index = min(len(rows) - 1, self.years_index + 15)
        elif key in ("left", "h", "right", "l"):
            year = rows[self.years_index].date.year if rows else self.sel.year
            self._move(-1 if key in ("left", "h") else 1)
            rows = self._years_rows()
            self.years_index = next((i for i, r in enumerate(rows) if r.date.year == year), 0)
        elif key == "s":
            current = rows[self.years_index] if rows else None
            self.years_sort = not self.years_sort
            rows = self._years_rows()
            self.years_index = rows.index(current) if current in rows else 0
        elif key == "enter" and rows:
            self.detail = rows[self.years_index].date
            self.drill = "detail"

    def _keys_detail(self, key: str) -> None:
        if not self.detail:
            return
        if key in ("up", "k"):
            self.detail = _shift_year(self.detail, 1)
        elif key in ("down", "j"):
            self.detail = _shift_year(self.detail, -1)
        elif key in ("left", "h"):
            self.detail -= timedelta(days=1)
        elif key in ("right", "l"):
            self.detail += timedelta(days=1)
        self.detail = max(date(self.clim.first_year, 1, 1), min(self.detail, max(self.clim.days)))
        with contextlib.suppress(ValueError):  # 29 Feb has no match in a common year
            self.sel = self.sel.replace(month=self.detail.month, day=self.detail.day)
        rows = self._years_rows()
        self.years_index = next((i for i, r in enumerate(rows) if r.date == self.detail), self.years_index)

    # ── actions ────────────────────────────────────────────────────────
    def _pick_compare(self) -> None:
        other = self.app.pick("Compare with…", exclude=self.place)
        if not other:
            return
        loaded = self.app.load(other)
        if not loaded:
            return
        clim = Climate(
            loaded.history, loaded.this_year, loaded.forecast, self.today, cached_extras(other), other.latitude
        )
        self.compare = (other, clim)
        self.view = "week" if self.view not in ("week",) else self.view
        self.status, self.status_colour = f"Comparing with {other.short} · x to stop", self.pal.dim
