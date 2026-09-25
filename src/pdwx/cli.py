"""Command-line entry point."""

from __future__ import annotations

import argparse
import contextlib
import json
import locale
import sys

from . import __version__
from .ui import VIEWS
from .location_history import recent_locations
from .weather_data import Place, geocode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="pdwx",
        description="How unusual is today? Forecasts against every day since 1940, for any place.",
    )
    result.add_argument("--location", help="Place name or latitude,longitude (opens a picker if omitted)")
    result.add_argument("--refresh-history", action="store_true", help="Download the archive again")
    result.add_argument("--view", choices=VIEWS, help="Open on this view (default: week)")
    result.add_argument("--settings", action="store_true", help="Open settings (units, icons, home place) first")
    result.add_argument("--version", action="version", version=f"pdwx {__version__}")
    out = result.add_mutually_exclusive_group()
    out.add_argument(
        "--line",
        action="store_true",
        help="Print one line about today (for a status bar) and exit; uses your home place",
    )
    out.add_argument("--json", action="store_true", help="Like --line, as Waybar JSON (text, tooltip, class)")
    out.add_argument(
        "--backtest",
        action="store_true",
        help="Score the long-range outlook methods against 1991 onward and print the results",
    )
    return result


def _default_place(query: str | None) -> Place | None:
    from . import config
    from .app import apply_settings

    settings = config.load()
    if settings:
        apply_settings(settings)
    if query:
        return geocode(query)
    if settings and settings.home:
        return settings.home
    recent = recent_locations()
    return recent[0] if recent else None


def summary(place: Place) -> dict[str, str]:
    """Today's one-liner and tooltip, from the cache where possible."""
    from .climate import Climate
    from .style import deg, deg1, delta
    from .ui import rank_phrase
    from .weather_data import has_history, load_current_year, load_forecast, load_history, local_today

    if not has_history(place):
        return {"text": f"{place.short}: open pdwx once", "tooltip": "History not downloaded yet", "class": "empty"}
    forecast = load_forecast(place)
    today = local_today(place, forecast.get("timezone"))
    history = load_history(place, today)
    try:
        this_year = load_current_year(place, today)
    except Exception:
        this_year = None
    clim = Climate(history, this_year, forecast, today, None, place.latitude)
    day, normal = clim.day(today), clim.normal(today)
    if not day or not normal:
        return {"text": f"{place.short}: no forecast", "tooltip": "", "class": "empty"}
    dv = day.hi - normal.hi  # type: ignore[operator]
    rank = rank_phrase(clim, today, day.hi)  # type: ignore[arg-type]
    text = f"{deg(day.hi)} · {delta(dv)} vs normal"
    short_rank = rank.split(" in ")[0] if " warmest " in f" {rank}" or " coolest " in f" {rank}" else ""
    if short_rank:
        text += f" · {short_rank}"
    rec = clim.records(today)
    tips = [
        f"{place.label}",
        f"Today {deg(day.hi)}/{deg(day.lo)} · normal {deg(normal.hi)}/{deg(normal.lo)}",
        rank[:1].upper() + rank[1:],
    ]
    if rec["hi"]:
        tips.append(f"Record {deg1(rec['hi'].hi)} ({rec['hi'].date.year})")
    alerts = clim.alerts()
    for alert_day, kind, old in alerts[:3]:
        attr = "hi" if kind in ("hottest", "coldest day") else "lo"
        new, record = deg(getattr(alert_day, attr)), deg1(getattr(old, attr))
        tips.append(f"Record watch {alert_day.date:%a %d %b}: {kind} ({new} vs {record})")
    css = "record" if alerts else "warm" if dv >= 3 else "cool" if dv <= -3 else "normal"
    return {"text": text, "tooltip": "\n".join(tips), "class": css}


def load_climate(place: Place):
    """Climate for a place from the cache (downloads only what is missing)."""
    from .climate import Climate
    from .weather_data import cached_extras, load_current_year, load_forecast, load_history, local_today

    forecast = load_forecast(place)
    today = local_today(place, forecast.get("timezone"))
    history = load_history(place, today)
    try:
        this_year = load_current_year(place, today)
    except Exception:
        this_year = None
    return Climate(history, this_year, forecast, today, cached_extras(place), place.latitude)


def print_backtest(place: Place) -> None:
    import time
    from .outlook import LABELS, WEEKS, Outlook, parse_oni
    from .weather_data import load_oni

    started = time.monotonic()
    oni_text = load_oni()
    outlook = Outlook(load_climate(place), parse_oni(oni_text) if oni_text else None)
    show = (lambda share: print(f"\r  scoring… {share:4.0%}", end="", file=sys.stderr)) if sys.stderr.isatty() else None
    r = outlook.backtest(place.label, show)
    print("\r", end="", file=sys.stderr)
    print(
        f"{place.label} · fitted 1961–1990 · scored {r.test_years[0]}–{r.test_years[1]} "
        f"({r.issues} issue dates × highs and lows) · {time.monotonic() - started:.0f}s"
    )
    print("Skill = how much smaller the squared error is than the plain 30-year normal (0% = no better).")
    print()
    models = list(r.daily)
    width = max(len(LABELS[m]) for m in models)
    leads = (1, 3, 7, 14, 17, 21, 28, 35, 42, 60)
    print("Daily, days ahead".ljust(width + 2) + "".join(f"{d:>6}" for d in leads))
    for m in models:
        print(LABELS[m].ljust(width + 2) + "".join(f"{r.daily[m][d - 1]:>6.0%}" for d in leads))
    print()
    print("Weekly average, week".ljust(width + 2) + "".join(f"{i + 1:>6}" for i in range(len(WEEKS))))
    for m in models:
        print(LABELS[m].ljust(width + 2) + "".join(f"{v:>6.0%}" for v in r.weekly[m]))
    print()
    print("Weekly, vs trend normal".ljust(width + 2) + "".join(f"{i + 1:>6}" for i in range(len(WEEKS))))
    for m in models:
        if m not in ("normal", "trend"):
            print(LABELS[m].ljust(width + 2) + "".join(f"{v:>6.0%}" for v in r.weekly_vs_trend[m]))
    print()
    print(
        f"Best for weeks 3–8: {LABELS[r.best]} · beats the trend normal by ≥3% out to day {r.useful_days} · "
        f"10–90% range held {r.coverage:.0%} of outcomes (target 80%) · El Niño data: {'yes' if r.enso else 'no'}"
    )


def main() -> None:
    args = parser().parse_args()
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "")
    try:
        if args.backtest:
            place = _default_place(args.location)
            if not place:
                print("pdwx: no place yet")
                return
            print_backtest(place)
            return
        if args.line or args.json:
            place = _default_place(args.location)
            if not place:
                print("pdwx: no place yet")
                return
            result = summary(place)
            print(json.dumps(result) if args.json else result["text"])
            return
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("pdwx: needs an interactive terminal (try --line)", file=sys.stderr)
            raise SystemExit(2)
        from .app import run

        run(geocode(args.location) if args.location else None, args.refresh_history, args.settings, args.view)
    except KeyboardInterrupt:
        return
    except Exception as exc:
        print(f"pdwx: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
