#!/usr/bin/env python3
"""살랑 — 오늘의 날씨와 계절이 머무는 터미널 정원."""

import argparse
from bisect import bisect_left
import datetime
from functools import lru_cache
import http.client
import json
import math
import os
from pathlib import Path
import queue
import random
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
import urllib.parse
import urllib.request

from regions import find_region, saved_region, save_region, select_region
from seasonal import draw_accents, draw_fog
from terminal_ui import FOREGROUND, MUTED, color_escape, fit_text, hint_line, read_key, screen_frame, styled_line, text_width


VERSION = "0.1.2"
SEASONS = ("spring", "summer", "autumn", "winter")
WEATHERS = ("clear", "wind", "rain", "snow", "cloudy")
SEASON_LABELS = dict(zip(SEASONS, ("봄", "여름", "가을", "겨울")))
WEATHER_LABELS = dict(zip(WEATHERS, ("맑음", "바람", "비", "눈", "흐림")))
STATUS_LABELS = {
    "loading": "날씨 불러오는 중(open-meteo.com)",
    "live": "실시간(open-meteo.com) · 15분마다 갱신",
    "manual": "직접 선택 · l 키로 실시간 복귀",
    "offline-last": "연결 끊김(open-meteo.com) · 마지막으로 확인한 날씨",
    "offline-demo": "날씨 연결 안 됨(open-meteo.com) · 임시 장면",
}
DEMO = (("spring", "wind"), ("summer", "rain"),
        ("autumn", "wind"), ("winter", "snow"))
PALETTES = {
    "spring": ((237, 157, 185), (255, 196, 213), (196, 117, 155), (245, 216, 224)),
    "summer": ((81, 152, 105), (134, 192, 124), (49, 112, 85), (180, 212, 136)),
    "autumn": ((222, 131, 77), (239, 181, 89), (185, 82, 64), (228, 153, 91)),
    "winter": ((188, 212, 225), (226, 236, 241), (142, 171, 191), (208, 225, 232)),
}
BARK = (155, 136, 119)
GROUND = (85, 109, 96)
RAIN = (92, 138, 166)
CLOUD = (107, 124, 139)
SNOW = (216, 231, 241)
SUN = (240, 192, 104)
SWING_ROPE = (199, 184, 145)
SWING_SEAT = (201, 146, 94)
SNOWMAN_SHADE = (160, 185, 199)
SNOWMAN_SCARF = (187, 91, 105)
SNOWMAN_NOSE = (234, 152, 78)
SNOWMAN_HAT = (91, 114, 132)
BITS = ((1, 8), (2, 16), (4, 32), (64, 128))
DEFAULT_LOCATION = "서울 강남구"
FRAME_INTERVAL = 0.2
WEATHER_REFRESH_INTERVAL = 900
WEATHER_RETRY_INTERVALS = (3, 10, 30, 60)
MAX_WEATHER_RESPONSE_BYTES = 64 * 1024


