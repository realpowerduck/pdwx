"""Can local history forecast past the 16-day forecast? A backtest that scores statistical outlooks honestly.

Every method is fitted on issue dates in 1961–1990 and scored on 1991 onward. At each issue date
every input (normal, trend, recent anomaly, ENSO state, analog years) uses only data from before it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from collections.abc import Callable

from .climate import SLOTS, Climate, slot

VARS = ("hi", "lo")
CLIMO_YEARS = 30
MIN_YEARS = 20
TRAIN = (1961, 1990)
MAX_LEAD = 60
ISSUE_STEP = 5
WEEKS = [(1, 7), (8, 14), (15, 21), (22, 28), (29, 35), (36, 42), (43, 49), (50, 56)]
SEASON = {12: 0, 1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3}
ANALOG_K = 10
MODELS = {  # predictors used by each fitted model (all include an intercept)
    "persist": ("a7", "a30"),
    "enso": ("a7", "a30", "oni"),
    "analog": ("an",),
    "all": ("a7", "a30", "oni", "an"),
}
LABELS = {
    "normal": "30-year normal",
    "trend": "trend-adjusted normal",
    "persist": "+ recent anomaly",
    "enso": "+ El Niño/La Niña",
    "analog": "similar past years",
    "all": "everything combined",
}


def parse_oni(text: str) -> dict[tuple[int, int], float]:
    """NOAA CPC ONI table → {(year, centre month): anomaly}. DJF is centred on January."""
    order = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] in order:
            try:
                out[(int(parts[1]), order.index(parts[0]) + 1)] = float(parts[3])
            except ValueError:
                continue
    return out


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        m[i][i] += 1e-6
        pivot = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[pivot] = m[pivot], m[i]
        if abs(m[i][i]) < 1e-12:
            continue
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                for c in range(i, n + 1):
                    m[r][c] -= f * m[i][c]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit(rows: list[tuple[list[float], float]]) -> list[float]:
    """Least squares with intercept: rows of (predictors, target)."""
    if not rows:
        return []
    k = len(rows[0][0]) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for x, y in rows:
        v = [1.0] + x
        for i in range(k):
            xty[i] += v[i] * y
            for j in range(k):
                xtx[i][j] += v[i] * v[j]
    return _solve(xtx, xty)


def apply(coef: list[float], x: list[float]) -> float:
    return coef[0] + sum(c * v for c, v in zip(coef[1:], x, strict=True)) if coef else 0.0


def quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    pos = q * (len(s) - 1)
    lo = int(pos)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (pos - lo)


@dataclass
class Report:
    place: str
    test_years: tuple[int, int]
    issues: int
    daily: dict[str, list[float]]  # model → skill vs 30-year normal at leads 1..60
    weekly: dict[str, list[float]]  # model → skill for week 1..8 averages
    weekly_vs_trend: dict[str, list[float]]
    coverage: float  # share of test outcomes inside the 10–90% range
    useful_days: int  # last lead where the outlook beats trend by ≥ 3%
    best: str
    enso: bool
    notes: list[str] = field(default_factory=list)


class Outlook:
    def __init__(self, clim: Climate, oni: dict[tuple[int, int], float] | None = None):
        self.clim = clim
        self.oni = oni or {}
        observed = [d for d in clim.observed]
        self.y0 = observed[0].year
        self.y1 = observed[-1].year
        self.last_obs = observed[-1]
        ny = self.y1 - self.y0 + 1
        self.val: dict[str, list[list[float | None]]] = {v: [[None] * SLOTS for _ in range(ny)] for v in VARS}
        for d in observed:
            day = clim.days[d]
            for v in VARS:
                self.val[v][d.year - self.y0][slot(d)] = getattr(day, v)
        self._normals: dict[tuple[str, int], list[float | None]] = {}
        self._prefix()
        self.anom: dict[str, dict[date, float]] = {v: {} for v in VARS}
        for d in observed:
            if d.year >= self.y0 + MIN_YEARS:
                for v in VARS:
                    n = self.normal(v, d.year, slot(d))
                    x = self.val[v][d.year - self.y0][slot(d)]
                    if n is not None and x is not None:
                        self.anom[v][d] = x - n
        self._pent: dict[str, dict[date, float | None]] = {v: {} for v in VARS}
        self.coef: dict[tuple[str, str, int, int], list[float]] = {}
        self.spread: dict[tuple[str, int, int], tuple[float, float]] = {}
        self.report: Report | None = None

    # ── normals ────────────────────────────────────────────────────────
    @staticmethod
    def _window(row: list[float | None], half: int) -> list[tuple[float, int]]:
        out = []
        for s in range(SLOTS):
            total, n = 0.0, 0
            for off in range(-half, half + 1):
                x = row[(s + off) % SLOTS]
                if x is not None:
                    total += x
                    n += 1
            out.append((total, n))
        return out

    def _prefix(self) -> None:
        """Year-prefix sums so any 30-year normal and any trend slope is O(1) per slot."""
        self.ps: dict[str, list[list[tuple[float, int]]]] = {}
        self.pt: dict[str, list[list[tuple[float, float, float, float, float]]]] = {}
        for v in VARS:
            ps = [[(0.0, 0)] * SLOTS]
            pt = [[(0.0, 0.0, 0.0, 0.0, 0.0)] * SLOTS]
            for yi, row in enumerate(self.val[v]):
                w15 = self._window(row, 7)
                w61 = self._window(row, 30)
                year = float(self.y0 + yi)
                prev_s, prev_t = ps[-1], pt[-1]
                ps.append([(prev_s[s][0] + w15[s][0], prev_s[s][1] + w15[s][1]) for s in range(SLOTS)])
                nxt = []
                for s in range(SLOTS):
                    n, sy, sm, sym, syy = prev_t[s]
                    if w61[s][1] >= 40:
                        m = w61[s][0] / w61[s][1]
                        n, sy, sm, sym, syy = n + 1, sy + year, sm + m, sym + year * m, syy + year * year
                    nxt.append((n, sy, sm, sym, syy))
                pt.append(nxt)
            self.ps[v], self.pt[v] = ps, pt

    def climo(self, v: str, year: int, s: int) -> tuple[float, float] | None:
        """Mean of the (up to) 30 years before `year`, and the centre year of that period."""
        end = min(year, self.y1 + 1) - self.y0
        start = max(0, end - CLIMO_YEARS)
        if end - start < MIN_YEARS:
            return None
        total = self.ps[v][end][s][0] - self.ps[v][start][s][0]
        n = self.ps[v][end][s][1] - self.ps[v][start][s][1]
        return (total / n, self.y0 + (start + end - 1) / 2) if n else None

    def slope(self, v: str, year: int, s: int) -> float:
        end = min(year, self.y1 + 1) - self.y0
        n, sy, sm, sym, syy = self.pt[v][end][s]
        den = n * syy - sy * sy
        return (n * sym - sy * sm) / den if n >= MIN_YEARS and den else 0.0

    def normal(self, v: str, year: int, s: int) -> float | None:
        """Trend-adjusted normal: the prior 30-year mean moved along the trend fitted on all prior years."""
        key = (v, year)
        if key not in self._normals:
            row: list[float | None] = []
            for sl in range(SLOTS):
                c = self.climo(v, year, sl)
                row.append(None if c is None else c[0] + self.slope(v, year, sl) * (year - c[1]))
            self._normals[key] = row
        return self._normals[key][s]

    # ── predictors ─────────────────────────────────────────────────────
    def _mean_anom(self, v: str, t: date, days: int, need: int) -> float | None:
        values = [a for i in range(1, days + 1) if (a := self.anom[v].get(t - timedelta(days=i))) is not None]
        return sum(values) / len(values) if len(values) >= need else None

    def _oni(self, t: date) -> float | None:
        """Latest ONI season that had been published by t (centre month two months back)."""
        m = t.year * 12 + t.month - 1 - 2
        return self.oni.get((m // 12, m % 12 + 1))

    def _p5(self, v: str, d: date) -> float | None:
        """Mean anomaly of the five days before d (cached)."""
        cache = self._pent[v]
        if d not in cache:
            cache[d] = self._mean_anom(v, d, 5, 3)
        return cache[d]

    def _analogs(self, v: str, t: date) -> list[int]:
        """Past years whose 60 days before this calendar date best match the 60 days before t."""
        now = [self._p5(v, t - timedelta(days=5 * k)) for k in range(12)]
        if sum(x is None for x in now) > 2:
            return []
        scored = []
        for y in range(self.y0 + MIN_YEARS, t.year):
            ty = same_day(t, y)
            past = [self._p5(v, ty - timedelta(days=5 * k)) for k in range(12)]
            dist, n = 0.0, 0
            for a, b in zip(now, past, strict=True):
                if a is not None and b is not None:
                    dist += (a - b) ** 2
                    n += 1
            if n >= 9:
                scored.append((dist / n, y))
        scored.sort()
        return [y for _, y in scored[:ANALOG_K]]

    def _features(self, v: str, t: date) -> dict[str, Any] | None:
        a7 = self._mean_anom(v, t, 7, 5)
        a30 = self._mean_anom(v, t, 30, 20)
        if a7 is None or a30 is None:
            return None
        oni = self._oni(t)
        analogs = self._analogs(v, t)
        an = []
        for lead in range(1, MAX_LEAD + 1):
            vals = [a for y in analogs if (a := self.anom[v].get(same_day(t, y) + timedelta(days=lead))) is not None]
            an.append(sum(vals) / len(vals) if vals else 0.0)
        return {"a7": a7, "a30": a30, "oni": oni if oni is not None else 0.0, "an": an, "has_oni": oni is not None}

    @staticmethod
    def _x(model: str, f: dict[str, Any], lead: int | tuple[int, int]) -> list[float]:
        out = []
        for name in MODELS[model]:
            if name == "an":
                if isinstance(lead, tuple):
                    out.append(sum(f["an"][lead[0] - 1 : lead[1]]) / (lead[1] - lead[0] + 1))
                else:
                    out.append(f["an"][lead - 1])
            else:
                out.append(f[name])
        return out

    # ── backtest ───────────────────────────────────────────────────────
    def _issues(self, first: int, last: int) -> list[date]:
        out = []
        d = date(first, 1, 1)
        stop = min(date(last, 12, 31), self.last_obs - timedelta(days=MAX_LEAD))
        while d <= stop:
            out.append(d)
            d += timedelta(days=ISSUE_STEP)
        return out

    def _targets(self, v: str, t: date) -> tuple[list[float | None], list[float | None]]:
        daily = [self.anom[v].get(t + timedelta(days=lead)) for lead in range(1, MAX_LEAD + 1)]
        weekly = []
        for a, b in WEEKS:
            chunk = [x for x in daily[a - 1 : b] if x is not None]
            weekly.append(sum(chunk) / len(chunk) if len(chunk) >= 5 else None)
        return daily, weekly

    def backtest(self, label: str = "", progress: Callable[[float], None] | None = None) -> Report:
        use_oni = bool(self.oni)
        models = [m for m in MODELS if use_oni or "oni" not in MODELS[m]]
        train_issues = self._issues(max(TRAIN[0], self.y0 + MIN_YEARS + 1), TRAIN[1])
        test_issues = self._issues(TRAIN[1] + 1, self.y1)
        total = 2 * (len(train_issues) + len(test_issues))
        done = 0
        samples: dict[tuple[str, str], list[tuple[date, dict[str, Any], list, list]]] = {}
        for v in VARS:
            for phase, issues in (("train", train_issues), ("test", test_issues)):
                rows = []
                for t in issues:
                    f = self._features(v, t)
                    done += 1
                    if progress and done % 25 == 0:
                        progress(done / total)
                    if f is None or (use_oni and not f["has_oni"]):
                        continue
                    daily, weekly = self._targets(v, t)
                    rows.append((t, f, daily, weekly))
                samples[(v, phase)] = rows
        # fit per variable, model, lead (daily 1..60 then weekly 101..108), season
        for v in VARS:
            for m in models:
                for lead in range(1, MAX_LEAD + 1 + len(WEEKS)):
                    for season in range(4):
                        rows = []
                        for t, f, daily, weekly in samples[(v, "train")]:
                            target_day = t + timedelta(days=lead if lead <= MAX_LEAD else WEEKS[lead - MAX_LEAD - 1][0])
                            if SEASON[target_day.month] != season:
                                continue
                            y = daily[lead - 1] if lead <= MAX_LEAD else weekly[lead - MAX_LEAD - 1]
                            if y is None:
                                continue
                            key = lead if lead <= MAX_LEAD else WEEKS[lead - MAX_LEAD - 1]
                            rows.append((self._x(m, f, key), y))
                        self.coef[(v, m, lead, season)] = fit(rows)
        # score on the test years; the baseline is the plain prior-30-year normal
        err: dict[str, list[float]] = {m: [0.0] * (MAX_LEAD + len(WEEKS)) for m in ["normal", "trend"] + models}
        count = [0] * (MAX_LEAD + len(WEEKS))
        resid: dict[tuple[int, int], list[float]] = {}
        best_model = "all" if use_oni else "persist"
        covered = tested = 0
        for v in VARS:
            for t, f, daily, weekly in samples[(v, "test")]:
                for lead in range(1, MAX_LEAD + 1 + len(WEEKS)):
                    is_week = lead > MAX_LEAD
                    span = WEEKS[lead - MAX_LEAD - 1] if is_week else (lead, lead)
                    y = weekly[lead - MAX_LEAD - 1] if is_week else daily[lead - 1]
                    if y is None:
                        continue
                    first_day = t + timedelta(days=span[0])
                    season = SEASON[first_day.month]
                    shift = []
                    for k in range(span[0], span[1] + 1):
                        d = t + timedelta(days=k)
                        c = self.climo(v, d.year, slot(d))
                        n = self.normal(v, d.year, slot(d))
                        if c is not None and n is not None:
                            shift.append(c[0] - n)
                    if not shift:
                        continue
                    normal_pred = sum(shift) / len(shift)  # plain normal, as an anomaly vs trend normal
                    err["normal"][lead - 1] += (y - normal_pred) ** 2
                    err["trend"][lead - 1] += y**2
                    count[lead - 1] += 1
                    key = span if is_week else lead
                    for m in models:
                        p = apply(self.coef[(v, m, lead, season)], self._x(m, f, key))
                        err[m][lead - 1] += (y - p) ** 2
                        if m == best_model:
                            resid.setdefault((lead, season), []).append(y - p)
        skill = {
            m: [1 - e[i] / err["normal"][i] if err["normal"][i] else 0.0 for i in range(len(e))] for m, e in err.items()
        }
        vs_trend = {
            m: [1 - e[i] / err["trend"][i] if err["trend"][i] else 0.0 for i in range(len(e))] for m, e in err.items()
        }
        # ranges from training residuals; coverage checked on the test years
        train_resid: dict[tuple[int, int], list[float]] = {}
        for v in VARS:
            for t, f, daily, _weekly in samples[(v, "train")]:
                for lead in range(1, MAX_LEAD + 1):
                    y = daily[lead - 1]
                    if y is None:
                        continue
                    season = SEASON[(t + timedelta(days=lead)).month]
                    p = apply(self.coef[(v, best_model, lead, season)], self._x(best_model, f, lead))
                    train_resid.setdefault((lead, season), []).append(y - p)
        for (lead, season), values in train_resid.items():
            self.spread[("", lead, season)] = (quantile(values, 0.1), quantile(values, 0.9))
        for (lead, season), values in resid.items():
            if lead > MAX_LEAD:
                continue
            lo, hi = self.spread.get(("", lead, season), (0.0, 0.0))
            covered += sum(1 for r in values if lo <= r <= hi)
            tested += len(values)
        useful = 0
        for lead in range(1, MAX_LEAD + 1):
            if vs_trend[best_model][lead - 1] >= 0.03:
                useful = lead
            else:
                break
        weekly = {m: s[MAX_LEAD:] for m, s in skill.items()}
        # the method that would be used: best average skill over weeks 3–8
        chosen = max(weekly, key=lambda m: sum(weekly[m][2:]) / len(weekly[m][2:]))
        self.report = Report(
            place=label,
            test_years=(TRAIN[1] + 1, self.y1),
            issues=len(test_issues),
            daily={m: s[:MAX_LEAD] for m, s in skill.items()},
            weekly=weekly,
            weekly_vs_trend={m: s[MAX_LEAD:] for m, s in vs_trend.items()},
            coverage=covered / tested if tested else 0.0,
            useful_days=useful,
            best=chosen,
            enso=use_oni,
        )
        return self.report


def same_day(t: date, y: int) -> date:
    try:
        return t.replace(year=y)
    except ValueError:
        return date(y, 2, 28)
