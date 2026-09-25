"""Terminal session: place picker, loading screen and the hand-off to the weather views."""

from __future__ import annotations

import contextlib

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import date
from typing import Any
from collections.abc import Callable

from . import config, style
from .config import Settings
from .location_history import forget_location, recent_locations, remember_location
from .style import Palette
from .term import Canvas, Terminal, clip, text_width
from .weather_data import (
    Place,
    RateLimited,
    has_history,
    load_current_year,
    load_forecast,
    load_history,
    local_today,
    search_places,
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MIN_H, MIN_W = 20, 60


@dataclass
class Loaded:
    place: Place
    today: date
    history: dict[str, Any]
    this_year: dict[str, Any]
    forecast: dict[str, Any]
    notes: list[str]


class App:
    def __init__(self, term: Terminal, pal: Palette, refresh_history: bool = False):
        self.home: Place | None = None
        self.config = Settings()
        self.term = term
        self.pal = pal
        self.refresh_history = refresh_history
        self._last_frame = ""
        self._last_size = (0, 0)

    # ── frame plumbing ─────────────────────────────────────────────────
    def canvas(self) -> Canvas:
        h, w = self.term.size()
        if (h, w) != self._last_size or self.term.resized:
            self.term.resized = False
            self._last_size = (h, w)
            self._last_frame = ""
            self.term.write("\x1b[2J")
        return Canvas(h, w)

    def show(self, canvas: Canvas) -> None:
        frame = canvas.render()
        if frame != self._last_frame:
            self.term.write(frame)
            self._last_frame = frame

    def too_small(self, c: Canvas) -> bool:
        if c.h >= MIN_H and c.w >= MIN_W:
            return False
        c.put(c.h // 2, max(0, (c.w - 34) // 2), f"Make the window bigger ({MIN_W}×{MIN_H})", self.pal.muted)
        return True

    def brand(self, c: Canvas, subtitle: str = "") -> int:
        x = c.put(0, 1, "◉ ", self.pal.accent)
        x = c.put(0, x, "pdwx", self.pal.text, bold=True)
        if subtitle:
            x = c.put(0, x + 2, subtitle, self.pal.muted)
        return x

    def footer(self, c: Canvas, hints: list[tuple[str, str]], status: str = "", status_colour: Any = None) -> None:
        y = c.h - 1
        x = 1
        right = c.w - 2 - (text_width(status) + 2 if status else 0)
        for key, label in hints:
            need = text_width(key) + 1 + text_width(label) + 3
            if x + need > right:
                break
            x = c.put(y, x, key, self.pal.text, bold=True)
            x = c.put(y, x + 1, label, self.pal.dim) + 3
        if status:
            c.put(y, c.w - 1 - text_width(status), clip(status, c.w // 2), status_colour or self.pal.muted)

    # ── line prompt ────────────────────────────────────────────────────
    def prompt(self, question: str, backdrop: Callable[[Canvas], None], initial: str = "") -> str | None:
        text, cursor = initial, len(initial)
        while True:
            c = self.canvas()
            backdrop(c)
            y = c.h - 2
            c.fill(y, 0, c.w, self.pal.panel)
            c.fill(y + 1, 0, c.w, self.pal.panel)
            x = c.put(y, 1, question + " ", self.pal.title, self.pal.panel, bold=True)
            room = c.w - x - 2
            start = max(0, cursor - room + 1)
            shown = text[start : start + room]
            c.put(y, x, shown, self.pal.text, self.pal.panel)
            cx = x + text_width(text[start:cursor])
            under = text[cursor] if cursor < len(text) else " "
            c.put(y, cx, under, self.pal.bg, self.pal.text)
            c.put(y + 1, 1, "Enter", self.pal.text, self.pal.panel, bold=True)
            c.put(y + 1, 7, "confirm   ", self.pal.dim, self.pal.panel)
            c.put(y + 1, 17, "Esc", self.pal.text, self.pal.panel, bold=True)
            c.put(y + 1, 21, "cancel", self.pal.dim, self.pal.panel)
            self.show(c)
            key = self.term.read_key(0.2)
            if key is None:
                continue
            if key in ("esc", "ctrl-c"):
                return None
            if key == "enter":
                return text.strip()
            if key == "backspace" and cursor:
                text, cursor = text[: cursor - 1] + text[cursor:], cursor - 1
            elif key == "delete" and cursor < len(text):
                text = text[:cursor] + text[cursor + 1 :]
            elif key == "left":
                cursor = max(0, cursor - 1)
            elif key == "right":
                cursor = min(len(text), cursor + 1)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(text)
            elif key == "ctrl-u":
                text, cursor = "", 0
            elif len(key) == 1 and key.isprintable():
                text, cursor = text[:cursor] + key + text[cursor:], cursor + 1

    # ── place picker ───────────────────────────────────────────────────
    def pick(self, title: str = "Choose a place", exclude: Place | None = None) -> Place | None:
        pal = self.pal
        recent = [p for p in recent_locations() if not exclude or p.key != exclude.key]
        query, cursor, selected = "", 0, 0
        results: list[Place] | None = None
        searching = False
        pending_at: float | None = None
        found_q: queue.Queue[tuple[str, list[Place] | str]] = queue.Queue()
        message = ""
        frame = 0

        def start_search(value: str) -> None:
            def work() -> None:
                try:
                    found_q.put((value, search_places(value)))
                except Exception as exc:
                    found_q.put((value, str(exc) or type(exc).__name__))

            threading.Thread(target=work, daemon=True).start()

        while True:
            now = time.monotonic()
            if pending_at is not None and now >= pending_at:
                pending_at, searching = None, True
                start_search(query)
            while not found_q.empty():
                value, found = found_q.get()
                if value != query:
                    continue
                searching = False
                if isinstance(found, str):
                    message = f"Search failed: {found} · saved places still work"
                    results = []
                else:
                    results = [p for p in found if not exclude or p.key != exclude.key]
                    message = f"{len(results)} match{'es' if len(results) != 1 else ''}" if results else "No matches"
            key_q = query.strip().casefold()
            saved = [p for p in recent if not key_q or key_q in p.label.casefold()]
            entries: list[tuple[Place, bool]] = [(p, True) for p in saved]
            if len(key_q) >= 2 and results:
                keys = {p.key for p in saved}
                entries += [(p, False) for p in results if p.key not in keys]
            selected = max(0, min(selected, len(entries) - 1))

            c = self.canvas()
            if not self.too_small(c):
                self.brand(c, title)
                # search box
                box_y = 2
                c.fill(box_y, 1, c.w - 2, pal.panel)
                x = c.put(box_y, 2, "\U000f0349 " if _nerd() else "/ ", pal.accent, pal.panel)
                if query:
                    c.put(box_y, x, query, pal.text, pal.panel, bold=True, width=c.w - x - 6)
                else:
                    c.put(box_y, x + 1, "Search any town, city or lat,lon", pal.dim, pal.panel)
                cx = x + text_width(query[:cursor])
                c.put(box_y, cx, query[cursor] if cursor < len(query) else " ", pal.bg, pal.text)
                if searching:
                    c.put(box_y, c.w - 4, SPINNER[frame % len(SPINNER)], pal.accent, pal.panel)
                # list
                y = 4
                if not entries:
                    hint = (
                        "Type a place name to search"
                        if not recent and not query
                        else "Keep typing…"
                        if len(key_q) < 2
                        else "Searching…"
                        if searching
                        else ""
                    )
                    c.put(y + 1, 3, hint, pal.muted)
                visible = c.h - y - 3
                top = max(0, min(selected - visible + 1, len(entries) - visible))
                last_group = None
                row = y
                for index in range(top, len(entries)):
                    place, is_saved = entries[index]
                    group = "Saved places" if is_saved else "Search results"
                    if group != last_group:
                        if row >= c.h - 3:
                            break
                        c.put(row, 3, group.upper(), pal.dim, bold=True)
                        row += 1
                        last_group = group
                    if row >= c.h - 3:
                        break
                    chosen = index == selected
                    bg = pal.select if chosen else None
                    if chosen:
                        c.fill(row, 1, c.w - 2, pal.select)
                        c.put(row, 1, "▌", pal.accent, pal.select)
                    head, _, rest = place.label.partition(",")
                    x = c.put(row, 3, "● " if is_saved else "  ", pal.accent if is_saved else pal.dim, bg)
                    x = c.put(row, x, head, pal.text, bg, bold=chosen)
                    x = c.put(row, x, ("," + rest) if rest else "", pal.muted, bg, width=c.w - x - 22)
                    coords = f"{place.latitude:.2f}, {place.longitude:.2f}"
                    c.put(row, c.w - 3 - len(coords), coords, pal.dim, bg)
                    row += 1
                if message and len(key_q) >= 2:
                    c.put(c.h - 2, 3, message, pal.dim, width=c.w - 6)
                hints = [
                    ("↑↓", "choose"),
                    ("Enter", "open"),
                    ("Del", "forget saved"),
                    ("Esc", "clear" if query else "back" if exclude else "quit"),
                ]
                self.footer(c, hints)
            self.show(c)
            frame += 1
            key = self.term.read_key(0.08)
            if key is None:
                continue
            if key == "ctrl-c":
                return None
            if key == "esc":
                if query:
                    query, cursor, results, pending_at, searching = "", 0, None, None, False
                    continue
                return None
            if key == "enter" and entries:
                return entries[selected][0]
            if key == "up":
                selected = max(0, selected - 1)
            elif key == "down":
                selected = min(len(entries) - 1, selected + 1)
            elif key == "pgup":
                selected = max(0, selected - 10)
            elif key == "pgdn":
                selected = min(len(entries) - 1, selected + 10)
            elif key == "delete" and entries:
                place, is_saved = entries[selected]
                if is_saved:
                    try:
                        forget_location(place)
                        recent = [p for p in recent if p.key != place.key]
                        message = f"Forgot {place.short}"
                    except OSError:
                        message = f"Could not forget {place.short}"
            elif key in ("left", "right", "home", "end", "backspace", "ctrl-u", "ctrl-w") or (
                len(key) == 1 and key.isprintable()
            ):
                old = query
                if key == "left":
                    cursor = max(0, cursor - 1)
                elif key == "right":
                    cursor = min(len(query), cursor + 1)
                elif key == "home":
                    cursor = 0
                elif key == "end":
                    cursor = len(query)
                elif key == "backspace" and cursor:
                    query, cursor = query[: cursor - 1] + query[cursor:], cursor - 1
                elif key in ("ctrl-u", "ctrl-w"):
                    query, cursor = "", 0
                elif len(key) == 1:
                    query, cursor = query[:cursor] + key + query[cursor:], cursor + 1
                if query != old:
                    selected, results, searching = 0, None, False
                    pending_at = time.monotonic() + 0.25 if len(query.strip()) >= 2 else None

    # ── settings ───────────────────────────────────────────────────────
    def settings(self, current: Settings, first_run: bool = False) -> Settings | None:
        """Units, week start, icons and home place. Returns the saved settings, or None if cancelled."""
        pal = self.pal
        rows = [
            ("temperature", "Temperature", config.TEMPERATURE, {"C": "°C", "F": "°F"}),
            ("rain", "Rain", config.RAIN, {"mm": "millimetres", "in": "inches"}),
            ("wind", "Wind", config.WIND, {"km/h": "km/h", "mph": "mph", "m/s": "m/s", "kn": "knots"}),
            ("week_start", "Week starts", config.WEEK_START, {"monday": "Monday", "sunday": "Sunday"}),
            ("icons", "Icons", config.ICONS, {"symbols": "Symbols", "nerd": "Nerd Font", "ascii": "ASCII"}),
        ]
        values = {key: getattr(current, key) for key, *_ in rows}
        home = current.home
        touched_units = False
        index = 0
        items = len(rows) + 2  # rows, home place, save
        message = ""
        while True:
            c = self.canvas()
            if not self.too_small(c):
                self.brand(c, "Welcome" if first_run else "Settings")
                y = 2
                if first_run:
                    for line in (
                        "pdwx compares today's weather with every day since 1940, anywhere in the world.",
                        "Pick your units and a home place. Change them any time with , (comma).",
                    ):
                        c.put(y, 3, line, pal.muted, width=c.w - 6)
                        y += 1
                    y += 1
                for i, (key, label, options, names) in enumerate(rows):
                    chosen = i == index
                    if chosen:
                        c.fill(y, 1, c.w - 2, pal.select)
                        c.put(y, 1, "▌", pal.accent, pal.select)
                    bg = pal.select if chosen else None
                    c.put(y, 3, label, pal.text if chosen else pal.muted, bg, bold=chosen)
                    x = 18
                    for option in options:
                        name = names[option]
                        on = values[key] == option
                        text = f" {name} "
                        c.put(y, x, text, pal.bg if on else pal.dim, pal.accent if on else bg, bold=on)
                        x += len(text) + 1
                    if key == "icons":
                        sample = "".join(style.icon(code, values["icons"]) + " " for code in (0, 2, 3, 45, 61, 71, 95))
                        x = c.put(y, x + 2, sample, pal.yellow, bg)
                        if values["icons"] == "nerd":
                            c.put(y, x + 1, "boxes or blanks? pick Symbols", pal.dim, bg)
                    y += 1
                y += 1
                chosen = index == len(rows)
                if chosen:
                    c.fill(y, 1, c.w - 2, pal.select)
                    c.put(y, 1, "▌", pal.accent, pal.select)
                bg = pal.select if chosen else None
                c.put(y, 3, "Home place", pal.text if chosen else pal.muted, bg, bold=chosen)
                if home:
                    x = c.put(y, 18, home.label, pal.text, bg, width=c.w - 40)
                    c.put(y, x + 2, "opens when you run pdwx", pal.dim, bg)
                else:
                    c.put(y, 18, "none: pdwx asks each time" + ("   Enter to choose" if chosen else ""), pal.dim, bg)
                y += 2
                chosen = index == len(rows) + 1
                label = " Save and continue " if first_run else " Save "
                c.put(y, 3, label, pal.bg if chosen else pal.text, pal.accent if chosen else pal.panel, bold=True)
                y += 2
                style.configure(values["temperature"], values["rain"], values["wind"], values["icons"])
                sample = [
                    ("Looks like  ", pal.dim, False),
                    (f"{style.deg(29.3)} / {style.deg(15.1)}", pal.text, True),
                    (f"   {style.delta(7.4)} vs normal   {style.rain(8.5)}   {style.wind(38.3)}", pal.muted, False),
                ]
                x = 3
                for text, colour, bold in sample:
                    x = c.put(y, x, text, colour, bold=bold)
                c.put(y + 2, 3, message or f"Settings file: {_tilde(config.config_path())}", pal.dim, width=c.w - 6)
                self.footer(
                    c,
                    [
                        ("↑↓", "setting"),
                        ("←→", "change"),
                        ("Enter", "choose / save"),
                        ("Esc", "skip for now" if first_run else "cancel"),
                    ],
                )
            self.show(c)
            key = self.term.read_key(0.2)
            if key is None:
                continue
            if key in ("esc", "ctrl-c"):
                return None
            if key in ("up", "k"):
                index = (index - 1) % items
            elif key in ("down", "j", "tab"):
                index = (index + 1) % items
            elif key in ("left", "right", "h", "l") and index < len(rows):
                name, _, options, _ = rows[index]
                step = -1 if key in ("left", "h") else 1
                values[name] = options[(options.index(values[name]) + step) % len(options)]
                touched_units = touched_units or name in ("temperature", "rain", "wind")
            elif key in ("delete", "backspace") and index == len(rows):
                home = None
            elif key == "enter" and index == len(rows):
                picked = self.pick("Home place")
                if picked:
                    home = picked
                    country = picked.label.rsplit(",", 1)[-1].strip()
                    if first_run and not touched_units and country in config.IMPERIAL_COUNTRIES:
                        values.update(temperature="F", rain="in", wind="mph")
            elif key == "enter":
                chosen_settings = Settings(home=home, **values)
                try:
                    config.save(chosen_settings)
                except OSError as exc:
                    message = f"Could not save: {exc}"
                    continue
                return chosen_settings

    # ── loading ────────────────────────────────────────────────────────
    def load(self, place: Place) -> Loaded | None:
        pal = self.pal
        updates: queue.Queue[tuple[str, Any]] = queue.Queue()
        state = {"message": "Loading forecast", "wait": None}
        first_visit = not has_history(place)

        def progress(message: str, wait: float | None) -> None:
            updates.put(("progress", (message, wait)))

        def work() -> None:
            notes: list[str] = []
            try:
                try:
                    forecast = load_forecast(place, progress=progress)
                    if forecast.get("_stale"):
                        notes.append("Offline · showing the last saved forecast")
                except Exception as exc:
                    forecast = {"daily": {"time": []}}
                    notes.append(f"No forecast ({exc})")
                today = local_today(place, forecast.get("timezone"))
                # small request first: the big history download can use up the per-minute quota
                try:
                    this_year = load_current_year(place, today, progress)
                except Exception as exc:
                    this_year = {"daily": {"time": []}}
                    notes.append(f"This year's archive unavailable ({exc})")
                history = load_history(place, today, self.refresh_history, progress)
                updates.put(("done", Loaded(place, today, history, this_year, forecast, notes)))
            except RateLimited as exc:
                updates.put(("error", f"Open-Meteo's free limit is used up for now: {exc}"))
            except Exception as exc:
                updates.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        started = time.monotonic()
        error: str | None = None
        frame = 0
        while True:
            while not updates.empty():
                kind, value = updates.get()
                if kind == "progress":
                    state["message"], state["wait"] = value
                elif kind == "done":
                    return value
                else:
                    error = value
            c = self.canvas()
            if not self.too_small(c):
                self.brand(c)
                cy = max(3, c.h // 2 - 4)
                head, _, rest = place.label.partition(",")
                c.put(cy, max(2, (c.w - text_width(head)) // 2), head, pal.text, bold=True)
                c.put(cy + 1, max(2, (c.w - text_width(rest.strip())) // 2), rest.strip(), pal.muted)
                if error:
                    msg = clip(error, c.w - 6)
                    c.put(cy + 3, max(2, (c.w - text_width(msg)) // 2), msg, pal.red)
                    self.footer(c, [("Enter", "try again"), ("Esc", "back")])
                else:
                    wait = state["wait"]
                    line = f"{SPINNER[frame % len(SPINNER)]}  {state['message']}"
                    if wait is not None:
                        line += f" · {int(wait)}s"
                    c.put(cy + 3, max(2, (c.w - text_width(line)) // 2), line, pal.accent)
                    elapsed = f"{time.monotonic() - started:.0f}s"
                    c.put(cy + 4, max(2, (c.w - len(elapsed)) // 2), elapsed, pal.dim)
                    if first_visit:
                        for i, note in enumerate(
                            (
                                "First visit: downloading every day since 1940 (about 1 MB).",
                                "It is saved, so this place opens instantly next time.",
                            )
                        ):
                            c.put(cy + 6 + i, max(2, (c.w - len(note)) // 2), note, pal.dim)
                    self.footer(c, [("Esc", "cancel")])
            self.show(c)
            frame += 1
            key = self.term.read_key(0.08)
            if key in ("esc", "ctrl-c", "q"):
                return None
            if key == "enter" and error:
                return self.load(place)


def _tilde(path: Any) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home) else text


def _nerd() -> bool:
    from . import style

    return style.ICON_MODE == "nerd"


def apply_settings(settings: Settings) -> None:
    style.configure(settings.temperature, settings.rain, settings.wind, settings.icons)


def run(
    initial: Place | None, refresh_history: bool = False, open_settings: bool = False, view: str | None = None
) -> None:
    from .ui import WeatherUI

    with Terminal() as term:
        pal = Palette(term.probe_theme())
        app = App(term, pal, refresh_history)
        saved = config.load()
        settings = saved or Settings()
        apply_settings(settings)
        if saved is None or open_settings:
            settings = app.settings(settings, first_run=saved is None) or settings
            apply_settings(settings)
        app.config, app.home = settings, settings.home
        place = initial or settings.home or app.pick()
        while place:
            loaded = app.load(place)
            if loaded is None:
                place = app.pick()
                continue
            with contextlib.suppress(OSError):
                remember_location(place)
            if WeatherUI(app, loaded, view).run() == "quit":
                return
            place = app.pick()
