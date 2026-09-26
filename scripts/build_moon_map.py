"""Build src/pdwx/moon_map.bin, the Moon's surface brightness for the Moon view.

Source: USGS Astrogeology, "Moon Clementine UVVIS 750nm Global Mosaic 118m v2" (public domain),
https://astrogeology.usgs.gov/search/map/Moon/Clementine/UVVIS/Lunar_Clementine_UVVIS_750nm_Global_Mosaic_118m_v2
It is the Moon's albedo seen under high sun, so it carries no baked-in shadows: pdwx lights it itself.

The mosaic is a 4.2 GB uncompressed BigTIFF, 92160 × 46080, simple cylindrical, longitude −180 at the
left edge, latitude +90 at the top, 0 = no data. Rows are stored back to back, so this fetches only
the rows and the longitude band it needs with HTTP range requests (about 75 MB), then box-averages
them to a grid of GRID degrees covering longitude ±LON_SPAN (the near side plus libration).

Output, zlib-compressed: an 8-byte header (b"PDMN", rows u16, cols u16), then rows × cols bytes.
Row 0 is latitude +90, column 0 is longitude −LON_SPAN. 255 = the bright highlands.

Run: python scripts/build_moon_map.py [cache-dir]
"""

from __future__ import annotations

import concurrent.futures as cf
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

URL = (
    "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/Lunar_Clementine_UVVIS_750nm_Global_Mosaic_118m_v2.tif"
)
FIRST_ROW, ROW_BYTES, SRC_W, SRC_H = 738069, 92160, 92160, 46080
PER_DEG = SRC_W / 360
GRID = 0.625  # output degrees per cell
LON_SPAN = 100  # output covers longitude −100…+100
SAMPLES = 5  # source rows averaged per output row
OUT = Path(__file__).resolve().parent.parent / "src" / "pdwx" / "moon_map.bin"


def fetch_row(row: int, col0: int, col1: int, cache: Path) -> bytes:
    path = cache / f"{row}.raw"
    if path.exists():
        return path.read_bytes()
    start = FIRST_ROW + row * ROW_BYTES + col0
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{start + (col1 - col0) - 1}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
            if len(data) == col1 - col0:
                path.write_bytes(data)
                return data
        except OSError:
            if attempt == 3:
                raise
    raise RuntimeError(f"short read on row {row}")


def main() -> None:
    cache = Path(sys.argv[1] if len(sys.argv) > 1 else "moon-cache")
    cache.mkdir(parents=True, exist_ok=True)
    rows, cols = round(180 / GRID), round(2 * LON_SPAN / GRID)
    col0 = round((180 - LON_SPAN) * PER_DEG)
    col1 = round((180 + LON_SPAN) * PER_DEG)
    step = SRC_H / rows
    wanted = sorted(
        {min(SRC_H - 1, int((r + (k + 0.5) / SAMPLES) * step)) for r in range(rows) for k in range(SAMPLES)}
    )
    print(f"{len(wanted)} rows × {col1 - col0} bytes", flush=True)
    with cf.ThreadPoolExecutor(16) as pool:
        data = dict(zip(wanted, pool.map(lambda r: fetch_row(r, col0, col1, cache), wanted), strict=True))
    per_cell = (col1 - col0) / cols
    grid: list[float | None] = []
    for r in range(rows):
        src = [data[min(SRC_H - 1, int((r + (k + 0.5) / SAMPLES) * step))] for k in range(SAMPLES)]
        for c in range(cols):
            a, b = int(c * per_cell), int((c + 1) * per_cell)
            total = count = 0
            for line in src:
                chunk = line[a:b]
                valid = len(chunk) - chunk.count(0)
                if valid:
                    total += sum(chunk)
                    count += valid
            grid.append(total / count if count else None)
    # fill any no-data cells from their row neighbours
    for i, v in enumerate(grid):
        if v is None:
            near = [grid[j] for j in (i - 1, i + 1, i - cols, i + cols) if 0 <= j < len(grid) and grid[j] is not None]
            grid[i] = sum(near) / len(near) if near else 0.0
    # Near the poles Clementine saw the ground under a low Sun, which leaves shadow streaks. There are
    # no maria up there, so lift anything darker than its row's median, fading in from 60° to 70°.
    for r in range(rows):
        lat = 90 - (r + 0.5) * GRID
        fade = min(1.0, max(0.0, (abs(lat) - 60) / 10))
        if fade:
            line = grid[r * cols : (r + 1) * cols]
            median = sorted(line)[cols // 2]  # type: ignore[type-var]
            for c, v in enumerate(line):
                if v < median:  # type: ignore[operator]
                    grid[r * cols + c] = v + (median - v) * fade  # type: ignore[operator]
    # scale so the bright highlands (98th percentile) are 255
    top = sorted(grid)[int(len(grid) * 0.98)]
    body = bytes(min(255, round(v / top * 255)) for v in grid)  # type: ignore[operator]
    OUT.write_bytes(zlib.compress(b"PDMN" + struct.pack("<HH", rows, cols) + body, 9))
    print(f"wrote {OUT} ({rows}×{cols}, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
