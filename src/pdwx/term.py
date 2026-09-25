"""Raw terminal I/O: truecolor canvas, key decoding, and theme colours.

Curses cannot draw 24-bit colour or react to Esc quickly, so pdwx owns the
terminal directly.
"""

from __future__ import annotations

import os
import re
import select
import signal
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass, field

RGB = tuple[int, int, int]

ESC_TIMEOUT = 0.025

_SEQUENCES = {
    "[A": "up",
    "[B": "down",
    "[C": "right",
    "[D": "left",
    "OA": "up",
    "OB": "down",
    "OC": "right",
    "OD": "left",
    "[H": "home",
    "[F": "end",
    "OH": "home",
    "OF": "end",
    "[1~": "home",
    "[4~": "end",
    "[7~": "home",
    "[8~": "end",
    "[2~": "insert",
    "[3~": "delete",
    "[5~": "pgup",
    "[6~": "pgdn",
    "[Z": "btab",
    "OP": "f1",
    "[11~": "f1",
    "[[A": "f1",
    "[15~": "f5",
    "[[E": "f5",
}
_SEQ_END = re.compile(r"(\[[0-9;]*[~A-Za-z]|\[\[[A-E]|O[A-Za-z])")


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def char_width(ch: str) -> int:
    if unicodedata.combining(ch) or ch in "‍︎️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def clip(text: str, width: int) -> str:
    """Cut text to a display width, ending in an ellipsis when shortened."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


# ── colour ──────────────────────────────────────────────────────────────


def mix(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def stops(points: list[tuple[float, RGB]], value: float) -> RGB:
    if value <= points[0][0]:
        return points[0][1]
    for (v0, c0), (v1, c1) in zip(points, points[1:], strict=False):
        if value <= v1:
            return mix(c0, c1, (value - v0) / (v1 - v0))
    return points[-1][1]


def luminance(c: RGB) -> float:
    def channel(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink_for(bg: RGB) -> RGB:
    """Dark or light text, whichever reads better on a filled cell."""
    return (24, 24, 28) if luminance(bg) > 0.22 else (238, 238, 238)


_CUBE = (0, 95, 135, 175, 215, 255)


def _to_256(c: RGB) -> int:
    def nearest(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    r, g, b = (nearest(v) for v in c)
    cube = 16 + 36 * r + 6 * g + b
    grey = max(0, min(23, round((sum(c) / 3 - 8) / 10)))
    grey_rgb = 8 + grey * 10
    cube_rgb = (_CUBE[r], _CUBE[g], _CUBE[b])
    d_cube = sum((cube_rgb[i] - c[i]) ** 2 for i in range(3))
    d_grey = sum((grey_rgb - c[i]) ** 2 for i in range(3))
    return 232 + grey if d_grey < d_cube else cube


TRUECOLOR = (
    os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    or any(name in os.environ.get("TERM", "") for name in ("ghostty", "kitty", "alacritty", "foot", "wezterm"))
    or os.environ.get("PDWX_TRUECOLOR") == "1"
)


def sgr_fg(c: RGB | None) -> str:
    if c is None:
        return "39"
    return f"38;2;{c[0]};{c[1]};{c[2]}" if TRUECOLOR else f"38;5;{_to_256(c)}"


def sgr_bg(c: RGB | None) -> str:
    if c is None:
        return "49"
    return f"48;2;{c[0]};{c[1]};{c[2]}" if TRUECOLOR else f"48;5;{_to_256(c)}"


# ── theme ───────────────────────────────────────────────────────────────

# Neutral dark palette for terminals that do not report their colours
DEFAULT_ANSI: tuple[RGB, ...] = (
    (30, 32, 36),
    (224, 108, 117),
    (152, 195, 121),
    (229, 192, 123),
    (97, 175, 239),
    (198, 120, 221),
    (86, 182, 194),
    (200, 204, 212),
    (92, 99, 112),
    (224, 108, 117),
    (152, 195, 121),
    (229, 192, 123),
    (97, 175, 239),
    (198, 120, 221),
    (86, 182, 194),
    (230, 232, 236),
)


@dataclass
class Theme:
    bg: RGB = (30, 32, 36)
    fg: RGB = (200, 204, 212)
    ansi: list[RGB] = field(default_factory=lambda: list(DEFAULT_ANSI))
    source: str = "default"


def _hex(value: str) -> RGB | None:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return None


def _osc_rgb(body: str) -> RGB | None:
    match = re.search(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", body)
    if not match:
        return None
    return tuple(int(part[:2].ljust(2, part[0]), 16) for part in match.groups())  # type: ignore[return-value]


# ── terminal ────────────────────────────────────────────────────────────


class Terminal:
    """Alternate screen, raw keyboard, resize flag. Use as a context manager."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout
        self._old: list | None = None
        self._pending = b""
        self.resized = True
        self._old_winch = None

    def __enter__(self) -> Terminal:
        self._old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self._old_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        self.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J")
        return self

    def __exit__(self, *exc: object) -> None:
        self.write("\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        if self._old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)
        if self._old_winch is not None:
            signal.signal(signal.SIGWINCH, self._old_winch)

    def suspend(self) -> None:
        self.__exit__()
        os.kill(os.getpid(), signal.SIGTSTP)
        self.__enter__()
        self.resized = True

    def _on_winch(self, *_: object) -> None:
        self.resized = True

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def size(self) -> tuple[int, int]:
        try:
            cols, rows = os.get_terminal_size(self.out.fileno())
        except OSError:
            cols, rows = 80, 24
        return rows, cols

    def _read(self, timeout: float) -> bytes:
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except InterruptedError:
            return b""
        if not ready:
            return b""
        try:
            return os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return b""

    def probe_theme(self) -> Theme:
        """Ask the terminal for its colours (OSC 10/11/4); a DA1 query marks the end."""
        queries = "\x1b]10;?\x1b\\\x1b]11;?\x1b\\" + "".join(f"\x1b]4;{i};?\x1b\\" for i in range(16)) + "\x1b[c"
        self.write(queries)
        data, deadline = b"", time.monotonic() + 0.35
        while time.monotonic() < deadline:
            data += self._read(0.05)
            if re.search(rb"\x1b\[\?[0-9;]*c", data):
                break
        text = data.decode("utf-8", "replace")
        tail = re.split(r"\x1b\[\?[0-9;]*c", text, maxsplit=1)
        if len(tail) > 1:
            self._pending += tail[1].encode()
        base = Theme()
        found = 0
        for kind, body in re.findall(r"\x1b\](10|11|4;\d+);([^\x07\x1b]*)", text):
            colour = _osc_rgb(body)
            if colour is None:
                continue
            found += 1
            if kind == "10":
                base.fg = colour
            elif kind == "11":
                base.bg = colour
            else:
                index = int(kind.split(";")[1])
                if index < 16:
                    base.ansi[index] = colour
        if found:
            base.source = "terminal"
        return base

    def read_key(self, timeout: float) -> str | None:
        """Return a key name ('up', 'enter', 'esc', …) or a typed character."""
        if not self._pending:
            self._pending = self._read(timeout)
            if not self._pending:
                return None
        data = self._pending
        if data[:1] == b"\x1b":
            if len(data) == 1:
                more = self._read(ESC_TIMEOUT)
                if not more:
                    self._pending = b""
                    return "esc"
                data += more
            rest = data[1:].decode("latin-1")
            match = _SEQ_END.match(rest)
            if match:
                seq = match.group(1)
                self._pending = data[1 + len(seq) :]
                return _SEQUENCES.get(seq, "unknown")
            if rest.startswith("]"):
                end = re.search(r"(\x07|\x1b\\)", rest)
                self._pending = data[1 + (end.end() if end else len(rest)) :]
                return "unknown"
            self._pending = data[1:]
            return "esc"
        head = data[0]
        if head == 0x1A:  # Ctrl+Z: hand the terminal back, resume on fg
            self._pending = data[1:]
            self.suspend()
            return None
        simple = {
            0x0D: "enter",
            0x0A: "enter",
            0x09: "tab",
            0x7F: "backspace",
            0x08: "backspace",
            0x03: "ctrl-c",
            0x12: "ctrl-r",
            0x15: "ctrl-u",
            0x17: "ctrl-w",
        }
        if head in simple:
            self._pending = data[1:]
            return simple[head]
        if head < 0x20:
            self._pending = data[1:]
            return "unknown"
        length = 1 if head < 0x80 else 2 if head < 0xE0 else 3 if head < 0xF0 else 4
        if len(data) < length:
            data += self._read(0.01)
        chunk, self._pending = data[:length], data[length:]
        return chunk.decode("utf-8", "replace")


# ── canvas ──────────────────────────────────────────────────────────────

Cell = tuple[str, "RGB | None", "RGB | None", bool]


COMPACT_W = 90  # narrower than this, views switch to their condensed layouts


class Canvas:
    def __init__(self, height: int, width: int):
        self.h, self.w = height, width
        self.compact = width < COMPACT_W
        blank: Cell = (" ", None, None, False)
        self.cells: list[list[Cell]] = [[blank] * width for _ in range(height)]

    def put(
        self,
        y: int,
        x: int,
        text: str,
        fg: RGB | None = None,
        bg: RGB | None = None,
        bold: bool = False,
        width: int | None = None,
    ) -> int:
        """Draw text; returns the column after it. Clipped to the canvas and optional width."""
        if y < 0 or y >= self.h:
            return x
        limit = self.w if width is None else min(self.w, x + width)
        for ch in text:
            if _CONTROL.match(ch):  # never let outside text (place names, errors) emit terminal escapes
                ch = "�"
            w = char_width(ch)
            if w == 0:
                continue
            if x + w > limit:
                break
            if x >= 0:
                self.cells[y][x] = (ch, fg, bg, bold)
                if w == 2 and x + 1 < self.w:
                    self.cells[y][x + 1] = ("", fg, bg, bold)
            x += w
        return x

    def fill(self, y: int, x: int, width: int, bg: RGB | None, ch: str = " ", fg: RGB | None = None) -> None:
        for col in range(max(0, x), min(self.w, x + width)):
            if 0 <= y < self.h:
                self.cells[y][col] = (ch, fg, bg, False)

    def tint(self, y: int, x: int, width: int, bg: RGB) -> None:
        """Set a background under existing text."""
        if not 0 <= y < self.h:
            return
        for col in range(max(0, x), min(self.w, x + width)):
            ch, fg, _, bold = self.cells[y][col]
            self.cells[y][col] = (ch, fg, bg, bold)

    def render(self) -> str:
        parts = ["\x1b[H\x1b[0m"]
        for y, row in enumerate(self.cells):
            parts.append(f"\x1b[{y + 1};1H")
            state: tuple | None = None
            for ch, fg, bg, bold in row:
                if ch == "":
                    continue
                if state != (fg, bg, bold):
                    parts.append(f"\x1b[0;{sgr_fg(fg)};{sgr_bg(bg)}{';1' if bold else ''}m")
                    state = (fg, bg, bold)
                parts.append(ch)
            parts.append("\x1b[0m")
        return "".join(parts)
