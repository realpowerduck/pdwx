# pdwx

**How unusual is today's weather?** pdwx puts the forecast for any place in the world next to every day since 1940, in a terminal app that takes its colours from your terminal theme.

![Week view: seven days with the normal range behind each forecast](https://raw.githubusercontent.com/realpowerduck/pdwx/main/docs/week.png)

- **Every day ranked.** "29° · +7° above normal · 3rd warmest 26 Sep in 87 years · last this warm: 3 Oct 2023".
- **Record watch.** Flags forecast days that would tie or beat the record for their date.
- **Month calendar** where past days show what actually happened, shaded by how far they were from normal.
- **Climate view.** Warming stripes for the year and the month, decade averages, hot days per decade, when the first hot day of the season arrives, rainfall stripes and dry spells.
- **Heatmap** of every day since 1940.
- **Any date across 87 years.** Press Enter on a date to see it in every year, then open a single day.
- **Compare two places** and jump to any date.
- `pdwx --line` / `--json` print a one-line summary for status bars such as Waybar.

| Month | Climate | Heatmap |
|---|---|---|
| ![Month](https://raw.githubusercontent.com/realpowerduck/pdwx/main/docs/month.png) | ![Climate](https://raw.githubusercontent.com/realpowerduck/pdwx/main/docs/climate.png) | ![Heatmap](https://raw.githubusercontent.com/realpowerduck/pdwx/main/docs/heatmap.png) |

## Install

Linux or macOS. Windows is not supported: pdwx reads the terminal directly using Unix-only interfaces. Any terminal from 60×20 up works; below 90 columns pdwx switches to a condensed layout.

```sh
uv tool install git+https://github.com/realpowerduck/pdwx
pdwx
```

[uv](https://docs.astral.sh/uv/) downloads a suitable Python (3.11 or newer) if you don't have one. With pipx instead: `pipx install git+https://github.com/realpowerduck/pdwx`. To update later: `uv tool upgrade pdwx`.

The first run opens settings: temperature (°C/°F), rain (mm/in), wind (km/h, mph, m/s, knots), week start, icons, and your home place. After that, `pdwx` opens your home place straight away. Change settings any time with `,` or `pdwx --settings`. They are saved in `~/.config/pdwx/config.toml`.

**Icons:** *Symbols* works in any font. *Nerd Font* looks best if your terminal uses a [Nerd Font](https://www.nerdfonts.com/). *ASCII* is the safest.

**Colours:** pdwx asks the terminal for its palette (dark or light) and derives everything from it. Terminals that don't answer get a neutral dark palette. Truecolor is used when `COLORTERM`/`TERM` says so (`PDWX_TRUECOLOR=1` forces it), otherwise 256 colours.

## Keys

| Key | |
|---|---|
| `Tab` / `Shift+Tab`, `1`–`5` | Week, Month, Chart, Climate, Heatmap |
| `↑↓←→`, `PgUp`/`PgDn` | move the day / month (the footer shows each view's keys) |
| `Enter` | this date across every year, then that single day |
| `Esc` | back; from a view, choose another place |
| `:` | go to a date (`14 Mar 1961`, `1990-07-02`, `march 14`) |
| `r` | temperature ↔ rain |
| `c` / `x` | compare with another place / stop |
| `,` | settings |
| `F5` | refresh the forecast |
| `?` | help · `q` quit · `Ctrl+Z` suspend |

Other options: `pdwx --location "Portland, OR"` or coordinates (`--location=45.52,-122.68`; use the `=` form when a number starts with a minus sign), `--view climate`, `--refresh-history`, `--version`.

## Where the numbers come from

- **History** is [ERA5](https://www.ecmwf.int/en/forecasts/dataset/ecmwf-reanalysis-v5) reanalysis via [Open-Meteo](https://open-meteo.com/): a modelled grid of about 25 km, not weather-station readings. Coasts, hills and cities can differ from local stations.
- **Forecast** is Open-Meteo's 16-day forecast. `H`, `R` and `F` mark archive, recent model analysis (ERA5 lags about five days) and forecast.
- **Normal** is the 1991–2020 average for the date, smoothed over ±7 days. A *hot day* is hotter than 95% of 1991–2020 days. *Dry* means under 1 mm.
- The first visit to a place downloads 1940 to last year (about 1 MB); after that only new years download. Sun, wind, humidity and feels-like history (about 1.2 MB) download only when you open a past day's details. Open-Meteo's free tier is shared by everything on your connection and weights long date ranges heavily, so allow a handful of new places a day. pdwx waits out the per-minute limit with a countdown and keeps working from its cache if the hourly or daily limit is reached.
- Cache: `~/.cache/pdwx`. Saved places: `~/.local/state/pdwx` (both follow the XDG variables).

### Can 86 years of history forecast further than 16 days?

We tested it. `pdwx --backtest` fits long-range methods on 1961–1990 and scores them on 1991 onward with no peeking. The methods are trend-adjusted normals, a fading recent anomaly, El Niño/La Niña (NOAA's ONI) and similar past years. Past the real forecast, history only tilts the odds. Coastal places gained 1–3% over the trend-adjusted normal in weeks 3–4 and nothing after that. Inland places held a small signal (about 7% in weeks 3–4, 4% by week 8). So pdwx shows the real forecast and, beyond it, the normal. Run it for your own place and see.

## Privacy

pdwx has no accounts, analytics or telemetry. It talks only to Open-Meteo, sending the text you search for and the coordinates of places you open, and, for `--backtest` only, downloads NOAA's El Niño index. Your settings, home place and saved places stay on your machine in files only you can read. Text from outside (place names, error messages) is stripped of terminal control codes before it is drawn.

## Credits

Weather data by [Open-Meteo.com](https://open-meteo.com/) under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Contains modified Copernicus Climate Change Service information (ERA5). The El Niño index used by `--backtest` is from NOAA's Climate Prediction Center. Open-Meteo's free API is for non-commercial use.

## Development

```sh
uv tool install --editable .
python -m unittest discover -s tests     # 26 tests, including every screen drawn at five terminal sizes
uvx ruff check src tests && uvx ruff format --check src tests
```

The code has no dependencies outside the standard library. `term.py` is the raw terminal and canvas, `climate.py` the statistics, `ui.py` and `views.py` the screens, and `outlook.py` the backtest. MIT licensed.