class Canvas:
    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows
        self.max_x, self.max_y = columns * 2 - 1, rows * 4 - 1
        self.pixels = {}

    def point(self, x, y, color):
        px = round(x / 160 * self.max_x)
        py = round(y / 112 * self.max_y)
        if 0 <= px <= self.max_x and 0 <= py <= self.max_y:
            self.pixels[px, py] = color

    def line(self, x1, y1, x2, y2, color, radius=0):
        steps = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) * 2))
        for i in range(steps + 1):
            x = x1 + (x2 - x1) * i / steps
            y = y1 + (y2 - y1) * i / steps
            self.point(x, y, color)
            if radius:
                for dx, dy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius)):
                    self.point(x + dx, y + dy, color)

    def sun(self):
        cx = round(130 / 160 * (self.columns * 2 - 1))
        cy = round(15 / 112 * (self.rows * 4 - 1))
        radius = max(2, round(4 * min((self.columns * 2 - 1) / 160, (self.rows * 4 - 1) / 112)))
        dots = {(dx, dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)
                if dx * dx + dy * dy <= radius * radius}
        for direction in range(8):
            angle = direction * math.pi / 4
            for distance in range(radius + 3, radius + 6):
                dots.add((round(math.cos(angle) * distance), round(math.sin(angle) * distance)))
        for dx, dy in dots:
            if 0 <= cx + dx < self.columns * 2 and 0 <= cy + dy < self.rows * 4:
                self.pixels[cx + dx, cy + dy] = SUN

    def cells(self):
        occupied = {}
        for (x, y), color in self.pixels.items():
            if not color or not (0 <= x <= self.max_x and 0 <= y <= self.max_y):
                continue
            position, rank = (y // 4, x // 2), (y % 4) * 2 + x % 2
            cell = occupied.get(position)
            if cell is None:
                cell = occupied[position] = [0, {}]
            cell[0] |= BITS[y % 4][x % 2]
            counts = cell[1].get(color)
            if counts is None:
                cell[1][color] = [1, -rank]
            else:
                counts[0] += 1
                if -rank > counts[1]:
                    counts[1] = -rank
        result = [[(" ", None)] * self.columns for _ in range(self.rows)]
        for (row, col), (mask, colors) in occupied.items():
            result[row][col] = (chr(0x2800 + mask), max(colors, key=colors.get))
        return result


def geometry():
    rng, branches, leaves = random.Random(71), [], []

    def branch(x, y, length, angle, depth):
        end_x, end_y = x + math.cos(angle) * length, y + math.sin(angle) * length
        branches.append((x, y, end_x, end_y, depth))
        if depth <= 2:
            for _ in range(72):
                theta, radius = rng.uniform(0, math.tau), math.sqrt(rng.random())
                leaves.append((end_x + math.cos(theta) * radius * 7.5,
                               end_y + math.sin(theta) * radius * 5.2,
                               rng.randrange(4), rng.random()))
        if depth == 0:
            return
        for direction in (-1, 1):
            spread = rng.uniform(0.37, 0.7)
            branch(end_x, end_y, length * rng.uniform(0.69, 0.81),
                   angle + spread * direction, depth - 1)

    branch(80, 96, 25, -math.pi / 2 - 0.05, 5)
    return branches, leaves


BRANCHES, LEAVES = geometry()
SWING_BRANCH = next(branch for branch in BRANCHES
                    if branch[0] > 95 and branch[4] == 2 and abs(branch[3] - branch[1]) < 1)


def falling_sources():
    sides = [sorted(leaf for leaf in LEAVES if leaf[0] < 80),
             sorted(leaf for leaf in LEAVES if leaf[0] >= 80)]
    result = []
    for index in range(18):
        for side in sides:
            x = side[0][0] + (index + 0.5) / 18 * (side[-1][0] - side[0][0])
            result.append(side[min(len(side) - 1, bisect_left(side, (x,)))])
    return tuple(result)


FALL_SOURCES = falling_sources()


def season_for(month, latitude=37):
    if latitude < 0:
        month = (month + 5) % 12 + 1
    return ("winter", "spring", "summer", "autumn")[(month % 12) // 3]


def parse_weather(payload, latitude):
    current = payload["current"]
    code, temperature, wind = (current[key] for key in
                               ("weather_code", "temperature_2m", "wind_speed_10m"))
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in (code, temperature, wind)):
        raise ValueError("Weather API returned invalid numeric values")
    if code in (71, 73, 75, 77, 85, 86):
        weather = "snow"
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 97, 99):
        weather = "rain"
    elif code in (2, 3, 45, 48):
        weather = "cloudy"
    elif code in (0, 1):
        weather = "clear"
    else:
        raise ValueError("Weather API returned an unsupported weather code")
    if weather == "clear" and wind >= 20:
        weather = "wind"
    month = datetime.datetime.fromisoformat(current["time"]).month
    return season_for(month, latitude), weather, temperature


def fetch_weather(latitude, longitude):
    query = urllib.parse.urlencode({
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "timezone": "auto", "wind_speed_unit": "kmh", "temperature_unit": "celsius",
    })
    request = urllib.request.Request("https://api.open-meteo.com/v1/forecast?" + query,
                                     headers={"User-Agent": "salang/" + VERSION})
    with urllib.request.urlopen(request, timeout=6) as response:
        body = response.read(MAX_WEATHER_RESPONSE_BYTES + 1)
    if len(body) > MAX_WEATHER_RESPONSE_BYTES:
        raise ValueError("Weather API response is too large")
    try:
        payload = json.loads(body)
    except RecursionError as error:
        raise ValueError("Weather API JSON nesting is too deep") from error
    return parse_weather(payload, latitude)


