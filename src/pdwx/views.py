"""Drawing for each view. Mixed into WeatherUI, which supplies state, palette and shared drawing."""

from __future__ import annotations

import calendar
import math
from statistics import mean
from datetime import date, timedelta

from .climate import BASE_YEARS, Climate, Day
from .style import day_month, deg, deg1, delta, delta1, full_date, icon_colour, ordinal
from .style import rain as rain_text
from .style import snow as snow_text
from .style import wind as wind_text
from .term import RGB, Canvas, clip, ink_for, mix, text_width
from .weather_data import condition


class Views:
    """Per-view drawing; every method expects WeatherUI's attributes (clim, pal, sel, today, …)."""

    # ── week ──────────────────────────────────────────────────────────
    def _week_dates(self) -> list[date]:
        if self.sel < self.week_top:
            self.week_top = self.sel
        elif self.sel > self.week_top + timedelta(days=6):
            self.week_top = self.sel - timedelta(days=6)
        return [self.week_top + timedelta(days=i) for i in range(7)]

    def _draw_week(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        dates = self._week_dates()
        others = self.compare[1] if self.compare else None
        lows, highs, rains = [], [], []
        for d in dates:
            for source in (clim, others):
                if source is None:
                    continue
                day, normal = source.day(d), source.normal(d)
                if day:
                    lows.append(day.lo)
                    highs.append(day.hi)
                    rains.append(day.rain or 0)  # noqa: E702
                if normal:
                    lows.append(normal.lo)
                    highs.append(normal.hi)
                    rains.append(normal.rain)  # noqa: E702
        axis = self._axis(lows, highs)  # type: ignore[arg-type]
        rain_top = self._rain_top(rains)
        rx, width, _ = self._layout(c, 8)
        if self.metric == "temp":
            self._ticks(c, top, rx, width, *axis)
        else:
            self._ticks(c, top, rx, width, 0, rain_top, "rain")
        y = top + 1
        gap = 2 if not others and bottom - top >= 30 else 1
        for d in dates:
            label = "Today" if d == self.today else f"{d:%a}"[: 2 if c.compact else 3] + f" {d.day}"
            day = clim.day(d)
            note = wind_text(day.wind) if day and day.wind is not None else ""
            self._row(
                c,
                y,
                label,
                day,
                clim.normal(d),
                axis,
                rain_top,
                d == self.sel,
                label_colour=pal.accent if d == self.today else None,
                note=note,
                note_w=8,
            )
            y += gap
            if others:
                self._row(
                    c,
                    y,
                    self.compare[0].short[:5] if c.compact else "  " + self.compare[0].short[:6],
                    others.day(d),
                    others.normal(d),
                    axis,  # type: ignore[index]
                    rain_top,
                    False,
                    sub=True,
                    label_colour=pal.magenta,
                    note_w=8,
                )
                y += 1
        y += 1
        if bottom - y >= 3:
            y = self._draw_strip(c, y, bottom)
        if bottom - y >= 2:
            self._draw_day_facts(c, y + 1, bottom, self.sel)

    def _draw_strip(self, c: Canvas, y: int, bottom: int) -> int:
        """Past 30 days and the forecast as coloured cells: departures from normal."""
        pal, clim = self.pal, self.clim
        start = self.today - timedelta(days=30)
        end = max((d for d, day in clim.days.items() if day.src == "F"), default=self.today)
        n = (end - start).days + 1
        avail = c.w - 4
        cell = max(1, avail // n)
        x0 = 2
        self._fit(
            c,
            y,
            x0,
            [[("Last 30 days and the forecast", pal.muted, False)], [(" · daily mean vs normal", pal.dim, False)]],
        )
        legend_x = x0 + n * cell - 22
        if legend_x > x0 + 56:
            lx = c.put(y, legend_x, "cooler ", pal.dim)
            for v in (-6, -3, 0, 3, 6):
                lx = c.put(y, lx, "  ", None, pal.anomaly_bg(v))
            c.put(y, lx + 1, "warmer", pal.dim)
        for i in range(n):
            d = start + timedelta(days=i)
            day, normal = clim.day(d), clim.normal(d)
            x = x0 + i * cell
            if day and normal:
                a = day.mid - normal.mid  # type: ignore[operator]
                c.fill(y + 1, x, cell, pal.anomaly_bg(a))
            else:
                c.fill(y + 1, x, cell, pal.panel)
            if d == self.sel:
                c.put(y + 2, x + (cell - 1) // 2, "▲", pal.text)
            elif d == self.today:
                c.put(y + 2, x + (cell - 1) // 2, "╵", pal.accent)
        tx = x0 + 30 * cell
        if self.sel != self.today:
            c.put(y + 2, tx + 2, "today", pal.accent)
        c.put(y + 2, x0, f"{day_month(start)}", pal.dim)
        label = day_month(end)
        c.put(y + 2, x0 + n * cell - len(label), label, pal.dim)
        return y + 3

    def _draw_day_facts(self, c: Canvas, y: int, bottom: int, d: date) -> None:
        pal, clim = self.pal, self.clim
        day = clim.day(d)
        if not day:
            return
        self._want_extras(day)
        facts: list[tuple[str, RGB | None, bool]] = []
        if day.feels is not None:
            facts += [("Feels ", pal.dim, False), (deg(day.feels), pal.temp(day.feels), False), ("   ", None, False)]
        if day.sun is not None:
            facts += [("Sun ", pal.dim, False), (f"{day.sun:.1f} h", pal.yellow, False), ("   ", None, False)]
        if day.wind is not None:
            facts += [("Wind ", pal.dim, False), (wind_text(day.wind), pal.muted, False), ("   ", None, False)]
        if day.humidity is not None:
            facts += [
                ("Humidity ", pal.dim, False),
                (f"{round(day.humidity)}%", pal.muted, False),
                ("   ", None, False),
            ]
        if day.snow:
            facts += [("Snow ", pal.dim, False), (snow_text(day.snow), pal.text, False)]
        if facts:
            self._parts(c, y, 2, facts)
            y += 1
        if y >= bottom:
            return
        similar = clim.similar(day, 5)
        if similar:
            groups: list[list[tuple[str, RGB | None, bool]]] = [
                [("Most like it" if c.compact else "Days most like this one", pal.dim, False)]
            ]
            for s in similar:
                groups.append(
                    [
                        ("   " if len(groups) > 1 or not c.compact else "  ", None, False),
                        (full_date(s.date) + " ", pal.muted, False),
                        (deg(s.hi), pal.temp(s.hi), False),  # type: ignore[arg-type]
                        ("/", pal.dim, False),
                        (deg(s.lo), pal.temp(s.lo), False),  # type: ignore[arg-type]
                    ]
                )
            self._fit(c, y, 2, groups)

    # ── month calendar ────────────────────────────────────────────────
    def _draw_month(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        year, month = self.sel.year, self.sel.month
        first = 6 if self.app.config.week_start == "sunday" else 0
        weeks = calendar.Calendar(first).monthdayscalendar(year, month)
        avail = bottom - top - 2
        ch = 4 if avail >= len(weeks) * 4 else 3 if avail >= len(weeks) * 3 else 2
        cw = max(8, min(16, (c.w - 2) // 7))
        narrow = cw < 10  # too narrow for both high and low: show the high, the low is in the headline
        gx = max(1, (c.w - cw * 7) // 2)
        title = f"{calendar.month_name[month]} {year}"
        c.put(top, gx, title, pal.title, bold=True)
        legend = "cell colour: high vs normal" if self.metric == "temp" else "cell colour: rain"
        if len(title) + len(legend) + 4 <= cw * 7:
            c.put(top, gx + cw * 7 - len(legend) - 1, legend, pal.dim)
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        for i, name in enumerate(names[first:] + names[:first]):
            c.put(top + 1, gx + i * cw + 1, name, pal.dim)
        y0 = top + 2
        for wi, week in enumerate(weeks):
            for col, dn in enumerate(week):
                if not dn:
                    continue
                d = date(year, month, dn)
                x, y = gx + col * cw, y0 + wi * ch
                day, normal = clim.day(d), clim.normal(d)
                inner = cw - 1
                if day and self.metric == "temp" and normal:
                    fill = pal.anomaly_bg(day.hi - normal.hi, 0.9)  # type: ignore[operator]
                elif day and self.metric == "rain":
                    fill = pal.rain_bg(day.rain or 0)
                else:
                    fill = pal.panel if day or normal else None
                for r in range(ch - 1):
                    c.fill(y + r, x, inner, fill)
                chosen = d == self.sel
                num = f"{dn:>2}"
                if chosen:
                    c.put(y, x, f" {num} ", pal.bg, pal.accent, bold=True)
                else:
                    c.put(y, x + 1, num, pal.accent if d == self.today else pal.text, fill, bold=True)
                if d == self.today and not chosen:
                    c.put(y, x + 3, "•", pal.accent, fill)
                if day and day.src != "H" and (ch >= 3 or inner >= 11) and not narrow:
                    c.put(y, x + inner - 2, day.src, pal.green if day.src == "F" else pal.dim, fill)
                if ch == 2:
                    value = (
                        (deg(day.hi) if self.metric == "temp" else rain_text(day.rain))
                        if day
                        else (deg(normal.hi) if normal and self.metric == "temp" else "")
                    )
                    vx = max(x + 4, x + inner - text_width(value)) if narrow else x + 5
                    c.put(
                        y,
                        vx,
                        value,
                        (ink_for(fill) if fill else pal.text) if day else pal.dim,
                        fill,
                        bold=bool(day),
                        width=x + inner - vx,
                    )
                if ch >= 3:
                    if self.metric == "temp":
                        if day:
                            xx = c.put(y + 1, x + 1, deg(day.hi), ink_for(fill) if fill else pal.text, fill, bold=True)
                            if not narrow:  # the low is in the headline for the selected day
                                c.put(
                                    y + 1,
                                    xx + 1,
                                    deg(day.lo),
                                    mix(ink_for(fill) if fill else pal.text, fill or pal.bg, 0.35),
                                    fill,
                                )
                        elif normal:
                            text = deg(normal.hi) if narrow else f"{deg(normal.hi)} {deg(normal.lo)}"
                            c.put(y + 1, x + 1, text, pal.dim, fill)
                        if ch >= 4 and day and normal:
                            c.put(y + 2, x + 1, delta(day.hi - normal.hi), ink_for(fill) if fill else pal.text, fill)  # type: ignore[operator]
                        elif ch >= 4 and normal and not day:
                            c.put(y + 2, x + 1, "norm" if narrow else "normal", pal.dim, fill)
                    else:
                        if day:
                            c.put(
                                y + 1, x + 1, rain_text(day.rain), ink_for(fill) if fill else pal.text, fill, bold=True
                            )
                        if ch >= 4 and normal:
                            c.put(
                                y + 2,
                                x + 1,
                                f"avg {rain_text(normal.rain)}",
                                mix(ink_for(fill) if fill else pal.text, fill or pal.bg, 0.3),
                                fill,
                            )
        y = y0 + len(weeks) * ch
        if y < bottom:
            self._fit(c, y, gx, self._month_summary(year, month))

    def _month_summary(self, year: int, month: int) -> list[list[tuple[str, RGB | None, bool]]]:
        """Highs and rain against normal, as phrases that are dropped whole when space runs out."""
        pal, clim = self.pal, self.clim
        days = [clim.day(date(year, month, dn)) for dn in range(1, calendar.monthrange(year, month)[1] + 1)]
        have = [(dd, clim.normal(dd.date)) for dd in days if dd]
        name = calendar.month_name[month]
        if not have:
            normals = [clim.normal(date(year, month, dn)) for dn in range(1, calendar.monthrange(year, month)[1] + 1)]
            normals = [n for n in normals if n]
            if not normals:
                return []
            return [
                [
                    (f"Normal {name}: ", pal.dim, False),
                    (f"{deg(mean(n.hi for n in normals))}/{deg(mean(n.lo for n in normals))}", pal.muted, False),
                ],
                [(f" · {rain_text(sum(n.rain for n in normals))} of rain", pal.muted, False)],
            ]
        mean_dev = sum(dd.hi - n.hi for dd, n in have if n) / len(have)  # type: ignore[operator]
        rain = sum(dd.rain or 0 for dd, _ in have)
        normal_rain = sum(n.rain for _, n in have if n)
        full = len(have) == len(days)
        lead = f"{name} {year}" + ("" if full else " so far")
        return [
            [
                (lead + ": highs ", pal.dim, False),
                (delta(mean_dev), pal.anomaly(mean_dev), True),
                (" vs normal", pal.dim, False),
            ],
            [(" · rain ", pal.dim, False), (rain_text(rain), pal.rain, True)],
            [(f" (normal {rain_text(normal_rain)}{'' if full else ' by now'})", pal.dim, False)],
        ]

    # ── month chart ───────────────────────────────────────────────────
    def _draw_chart(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        year, month = self.sel.year, self.sel.month
        n = calendar.monthrange(year, month)[1]
        dates = [date(year, month, dn) for dn in range(1, n + 1)]
        lows, highs, rains = [], [], []
        for d in dates:
            day, normal = clim.day(d), clim.normal(d)
            if day:
                lows.append(day.lo)
                highs.append(day.hi)
                rains.append(day.rain or 0)  # noqa: E702
            if normal:
                lows.append(normal.lo)
                highs.append(normal.hi)
                rains.append(normal.rain)  # noqa: E702
        axis = self._axis(lows, highs)  # type: ignore[arg-type]
        rain_top = self._rain_top(rains)
        rx, width, _ = self._layout(c)
        title = f"{calendar.month_name[month]} {year}"
        c.put(top - 1, 2, title, pal.title, bold=True)
        if self.metric == "temp":
            self._ticks(c, top, rx, width, *axis)
        else:
            self._ticks(c, top, rx, width, 0, rain_top, "rain")
        visible = max(1, bottom - top - 2)
        idx = self.sel.day - 1
        if idx < self.chart_scroll:
            self.chart_scroll = idx
        elif idx >= self.chart_scroll + visible:
            self.chart_scroll = idx - visible + 1
        self.chart_scroll = max(0, min(self.chart_scroll, n - visible))
        y = top + 1
        for d in dates[self.chart_scroll : self.chart_scroll + visible]:
            label = f"{d.day:>2} {d:%a}"[: 5 if c.compact else 6]
            self._row(
                c,
                y,
                label,
                clim.day(d),
                clim.normal(d),
                axis,
                rain_top,
                d == self.sel,
                label_colour=pal.accent if d == self.today else None,
            )
            y += 1
        if y < bottom:
            self._fit(c, y, 2, self._month_summary(year, month))

    # ── years for one date ────────────────────────────────────────────
    def _years_rows(self) -> list[Day]:
        rows = self.clim.same_date(self.sel, include_year=True)
        now = self.clim.day(self.sel)
        if now and now.src == "F":
            rows = [now] + rows
        if self.years_sort:
            rows = sorted(rows, key=lambda r: r.hi if self.metric == "temp" else (r.rain or 0), reverse=True)  # type: ignore[arg-type,return-value]
        return rows

    def _draw_years(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        rows = self._years_rows()
        if not rows:
            c.put(top, 2, "No history for this date.", pal.muted)
            return
        self.years_index = max(0, min(self.years_index, len(rows) - 1))
        rec = clim.records(self.sel)
        groups: list[list[tuple[str, RGB | None, bool]]] = [
            [(f"{day_month(self.sel)} across {len(rows)} years", pal.title, True)]
        ]
        if rec["hi"]:
            groups.append(
                [
                    ("   hottest ", pal.dim, False),
                    (deg1(rec["hi"].hi), pal.temp(rec["hi"].hi), False),  # type: ignore[arg-type]
                    (f" {rec['hi'].date.year}", pal.muted, False),
                ]
            )
        if rec["lo"]:
            groups.append(
                [
                    ("   coldest night ", pal.dim, False),
                    (deg1(rec["lo"].lo), pal.temp(rec["lo"].lo), False),  # type: ignore[arg-type]
                    (f" {rec['lo'].date.year}", pal.muted, False),
                ]
            )
        if rec["wet"] and (rec["wet"].rain or 0) >= 0.1:
            groups.append(
                [
                    ("   wettest ", pal.dim, False),
                    (rain_text(rec["wet"].rain), pal.rain, False),
                    (f" {rec['wet'].date.year}", pal.muted, False),
                ]
            )
        groups.append([("   sorted by " + ("value" if self.years_sort else "year"), pal.dim, False)])
        self._fit(c, top, 2, groups)
        normal = clim.normal(self.sel)
        lows = [r.lo for r in rows] + ([normal.lo] if normal else [])
        highs = [r.hi for r in rows] + ([normal.hi] if normal else [])
        axis = self._axis(lows, highs)  # type: ignore[arg-type]
        rain_top = self._rain_top([r.rain or 0 for r in rows])
        rx, width, _ = self._layout(c, 18)
        if self.metric == "temp":
            self._ticks(c, top + 1, rx, width, *axis)
        else:
            self._ticks(c, top + 1, rx, width, 0, rain_top, "rain")
        visible = max(1, bottom - top - 2)
        start = max(0, min(self.years_index - visible // 2, len(rows) - visible))
        y = top + 2
        for i in range(start, min(len(rows), start + visible)):
            r = rows[i]
            note, colour = condition(r.code), pal.dim
            if rec["hi"] and r is rec["hi"]:
                note, colour = "▲ hottest", pal.red
            elif rec["lo"] and r is rec["lo"]:
                note, colour = "▼ coldest night", pal.blue
            elif r.src == "F":
                note, colour = "forecast", pal.green
            self._row(
                c,
                y,
                str(r.date.year),
                r,
                normal,
                axis,
                rain_top,
                i == self.years_index,
                label_colour=pal.accent if r.date.year == self.today.year else None,
                note=note,
                note_colour=colour,
                note_w=18,
            )
            y += 1

    # ── one day, one year ─────────────────────────────────────────────
    def _draw_detail(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        d = self.detail or self.sel
        day, normal = clim.day(d), clim.normal(d)
        if not day:
            c.put(top, 2, f"No data for {full_date(d)}.", pal.muted)
            return
        self._want_extras(day)
        x = 2
        c.put(top, x, condition(day.code), icon_colour(pal, day.code), bold=True)
        y = top + 2
        lines: list[tuple[str, str, RGB, str, RGB]] = []
        if normal:
            lines.append(
                (
                    "High",
                    deg1(day.hi),
                    pal.temp(day.hi),
                    f"{delta(day.hi - normal.hi)} vs normal {deg(normal.hi)}",  # type: ignore[arg-type,operator]
                    pal.anomaly(day.hi - normal.hi),
                )
            )  # type: ignore[operator]
            lines.append(
                (
                    "Low",
                    deg1(day.lo),
                    pal.temp(day.lo),
                    f"{delta(day.lo - normal.lo)} vs normal {deg(normal.lo)}",  # type: ignore[arg-type,operator]
                    pal.anomaly(day.lo - normal.lo),
                )
            )  # type: ignore[operator]
            lines.append(("Rain", rain_text(day.rain), pal.rain, f"normal {rain_text(normal.rain)} a day", pal.dim))
        if day.feels is not None:
            lines.append(("Feels like", deg(day.feels), pal.temp(day.feels), "at the warmest", pal.dim))
        if day.sun is not None:
            lines.append(("Sunshine", f"{day.sun:.1f} h", pal.yellow, "", pal.dim))
        if day.wind is not None:
            lines.append(("Wind", wind_text(day.wind), pal.muted, "strongest", pal.dim))
        if day.humidity is not None:
            lines.append(("Humidity", f"{round(day.humidity)}%", pal.muted, "daily mean", pal.dim))
        if day.snow:
            lines.append(("Snow", snow_text(day.snow), pal.text, "", pal.dim))
        if day.feels is None and day.src == "H" and not self._extras_complete():
            lines.append(("", "", pal.dim, "Sun, wind, humidity still loading", pal.dim))
        for label, value, vc, note, nc in lines:
            if y >= bottom:
                return
            c.put(y, x, label, pal.dim)
            c.put(y, x + 12, value, vc, bold=True)
            c.put(y, x + 22, note, nc)
            y += 1
        warm, cool, total = clim.rank(d, day.hi)  # type: ignore[arg-type]
        lwarm, lcool, _ = clim.rank(d, day.lo, "lo")  # type: ignore[arg-type]
        y += 1
        sentence = (
            f"On {day_month(d)} since {clim.first_year}, this high ranks {ordinal(warm)} warmest and this "
            f"low {ordinal(lwarm)} warmest of {total} years."
        )
        y = self._wrap(c, y, x, sentence, pal.muted, bottom) + 1
        if y + 3 >= bottom:
            return
        c.put(y, x, f"The week around it, {d.year}", pal.title, bold=True)
        y += 1
        span = [d + timedelta(days=i) for i in range(-3, 4)]
        lows = [dd.lo for s in span if (dd := clim.day(s))] + [n.lo for s in span if (n := clim.normal(s))]
        highs = [dd.hi for s in span if (dd := clim.day(s))] + [n.hi for s in span if (n := clim.normal(s))]
        axis = self._axis(lows, highs)  # type: ignore[arg-type]
        for s in span:
            if y >= bottom:
                break
            label = f"{s:%a}"[: 2 if c.compact else 3] + f" {s.day}"
            self._row(c, y, label, clim.day(s), clim.normal(s), axis, self._rain_top([0]), s == d)
            y += 1

    # ── climate ───────────────────────────────────────────────────────
    def _stripes(self, c: Canvas, y: int, rows: int, series: list[tuple[int, float, float]], rain: bool = False) -> int:
        """Warming-stripe style band across the full width, coloured against the 1991–2020 normal."""
        pal = self.pal
        if not series:
            return y
        base_t, base_r = Climate.baseline(series)
        x0, width, n = 2, c.w - 4, len(series)
        devs = sorted(abs(t - base_t) for _, t, _ in series)
        scale = max(0.6, devs[max(0, int(n * 0.95) - 1)])
        starts: list[int] = []
        for col in range(width):
            i = min(n - 1, col * n // width)
            year, t, r = series[i]
            if not starts or starts[-1] != i:
                starts.append(i)
            colour = (
                pal.wet_dry((r - base_r) / base_r if base_r else 0)
                if rain
                else pal.anomaly_bg((t - base_t) / scale * 5.5)
            )
            for k in range(rows):
                c.put(y + k, x0 + col, " ", None, colour)
        ly = y + rows
        first, last = str(series[0][0]), str(series[-1][0])
        c.put(ly, x0, first, pal.dim)
        c.put(ly, x0 + width - len(last), last, pal.dim)
        for i, (year, _, _) in enumerate(series):
            if year % 20 == 0:
                lx = x0 + math.ceil(i * width / n)
                if lx > x0 + len(first) + 1 and lx + 4 < x0 + width - len(last) - 1:
                    c.put(ly, lx, str(year), pal.dim)
        return ly + 1

    def _heading(
        self, c: Canvas, y: int, title: str, subtitle: str, note: str = "", note_colour: RGB | None = None
    ) -> int:
        """Title, then the note on the right; the grey subtitle only if both still fit. Returns the next row."""
        x = c.put(y, 2, title, self.pal.title, bold=True)
        if c.compact and note and x + 3 + len(note) > c.w - 2:
            c.put(y + 1, 2, clip(note, c.w - 4), note_colour or self.pal.muted, bold=note_colour is not None)
            return y + 2
        right = c.w - len(note) - 2
        if note and right > x + 2:
            c.put(y, right, note, note_colour or self.pal.muted, bold=note_colour is not None)
        elif note:
            c.put(y, x + 3, clip(note, c.w - x - 4), note_colour or self.pal.muted)
            return y + 1
        if subtitle and x + 3 + len(subtitle) < right - 2:
            c.put(y, x + 3, subtitle, self.pal.dim)
        return y + 1

    def _draw_climate(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        month = self.sel.month
        mname = calendar.month_name[month]
        y = top
        annual = clim.annual()
        monthly = clim.monthly(month)
        tall = bottom - top >= 30
        # yearly stripes
        if annual:
            change = Climate.change(annual)
            txt, colour = "", None
            if change:
                label, dv = change
                txt = f"Recent years are {delta1(dv)} {'warmer' if dv >= 0 else 'cooler'} than {label}"
                colour = pal.anomaly(dv * 4)
            y = self._heading(
                c, y, f"Every year since {annual[0][0]}", "average temperature, one stripe per year", txt, colour
            )
            y = self._stripes(c, y, 5 if tall else 3, annual) + 1
        # month stripes
        if monthly and bottom - y > 6:
            change = Climate.change(monthly)
            txt, colour = "", None
            if change:
                label, dv = change
                txt = f"{mname}s are {delta1(dv)} {'warmer' if dv >= 0 else 'cooler'} than in {label}"
                colour = pal.anomaly(dv * 4)
            y = self._heading(c, y, f"{mname} only", "←→ changes month", txt, colour)
            y = self._stripes(c, y, 4 if tall else 2, monthly) + 1
        # decades of the month
        if monthly and bottom - y > 5:
            decades = Climate.decade_means(monthly)
            c.put(y, 2, f"{mname} by decade", pal.muted, bold=True)
            x = 2
            y += 1
            base_t, _ = Climate.baseline(monthly)
            for dec, t, _rain in decades:
                cell = f" {dec}s {deg1(t)} "
                if x + len(cell) > c.w - 2:
                    if not c.compact or y + 2 >= bottom:
                        break
                    x, y = 2, y + 1
                bgc = pal.anomaly_bg((t - base_t) * 4, 0.8)
                c.put(y, x, cell, ink_for(bgc), bgc)
                x += len(cell) + 1
            y += 2
        # hot days
        if bottom - y > 4:
            thr = clim.hot_threshold()
            hot = clim.hot_days_by_decade(thr)
            x = c.put(y, 2, "Hot days a year", pal.muted, bold=True)
            self._fit(
                c,
                y,
                x + 2,
                [
                    [(f"above {deg(thr)}", pal.dim, False)],
                    [(f", the hottest 5% of {BASE_YEARS[0]}–{BASE_YEARS[1]} days", pal.dim, False)],
                ],
            )
            y += 1
            if hot:
                peak = max(v for _, v in hot) or 1
                x = 2
                for dec, v in hot:
                    bar = "▁▂▃▄▅▆▇█"[min(7, int(v / peak * 7.99))]
                    cell = f"{dec}s {bar} {v:.0f}  "
                    if x + len(cell) > c.w - 2:
                        if not c.compact or y + 2 >= bottom:
                            break
                        x, y = 2, y + 1
                    x = c.put(y, x, f"{dec}s ", pal.dim)
                    x = c.put(y, x, bar, pal.temp(thr), bold=True)
                    x = c.put(y, x, f" {v:.0f}  ", pal.text)
            y += 1
            first = clim.first_hot_by_decade(thr)
            if len(first) >= 2 and y < bottom:
                a_dec, a = first[0]
                b_dec, b = first[-1]
                ref = clim.season_start(2001)
                da, db = ref + timedelta(days=round(a)), ref + timedelta(days=round(b))
                shift = round(a - b)
                word = (
                    f"{abs(shift)} days {'earlier' if shift > 0 else 'later'}" if abs(shift) >= 3 else "about the same"
                )
                self._fit(
                    c,
                    y,
                    2,
                    [
                        [
                            ("First hot day" if c.compact else "First hot day of the season: ", pal.dim, False),
                            (": " if c.compact else "", pal.dim, False),
                            (f"{a_dec}s ~{day_month(da)}", pal.muted, False),
                            (" → " if c.compact else "  →  ", pal.dim, False),
                            (f"{b_dec}s ~{day_month(db)}", pal.text, True),
                        ],
                        [
                            (
                                f"   ({word})" if not c.compact else f" ({word})",
                                pal.red if shift >= 3 else pal.dim,
                                False,
                            )
                        ],
                    ],
                )
                y += 1
            y += 1
        # rainfall stripes
        if annual and bottom - y > 4:
            wettest = max(annual, key=lambda s: s[2])
            driest = min(annual, key=lambda s: s[2])
            txt = f"Driest {driest[0]} {rain_text(driest[2])} · wettest {wettest[0]} {rain_text(wettest[2])}"
            y = self._heading(c, y, "Rain every year", "yellow drier, blue wetter than normal", txt)
            y = self._stripes(c, y, 3 if tall else 2, annual, rain=True) + 1
        # dry spells
        if bottom - y > 1:
            spells = clim.dry_spells()
            groups: list[list[tuple[str, RGB | None, bool]]] = [
                [("Dry spells" if c.compact else f"Dry spells (under {rain_text(1.0)})", pal.muted, True)]
            ]
            ever, this_year, current = spells["ever"], spells["year"], spells["current"]
            if ever:
                groups.append([("  longest ", pal.dim, False), (f"{ever[2]} days", pal.yellow, True)])
                groups.append([(f" ({day_month(ever[0])} – {full_date(ever[1])})", pal.dim, False)])
            if this_year:
                groups.append([("   this year ", pal.dim, False), (f"{this_year[2]} days", pal.text, False)])
            if current and current[2] > 1:
                groups.append([("   now ", pal.dim, False), (f"{current[2]} dry days running", pal.text, False)])
            if c.compact and len(groups) > 3 and y + 1 < bottom:  # dates on the first line, this year and now below
                self._fit(c, y, 2, groups[:3])
                self._fit(c, y + 1, 1, groups[3:])
            else:
                self._fit(c, y, 2, groups)

    # ── heatmap ───────────────────────────────────────────────────────
    def _draw_heatmap(self, c: Canvas, top: int, bottom: int) -> None:
        pal, clim = self.pal, self.clim
        years = list(range(clim.first_year, self.today.year + 1))
        self.heat_year = max(years[0], min(years[-1], self.heat_year))
        lx = 7
        width = c.w - lx - 2
        cols = min(width, 366)
        per = 366 / cols
        subtitle = "   daily mean vs normal" if c.compact else "   daily mean temperature vs normal · two years per row"
        self._fit(c, top, 2, [[("Every day since " + str(years[0]), pal.title, True)], [(subtitle, pal.dim, False)]])
        lgx = c.w - 30
        if lgx > 70:
            x = c.put(top, lgx, "colder ", pal.dim)
            for v in (-6, -3, 0, 3, 6):
                x = c.put(top, x, "  ", None, pal.anomaly_bg(v))
            c.put(top, x + 1, "warmer", pal.dim)
        # month labels + cursor
        my = top + 1
        for m in range(1, 13):
            s = date(2000, m, 1).timetuple().tm_yday - 1
            x = lx + int(s / per)
            c.put(
                my, x, calendar.month_abbr[m], pal.accent if m == self.sel.month else pal.dim, bold=m == self.sel.month
            )
        rows_avail = bottom - my - 2
        pairs = [years[i : i + 2] for i in range(0, len(years), 2)]
        sel_pair = (self.heat_year - years[0]) // 2
        start = max(0, min(sel_pair - rows_avail // 2, len(pairs) - rows_avail))
        y = my + 1
        cache: dict[int, list[float | None]] = {}

        def bins(year: int) -> list[float | None]:
            if year not in cache:
                values = clim.year_anomalies(year)
                out: list[float | None] = []
                for col in range(cols):
                    # a centred week: single days are too noisy to show warm and cold spells
                    centre = int((col + 0.5) * per)
                    chunk = [v for v in values[max(0, centre - 3) : centre + 4] if v is not None]
                    out.append(sum(chunk) / len(chunk) if len(chunk) >= 3 else None)
                cache[year] = out
            return cache[year]

        for pair in pairs[start : start + rows_avail]:
            upper = bins(pair[0])
            lower = bins(pair[1]) if len(pair) > 1 else [None] * cols
            for col in range(cols):
                u, l_ = upper[col], lower[col]
                fg = pal.anomaly_bg(u * 1.6) if u is not None else pal.bg
                bgc = pal.anomaly_bg(l_ * 1.6) if l_ is not None else pal.bg
                c.put(y, lx + col, "▀", fg, bgc)
            marked = self.heat_year in pair
            if marked:
                c.put(y, 1, str(self.heat_year), pal.accent, bold=True)
                c.put(y, lx - 1, "▸" if self.heat_year == pair[0] else "▹", pal.accent)
            elif pair[0] % 10 == 0 or (len(pair) > 1 and pair[1] % 10 == 0):
                dec = pair[0] if pair[0] % 10 == 0 else pair[1]
                c.put(y, 1, str(dec), pal.dim)
            y += 1
        # the selected year's month in words
        summary_y = bottom - 1
        m = self.sel.month
        try:
            first = date(self.heat_year, m, 1)
        except ValueError:
            return
        n = calendar.monthrange(self.heat_year, m)[1]
        devs = []
        for dn in range(n):
            d = first + timedelta(days=dn)
            day, normal = clim.day(d), clim.normal(d)
            if day and normal:
                devs.append(day.mid - normal.mid)  # type: ignore[operator]
        if devs:
            mean_dev = sum(devs) / len(devs)
            key = "   ▸ upper  ▹ lower half" if c.compact else "   ▸ upper half of a row  ▹ lower half"
            self._fit(
                c,
                summary_y,
                2,
                [
                    [
                        (f"{calendar.month_name[m]} {self.heat_year}: ", pal.text, True),
                        (delta(mean_dev), pal.anomaly(mean_dev), True),
                        (" vs normal" if c.compact else " vs normal (daily mean)", pal.dim, False),
                    ],
                    [(key, pal.dim, False)],
                ],
            )
