"""Small shared terminal formatting and keyboard helpers."""

import os
import select
import sys
import unicodedata


FOREGROUND = (238, 239, 241)
MUTED = (148, 151, 157)


def color_mode():
    if "NO_COLOR" in os.environ or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return "none"
    return "rgb" if os.environ.get("COLORTERM") in ("truecolor", "24bit") else "256"


def color_escape(color, mode):
    if mode == "none":
        return ""
    r, g, b = color
    if mode == "rgb":
        return f"\033[38;2;{r};{g};{b}m"
    if max(color) - min(color) <= 16:
        gray = max(0, min(23, round((sum(color) / 3 - 8) / 10)))
        return f"\033[38;5;{232 + gray}m"
    levels = (0, 95, 135, 175, 215, 255)
    ri, gi, bi = (min(range(6), key=lambda i: abs(levels[i] - value)) for value in color)
    return f"\033[38;5;{16 + ri * 36 + gi * 6 + bi}m"


def text_width(value):
    return sum(0 if unicodedata.combining(char) else
               (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1) for char in value)


def fit_text(value, columns):
    result, width = [], 0
    for char in value:
        if unicodedata.category(char).startswith("C"):
            continue
        width += text_width(char)
        if width > columns:
            break
        result.append(char)
    return "".join(result)


def styled_line(segments, columns, mode):
    result = []
    for value, color in segments:
        visible = fit_text(value, columns)
        if visible:
            result.append(color_escape(color, mode) + visible)
            columns -= text_width(visible)
        if columns <= 0:
            break
    return "".join(result) + ("\033[0m" if mode != "none" else "")


def hint_line(hints, columns, mode):
    segments = [("  ", MUTED)]
    for key, label in hints:
        segments.extend(((key, FOREGROUND), (" " + label + "   ", MUTED)))
    return styled_line(segments, columns, mode)


def screen_frame(value, previous=None):
    lines = value.splitlines()
    old = previous.splitlines() if previous is not None else []
    updates = [f"\033[{row + 1};1H" + line + "\033[K" for row, line in enumerate(lines)
               if row >= len(old) or line != old[row]]
    if updates or len(lines) != len(old):
        updates.append(f"\033[{len(lines) + 1};1H\033[K\033[J")
    return "".join(updates)


def read_key(fd):
    value = os.read(fd, 1)
    if not value:
        raise EOFError
    if value == b"\x1b":
        if select.select([fd], [], [], 0.04)[0]:
            value += os.read(fd, 1)
        if value in (b"\x1b[", b"\x1bO"):
            while select.select([fd], [], [], 0.04)[0]:
                part = os.read(fd, 1)
                if not part:
                    raise EOFError
                value += part
                if b"@" <= part <= b"~" and value != b"\x1b[[":
                    break
    keys = {b"\x1b[A": "up", b"\x1bOA": "up", b"\x1b[B": "down", b"\x1bOB": "down",
            b"\x1b[D": "back", b"\x1b": "back", b"\x7f": "back", b"\x1b[C": "enter",
            b"\r": "enter", b"\n": "enter", b"\x1b[H": "home", b"\x1b[F": "end",
            b"\x1b[1~": "home", b"\x1b[7~": "home", b"\x1b[4~": "end", b"\x1b[8~": "end",
            b"\x1b[5~": "page-up", b"\x1b[6~": "page-down"}
    if value in (b"\x03", b"\x04"):
        raise KeyboardInterrupt
    return keys.get(value, value.decode("ascii", errors="ignore").lower() if len(value) == 1 else "")