def resolve_location(name):
    selected = find_region(name)
    if selected is None:
        raise ValueError("지역 목록에 없습니다. 목록에서 선택하거나 --latitude와 --longitude로 좌표를 지정해 주세요.")
    return selected.latitude, selected.longitude


def shortcut_line(columns, mode):
    hints = [("s", "계절"), ("w", "날씨"), ("l", "실시간"), ("r", "지역"), ("Space", "멈춤/재개"), ("q", "종료")]
    return hint_line(hints, columns, mode)


def draw_swing(canvas, elapsed, anchors):
    angle = math.sin(elapsed * 1.15) * 0.23
    dx, dy = math.sin(angle) * 29, math.cos(angle) * 29 + 10
    seat_y = (anchors[0][1] + anchors[1][1]) / 2 + dy
    for x, y in anchors:
        canvas.line(x, y, x + dx, seat_y, SWING_ROPE)
    canvas.line(anchors[0][0] + dx - 1.2, seat_y,
                anchors[1][0] + dx + 1.2, seat_y, SWING_SEAT, radius=0.45)


@lru_cache(maxsize=32)
def snowman_shape(columns, rows, scarf_pose=0, snow_cover=0):
    canvas = Canvas(columns, rows)

    def ball(x, y, radius_x, radius_y):
        cx, cy = round(x / 160 * canvas.max_x), round(y / 112 * canvas.max_y)
        rx = max(2, round(radius_x / 160 * canvas.max_x))
        ry = max(2, round(radius_y / 112 * canvas.max_y))
        for dy in range(-ry, ry + 1):
            for dx in range(-rx, rx + 1):
                if (dx / rx) ** 2 + (dy / ry) ** 2 <= 1:
                    color = SNOWMAN_SHADE if dx > 0 and dx / rx + dy / ry > 0.7 else SNOW
                    canvas.pixels[cx + dx, cy + dy] = color
        return cx, cy, rx, ry

    for x1, y1, x2, y2 in ((109, 87, 102, 83), (102, 83, 101, 80), (102, 83, 99.5, 83),
                            (117, 87, 123, 83), (123, 83, 124, 80), (123, 83, 125.5, 83)):
        canvas.line(x1, y1, x2, y2, BARK)
    bx, by, _, _ = ball(113, 94, 5.9, 5.2)
    tx, ty, _, _ = ball(113, 87, 4.3, 3.9)
    hx, hy, rx, ry = ball(113, 81, 3.3, 3.3)

    brim = hy - ry
    for y in range(brim - max(3, round(3 / 112 * canvas.max_y)), brim):
        for x in range(hx - max(1, rx - 1), hx + max(1, rx - 1) + 1):
            canvas.pixels[x, y] = SNOWMAN_HAT
    for x in range(hx - rx - 1, hx + rx + 2):
        canvas.pixels[x, brim] = SNOWMAN_HAT
    neck = hy + ry - 1
    for y in range(neck, neck + 3):
        for x in range(hx - rx, hx + rx + 1):
            canvas.pixels[x, y] = SNOWMAN_SCARF
    for y in range(neck + 2, neck + 6):
        shift = round(scarf_pose * (y - neck - 1) / 4)
        for x in range(hx + rx - 1, hx + rx + 1):
            canvas.pixels[x + shift, y] = SNOWMAN_SCARF
    if snow_cover:
        roof = brim - max(3, round(3 / 112 * canvas.max_y))
        for y in range(roof - snow_cover + 1, roof + 1):
            for x in range(hx - max(1, rx - 1), hx + max(1, rx - 1) + 1):
                canvas.pixels[x, y] = SNOW
        for side in (-1, 1):
            for dx in range(snow_cover + 1):
                canvas.pixels[hx + side * (rx + dx), neck + 2] = SNOW
    for dx in range(rx - 1, rx + 4):
        for dy in range(2 if dx <= rx + 1 else 1):
            canvas.pixels[hx + dx, hy + dy] = SNOWMAN_NOSE
    cutouts = [(hx - max(1, rx // 2), hy - 1), (hx + max(1, rx // 2), hy - 1),
               (tx, ty + 1), (bx, by - 1), (bx, by + 2)]
    if ry >= 4:
        cutouts.extend(((hx - 1, hy + 1), (hx, hy + 2), (hx + 1, hy + 1)))
    for point in cutouts:
        canvas.pixels.pop(point, None)
    inside = lambda point: 0 <= point[0] <= canvas.max_x and 0 <= point[1] <= canvas.max_y
    return {point: color for point, color in canvas.pixels.items() if inside(point)}, tuple(filter(inside, cutouts))


def draw_snowman(canvas, elapsed=0, weather="clear"):
    scarf_pose = round(math.sin(elapsed * 2) * 2) if weather == "wind" else 0
    snow_cover = min(2, int(max(0, elapsed) / 12)) if weather == "snow" else 0
    pixels, cutouts = snowman_shape(canvas.columns, canvas.rows, scarf_pose, snow_cover)
    canvas.pixels.update(pixels)
    for point in cutouts:
        canvas.pixels.pop(point, None)


def scene(season, weather, elapsed, columns=76, rows=28):
    canvas, rng = Canvas(columns, rows), random.Random(11)
    palette = PALETTES[season]
    breeze = 2.8 if weather == "wind" else 0.65

    def sway(x, y):
        return x + math.sin(elapsed * 0.85 + y / 27) * breeze * max(0, (96 - y) / 90)

    if weather == "clear":
        canvas.sun()
    elif weather in ("cloudy", "rain", "snow"):
        for cx, cy in ((27, 14), (124, 18)):
            for _ in range(90):
                x, y = rng.uniform(-13, 13), rng.uniform(-4, 4)
                if (x / 13) ** 2 + (y / 4) ** 2 < 1:
                    canvas.point(cx + x + math.sin(elapsed * 0.14) * 2, cy + y, CLOUD)

    if weather == "cloudy":
        draw_fog(canvas, elapsed)

    for x1, y1, x2, y2, depth in BRANCHES:
        canvas.line(sway(x1, y1), y1, sway(x2, y2), y2, BARK, depth * 0.24)
        if season == "winter":
            cover = min(1, 0.2 + elapsed / 35) if weather == "snow" else 0.35
            if depth <= 3 and rng.random() < cover:
                canvas.line(sway(x1, y1), y1 - 1.1,
                            sway(x2, y2), y2 - 1.1, SNOW, cover * 0.55)

    if season != "winter":
        density = {"spring": 0.82, "summer": 1, "autumn": 0.66}[season]
        for x, y, index, chance in LEAVES:
            if chance < density:
                canvas.point(sway(x, y), y, palette[index])

    for _ in range(180):
        x = rng.uniform(31, 129)
        y = 98 + rng.uniform(-1, 2) + ((x - 80) / 48) ** 2 * 1.5
        canvas.point(x, y, SNOW if season == "winter" else GROUND)

    if season == "summer" and weather == "clear":
        x1, y1, x2, y2, _ = SWING_BRANCH
        draw_swing(canvas, elapsed, ((sway(x1, y1), y1), (sway(x2, y2), y2)))

    if season in ("spring", "autumn"):
        for i, (source_x, source_y, index, _) in enumerate(FALL_SOURCES):
            fall = (elapsed * (3.5 + (i // 2 % 4) * 0.5) + i * 7.1) % (99 - source_y)
            y = source_y + fall
            x = source_x + math.sin(elapsed * 1.2 + i) * 2 + fall * breeze * 0.11
            canvas.point(x, y, palette[index])
            if season == "autumn":
                canvas.point(x + 0.6, y + 0.7, palette[index])
        for i in range(95):
            canvas.point(rng.uniform(37, 128), rng.uniform(97, 101), palette[i % 4])

    if weather == "rain":
        for i in range(68):
            x = (i * 31.7 + elapsed * 5) % 156 + 2
            y = (i * 17.3 + elapsed * (37 + i % 9)) % 99
            canvas.line(x, y, x - 0.8, min(99, y + 2.6), RAIN)
    elif weather == "snow":
        for i in range(63):
            x = (i * 37.1 + math.sin(elapsed * 0.6 + i) * 2 + elapsed * 0.6) % 156 + 2
            y = (i * 21.3 + elapsed * (3 + i % 4)) % 99
            canvas.point(x, y, SNOW)
            if i % 8 == 0:
                canvas.point(x - 0.8, y, SNOW)
                canvas.point(x + 0.8, y, SNOW)
                canvas.point(x, y - 0.8, SNOW)
                canvas.point(x, y + 0.8, SNOW)
    elif weather == "wind":
        for i in range(10):
            x = (elapsed * 11 + i * 17.7) % 156
            y = 26 + i * 6.4 + math.sin(elapsed + i) * 2
            for dx in range(4):
                canvas.point(x + dx, y + math.sin(dx / 2) * 0.5, CLOUD)
    draw_accents(canvas, season, weather, elapsed)
    if season == "winter":
        draw_snowman(canvas, elapsed, weather)
    return canvas


def render(canvas, mode):
    lines = []
    for row in canvas.cells():
        parts, previous = [], None
        for char, color in row:
            if color and color != previous:
                parts.append(color_escape(color, mode))
                previous = color
            parts.append(char)
        lines.append("".join(parts) + ("\033[0m" if mode != "none" else ""))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(prog="salang", description=__doc__,
                                     epilog="날씨: https://open-meteo.com/ · CC BY 4.0: https://creativecommons.org/licenses/by/4.0/")
    parser.add_argument("--version", action="version", version=f"salang {VERSION}")
    parser.add_argument("--licenses", action="store_true", help="데이터 출처와 포함된 구성 요소의 라이선스 표시")
    parser.add_argument("--season", choices=SEASONS)
    parser.add_argument("--weather", choices=WEATHERS)
    parser.add_argument("--demo", action="store_true", help="사계절 장면을 차례로 미리보기")
    parser.add_argument("--snapshot", action="store_true", help="정지 화면 한 장 출력")
    parser.add_argument("--seconds", type=float, default=0, help="지정한 초 뒤 자동 종료")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--live", action="store_true", help="15분마다 실제 날씨 갱신")
    parser.add_argument("--setup", action="store_true", help="시·도, 시·군·구, 읍·면·동을 선택하고 기본 지역으로 저장")
    parser.add_argument("--location", "--city", nargs="?", const="", help="지역 이름. 값 없이 사용하면 지역 선택 메뉴를 엽니다")
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    args = parser.parse_args()
    if args.licenses:
        notice = Path(__file__).with_name("THIRD_PARTY_NOTICES.md")
        print(notice.read_text(encoding="utf-8") if notice.exists() else "https://github.com/100space/salang")
        for license_file in sorted(Path(__file__).with_name("licenses").glob("*.txt")):
            print("\n" + license_file.name + "\n" + license_file.read_text(encoding="utf-8"))
        return
    region_notice = ""
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds must be a finite, nonnegative number")
    if (args.latitude is None) != (args.longitude is None):
        parser.error("provide both --latitude and --longitude")
    for value, limit, name in ((args.latitude, 90, "latitude"), (args.longitude, 180, "longitude")):
        if value is not None and (not math.isfinite(value) or not -limit <= value <= limit):
            parser.error(f"--{name} must be between {-limit} and {limit}")
    if args.live and args.demo:
        parser.error("choose either --live or --demo")
    if args.setup or args.location == "":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("지역 선택 메뉴는 터미널에서 salang --setup으로 열어 주세요.")
        if args.latitude is not None:
            parser.error("지역 선택 메뉴와 위도·경도 직접 지정을 함께 사용할 수 없습니다.")
        try:
            selected = select_region(no_color=args.no_color)
            args.location = selected.label
            args.latitude, args.longitude = selected.latitude, selected.longitude
            try:
                save_region(selected)
                print(f"기본 지역을 저장했습니다: {selected.label}")
            except OSError as error:
                print(f"지역을 저장하지 못해 이번 실행에만 적용합니다: {error}", file=sys.stderr)
        except (KeyboardInterrupt, EOFError):
            print("\n지역 설정을 취소했습니다.")
            return
        except (OSError, ValueError) as error:
            parser.error(f"지역 목록을 읽지 못했습니다: {error}")
    elif args.location is None and args.latitude is None:
        selected = saved_region()
        if selected is not None:
            args.location = selected.label
            args.latitude, args.longitude = selected.latitude, selected.longitude
    args.live = args.live or not (args.demo or args.season or args.weather)
    location = args.location or (DEFAULT_LOCATION if args.latitude is None else
                                 f"{args.latitude:.4f}, {args.longitude:.4f}")
    if args.live and args.latitude is None:
        try:
            args.latitude, args.longitude = resolve_location(location)
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(f"지역을 찾지 못했습니다: {error} · salang --setup으로 목록에서 선택할 수 있습니다.")
    season = args.season or season_for(datetime.date.today().month, args.latitude or 0)
    weather = args.weather or "clear"
    status, has_reading, paused = ("loading" if args.live else "manual"), False, False
    updates = queue.SimpleQueue()
    last_weather, weather_pending, weather_generation, next_refresh = None, False, 0, 0.0
    retry_index = 0
    interactive = sys.stdout.isatty() and sys.stdin.isatty() and os.environ.get("TERM") != "dumb"
    mode = "none" if args.no_color or "NO_COLOR" in os.environ or not sys.stdout.isatty() else (
        "rgb" if os.environ.get("COLORTERM") in ("truecolor", "24bit") else "256")

    def frame(elapsed, size):
        nonlocal season, weather
        if args.demo:
            season, weather = DEMO[int(elapsed / 12) % len(DEMO)]
        columns, rows = min(90, max(1, size.columns - 2)), min(32, max(1, size.lines - 6))
        if size.columns < 26 or size.lines < 12:
            return fit_text("창을 키워 주세요", columns)
        temperature = f"{last_weather[2]:.1f}°C" if args.live and last_weather else ""
        details = " · ".join(value for value in (temperature, WEATHER_LABELS[weather], SEASON_LABELS[season]) if value)
        header = styled_line((("  " + location, FOREGROUND), ("  │  ", MUTED), (details, FOREGROUND)), columns, mode)
        state = "계절 미리보기 · l 키로 실시간 전환" if args.demo else STATUS_LABELS[status]
        if paused:
            state += " · 일시정지"
        if region_notice:
            state += " · " + region_notice
        state = styled_line((("  " + state, MUTED),), columns, mode)
        if args.live and mode != "none":
            state = "\033]8;;https://open-meteo.com/\033\\" + state + "\033]8;;\033\\"
        return (header + "\n" + state + "\n" + render(scene(season, weather, elapsed, columns, rows), mode)
                + "\n" + shortcut_line(columns, mode))

    def apply_weather(result):
        nonlocal season, weather, status, has_reading, last_weather
        if result is None:
            status = "offline-last" if has_reading else "offline-demo"
            return
        local_season, weather, _ = result
        if not args.season:
            season = local_season
        status, has_reading = "live", True
        last_weather = result

    def fetch_update(generation, latitude, longitude, requested_location):
        result = None
        try:
            if latitude is None:
                latitude, longitude = resolve_location(requested_location)
            result = fetch_weather(latitude, longitude)
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            pass
        finally:
            updates.put((generation, latitude, longitude, result, time.monotonic()))

    if args.snapshot or not interactive:
        if args.live:
            try:
                apply_weather(fetch_weather(args.latitude, args.longitude))
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                apply_weather(None)
        print(frame(6, shutil.get_terminal_size((80, 35))))
        return

    old_settings = termios.tcgetattr(sys.stdin.fileno())
    old_sigterm = signal.getsignal(signal.SIGTERM)
    termination_requested = False
    def stop(_signum, _frame):
        nonlocal termination_requested
        termination_requested = True
        raise KeyboardInterrupt

    started, previous, elapsed, paused = time.monotonic(), time.monotonic(), 0.0, False
    redraw, last_size, last_frame, next_frame = True, None, None, started
    try:
        signal.signal(signal.SIGTERM, stop)
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write("\033[?1049h\033[?25l")
        while True:
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            if not paused:
                elapsed += now - previous
            previous = now
            try:
                generation, latitude, longitude, result, completed_at = updates.get_nowait()
                weather_pending = False
                if args.live and generation == weather_generation:
                    args.latitude, args.longitude = latitude, longitude
                    apply_weather(result)
                    if result is None:
                        next_refresh = completed_at + WEATHER_RETRY_INTERVALS[retry_index]
                        retry_index = min(retry_index + 1, len(WEATHER_RETRY_INTERVALS) - 1)
                    else:
                        next_refresh = completed_at + WEATHER_REFRESH_INTERVAL
                        retry_index = 0
                    redraw = True
            except queue.Empty:
                pass
            if args.live and not paused and not weather_pending and now >= next_refresh:
                weather_pending = True
                threading.Thread(target=fetch_update, daemon=True,
                                 args=(weather_generation, args.latitude, args.longitude, location)).start()
            size = shutil.get_terminal_size((80, 35))
            if redraw or size != last_size or (not paused and now >= next_frame):
                current_frame = frame(elapsed, size)
                output = screen_frame(current_frame, last_frame if size == last_size else None)
                if output:
                    sys.stdout.write(output)
                    sys.stdout.flush()
                redraw, last_size, last_frame = False, size, current_frame
                next_frame = now + FRAME_INTERVAL
            timeout = 1.0 if paused else max(0, next_frame - time.monotonic())
            if args.seconds:
                timeout = min(timeout, max(0, started + args.seconds - time.monotonic()))
            if select.select([sys.stdin], [], [], timeout)[0]:
                key = read_key(sys.stdin.fileno())
                if key == "q":
                    break
                if key in (" ", "s", "w", "l", "r"):
                    redraw = True
                if key == " ":
                    paused = not paused
                    if not paused and args.live:
                        weather_generation += 1
                        next_refresh, retry_index, status = 0.0, 0, "loading"
                elif key in ("s", "w"):
                    args.demo = False
                    args.live, status = False, "manual"
                    weather_generation += 1
                    if key == "s":
                        season = SEASONS[(SEASONS.index(season) + 1) % len(SEASONS)]
                        args.season = season
                    else:
                        weather = WEATHERS[(WEATHERS.index(weather) + 1) % len(WEATHERS)]
                elif key == "l" and (not args.live or args.season is not None):
                    args.demo = False
                    args.live, args.season, paused = True, None, False
                    weather_generation += 1
                    next_refresh, retry_index = 0.0, 0
                    if last_weather is not None:
                        apply_weather(last_weather)
                    else:
                        season = season_for(datetime.date.today().month, args.latitude or 0)
                        weather = "clear"
                    status = "loading"
                elif key == "r":
                    sys.stdout.write("\033[0m\033[?25h\033[?1049l")
                    sys.stdout.flush()
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
                    selected = None
                    try:
                        selected = select_region(no_color=args.no_color)
                        region_notice = ""
                        try:
                            save_region(selected)
                        except OSError:
                            region_notice = "지역 저장 실패 · 이번 실행만 적용"
                    except (KeyboardInterrupt, EOFError):
                        if termination_requested:
                            raise
                    except (OSError, ValueError):
                        region_notice = "지역 목록을 읽지 못했습니다"
                    finally:
                        tty.setcbreak(sys.stdin.fileno())
                        sys.stdout.write("\033[?1049h\033[?25l")
                        sys.stdout.flush()
                        previous = time.monotonic()
                        last_frame = None
                    if selected is not None:
                        location = args.location = selected.label
                        args.latitude, args.longitude = selected.latitude, selected.longitude
                        args.demo, args.live, args.season, paused = False, True, None, False
                        weather_generation += 1
                        retry_index = 0
                        next_refresh, last_weather, has_reading = 0.0, None, False
                        season = season_for(datetime.date.today().month, selected.latitude)
                        weather, status = "clear", "loading"
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        signal.signal(signal.SIGTERM, old_sigterm)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
