import fcntl
import contextlib
from collections import Counter
import http.client
import io
import json
import math
import os
import pty
import random
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

import salang
import seasonal
import regions
from terminal_ui import read_key, screen_frame
from tools.benchmark import cpu_seconds


SCRIPT = str(Path(__file__).with_name("salang.py"))
WEATHER_RESPONSE = json.dumps({"current": {
    "weather_code": 0, "temperature_2m": 12.5, "wind_speed_10m": 0,
    "time": "2026-04-18T12:00",
}}).encode()


def read_output(master, expected, output, start=0):
    deadline = time.monotonic() + 6
    while expected not in output[start:] and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            output.extend(os.read(master, 65536))
    if expected not in output[start:]:
        raise AssertionError(f"Missing {expected!r}; terminal tail: {bytes(output[-3000:])!r}")


def choice_keys(prefix, child):
    children, exact = regions.region_options(prefix)
    position = 0 if child is None else children.index(child) + bool(exact)
    return ["down"] * position + ["enter"]


def choice_bytes(prefix, child):
    return b"".join(b"\x1b[B" if key == "down" else b"\r" for key in choice_keys(prefix, child))


@contextlib.contextmanager
def simulated_menu(keys):
    with mock.patch.object(regions.termios, "tcgetattr", return_value=[]), \
            mock.patch.object(regions.termios, "tcsetattr"), mock.patch.object(regions.tty, "setcbreak"), \
            mock.patch.object(regions, "read_key", side_effect=keys), contextlib.redirect_stdout(io.StringIO()) as output:
        yield output


@contextlib.contextmanager
def simulated_weather(clock, fetch, wait, key):
    def thread(*, target, args, daemon):
        worker = mock.Mock()
        worker.start.side_effect = lambda: target(*args)
        return worker

    output, errors = io.StringIO(), io.StringIO()
    arguments = [SCRIPT, "--live", "--latitude", "37", "--longitude", "127"]
    with mock.patch.object(sys, "argv", arguments), mock.patch.object(sys.stdin, "isatty", return_value=True), \
            mock.patch.object(output, "isatty", return_value=True), mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}), \
            mock.patch.object(salang.time, "monotonic", new=lambda: clock[0]), \
            mock.patch.object(salang.select, "select", new=wait), mock.patch.object(salang, "read_key", new=lambda _: key()), \
            mock.patch.object(salang.threading, "Thread", new=thread), mock.patch.object(salang, "fetch_weather", new=fetch), \
            mock.patch.object(salang, "scene", new=lambda *_: None), mock.patch.object(salang, "render", new=lambda *_: "tree"), \
            mock.patch.object(salang.shutil, "get_terminal_size", new=lambda *_: os.terminal_size((100, 40))), \
            mock.patch.object(salang.termios, "tcgetattr", return_value=[]), mock.patch.object(salang.termios, "tcsetattr"), \
            mock.patch.object(salang.tty, "setcbreak"), mock.patch.object(salang.signal, "signal"), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        yield output, errors


class SalangChecks(unittest.TestCase):
    def test_weather_response_size_boundary_rejects_oversize_without_truncation(self):
        for size in (len(WEATHER_RESPONSE), 65536, 65537):
            with self.subTest(size=size):
                body = WEATHER_RESPONSE + b" " * (size - len(WEATHER_RESPONSE))
                response = io.BytesIO(body)
                with mock.patch.object(response, "read", wraps=response.read) as read, \
                        mock.patch.object(salang.urllib.request, "urlopen", return_value=response):
                    if size > 65536:
                        with self.assertRaisesRegex(ValueError, "too large"):
                            salang.fetch_weather(37, 127)
                    else:
                        self.assertEqual(salang.fetch_weather(37, 127), ("spring", "clear", 12.5))
                read.assert_called_once_with(65537)

    def test_nested_and_oversized_weather_show_offline_state_then_recover(self):
        nested = b"[" * 2000 + b"0" + b"]" * 2000
        oversized = WEATHER_RESPONSE + b" " * (65537 - len(WEATHER_RESPONSE))
        self.assertLess(len(nested), 65536)
        for body in (nested, oversized):
            with mock.patch.object(sys, "argv", [SCRIPT, "--snapshot", "--location", "울릉도"]), \
                    mock.patch.object(salang.urllib.request, "urlopen", return_value=io.BytesIO(body)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                salang.main()
            self.assertIn("날씨 연결 안 됨(open-meteo.com) · 임시 장면", output.getvalue())

        clock, starts = [0.0], []
        fetch_weather = salang.fetch_weather
        bodies = (nested, oversized, WEATHER_RESPONSE)

        def fetch(latitude, longitude):
            index = len(starts)
            starts.append(clock[0])
            self.assertLess(index, len(bodies))
            with mock.patch.object(salang.urllib.request, "urlopen", return_value=io.BytesIO(bodies[index])):
                return fetch_weather(latitude, longitude)

        def wait(readers, writers, errors, timeout):
            self.assertLess(clock[0], 20, "Malformed weather did not recover")
            clock[0] = math.nextafter(clock[0] + timeout, math.inf)
            if "실시간(open-meteo.com) · 15분마다 갱신" in output.getvalue():
                return readers, [], []
            return [], [], []

        with simulated_weather(clock, fetch, wait, lambda: "q") as (output, errors):
            salang.main()
        self.assertEqual(len(starts), 3)
        for actual, expected in zip(starts, (0, 3, 13)):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertIn("12.5°C · 맑음 · 봄", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_retries_follow_completion_time_cap_at_sixty_and_reset_after_success(self):
        clock, starts, completed = [0.0], [], []

        def fetch(latitude, longitude):
            starts.append(clock[0])
            clock[0] += 2
            completed.append(clock[0])
            if len(starts) == 1:
                raise http.client.IncompleteRead(b"partial", 10)
            if len(starts) == 2:
                raise http.client.BadStatusLine("malformed fixture")
            if len(starts) not in (6, 8):
                raise OSError("offline fixture")
            return "summer", "clear", 25.0

        def wait(readers, writers, errors, timeout):
            self.assertLess(clock[0], 1300, "Weather recovery stopped")
            clock[0] = math.nextafter(clock[0] + timeout, math.inf)
            return (readers, [], []) if len(starts) == 8 else ([], [], [])

        with simulated_weather(clock, fetch, wait, lambda: "q") as (output, errors):
            salang.main()
        self.assertEqual(len(starts), 8)
        for index, delay in enumerate((3, 10, 30, 60, 60, 900, 3)):
            self.assertAlmostEqual(starts[index + 1] - completed[index], delay, places=5)
        self.assertIn("실시간(open-meteo.com) · 15분마다 갱신", output.getvalue())
        self.assertIn("마지막으로 확인한 날씨", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_resume_and_location_change_skip_retry_wait_and_reset_failure_delay(self):
        for action in ("resume", "location"):
            with self.subTest(action=action):
                clock, starts, completed, key = [0.0], [], [], ["q"]
                events = [(8, " "), (70, " ")] if action == "resume" else [(8, "r")]
                selected = regions.find_region("울릉도")

                def fetch(latitude, longitude):
                    starts.append((clock[0], latitude, longitude))
                    clock[0] += 2
                    completed.append(clock[0])
                    raise OSError("offline fixture")

                def wait(readers, writers, errors, timeout):
                    self.assertLess(clock[0], 100, "Explicit refresh did not bypass retry wait")
                    clock[0] = math.nextafter(clock[0] + timeout, math.inf)
                    if len(starts) == 4:
                        key[0] = "q"
                        return readers, [], []
                    if events and clock[0] >= events[0][0]:
                        _, key[0] = events.pop(0)
                        return readers, [], []
                    return [], [], []

                with mock.patch.object(salang, "select_region", return_value=selected), mock.patch.object(salang, "save_region"), \
                        simulated_weather(clock, fetch, wait, lambda: key[0]) as (_, errors):
                    salang.main()
                self.assertEqual(len(starts), 4)
                self.assertAlmostEqual(starts[1][0] - completed[0], 3, places=5)
                self.assertAlmostEqual(starts[2][0], 70 if action == "resume" else 8, places=5)
                self.assertAlmostEqual(starts[3][0] - completed[2], 3, places=5)
                if action == "location":
                    self.assertEqual(starts[2][1:], (selected.latitude, selected.longitude))
                self.assertEqual(errors.getvalue(), "")

    def test_http_protocol_errors_show_offline_state_in_snapshot(self):
        for error in (http.client.IncompleteRead(b"partial", 10), http.client.BadStatusLine("malformed fixture")):
            with mock.patch.object(sys, "argv", [SCRIPT, "--snapshot", "--location", "울릉도"]), \
                    mock.patch.object(salang, "fetch_weather", side_effect=error), contextlib.redirect_stdout(io.StringIO()) as output:
                salang.main()
            self.assertIn("날씨 연결 안 됨(open-meteo.com) · 임시 장면", output.getvalue())

    def test_weather_source_is_in_top_status_and_shortcuts_are_the_last_line(self):
        cases = ((("summer", "clear", 25), "실시간(open-meteo.com) · 15분마다 갱신"),
                 (None, "날씨 연결 안 됨(open-meteo.com) · 임시 장면"))
        for reading, expected in cases:
            for colored in (False, True):
                output = io.StringIO()
                arguments = [SCRIPT, "--live", "--snapshot", "--location", "울릉도"]
                with mock.patch.object(sys, "argv", arguments), \
                        mock.patch.object(salang, "fetch_weather") as fetch, \
                        mock.patch.object(salang.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))), \
                        mock.patch.dict(os.environ, {"COLORTERM": "truecolor"}), \
                        mock.patch.object(output, "isatty", return_value=colored), contextlib.redirect_stdout(output):
                    os.environ.pop("NO_COLOR", None)
                    if reading is None:
                        fetch.side_effect = OSError("offline fixture")
                    else:
                        fetch.return_value = reading
                    salang.main()
                value = output.getvalue()
                plain = re.sub(r"\x1b\]8;;.*?\x1b\\", "", value)
                plain = re.sub(r"\x1b\[[0-9;]*m", "", plain)
                lines = plain.splitlines()
                self.assertEqual(lines[1].strip(), expected)
                self.assertEqual(plain.count("open-meteo.com"), 1)
                self.assertIn("r 지역", lines[-1])
                self.assertIn("q 종료", lines[-1])
                self.assertEqual(len(lines), 35)
                if colored:
                    self.assertIn("\033]8;;https://open-meteo.com/\033\\", value.splitlines()[1])
                else:
                    self.assertNotIn("\033", value)
        with mock.patch.object(sys, "argv", [SCRIPT, "--season", "summer", "--weather", "clear", "--snapshot"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            salang.main()
            self.assertNotIn("open-meteo.com", output.getvalue())

    def test_snowman_is_winter_only_visible_and_stable_across_weather_and_sizes(self):
        features = {salang.SNOWMAN_HAT, salang.SNOWMAN_SCARF, salang.SNOWMAN_NOSE}
        for columns, rows in ((50, 18), (76, 28), (90, 32)):
            for season in salang.SEASONS:
                for weather in salang.WEATHERS:
                    canvas = salang.scene(season, weather, 3, columns, rows)
                    visible = {color for line in canvas.cells() for _, color in line}
                    self.assertEqual(features.issubset(visible), season == "winter")
                    if season != "winter":
                        self.assertTrue(features.isdisjoint(canvas.pixels.values()))
            pixels, cutouts = salang.snowman_shape(columns, rows)
            self.assertTrue(all(0 <= x <= columns * 2 - 1 and 0 <= y <= rows * 4 - 1 for x, y in pixels))
            self.assertGreater(min(x for x, _ in pixels), (columns * 2 - 1) * 0.6)
            self.assertGreater(min(y for _, y in pixels), (rows * 4 - 1) * 0.6)
            for weather, elapsed in (("snow", 0), ("wind", 8), ("clear", 20), ("rain", 30)):
                canvas = salang.scene("winter", weather, elapsed, columns, rows)
                fixed = {point: color for point, color in pixels.items()
                         if color != salang.SNOWMAN_SCARF and canvas.pixels.get(point) != salang.SNOWMAN_SCARF}
                self.assertTrue(all(canvas.pixels[point] == color for point, color in fixed.items()))
                self.assertTrue(all(point not in canvas.pixels for point in cutouts))
                canvas.pixels.clear()
            connected, pending = set(), [next(iter(pixels))]
            while pending:
                point = pending.pop()
                if point in connected or point not in pixels:
                    continue
                connected.add(point)
                x, y = point
                pending.extend((x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            self.assertEqual(connected, set(pixels))
        for columns, rows in ((1, 1), (24, 6)):
            pixels, cutouts = salang.snowman_shape(columns, rows)
            self.assertTrue(all(0 <= x < columns * 2 and 0 <= y < rows * 4 for x, y in (*pixels, *cutouts)))

    def test_weather_accents_are_visible_only_in_their_conditions_and_stay_bounded(self):
        accents = {
            ("spring", "clear"): set(seasonal.BUTTERFLY),
            ("spring", "wind"): {seasonal.SEED},
            ("summer", "rain"): {seasonal.RIPPLE},
            ("summer", "wind"): set(seasonal.PINWHEEL),
            ("autumn", "rain"): {seasonal.MUSHROOM_CAP},
            ("autumn", "wind"): {seasonal.ROLLING_LEAF},
        }
        all_colors = set().union(*accents.values(), seasonal.MIST)
        for columns, rows in ((50, 18), (76, 28), (90, 32)):
            for season in salang.SEASONS:
                for weather in salang.WEATHERS:
                    canvas = salang.scene(season, weather, 8, columns, rows)
                    visible = {color for line in canvas.cells() for _, color in line}
                    expected = accents.get((season, weather), set())
                    if weather == "cloudy":
                        expected = set(seasonal.MIST)
                    if expected:
                        self.assertTrue(expected.intersection(visible), (columns, rows, season, weather))
                    self.assertTrue(all_colors.intersection(canvas.pixels.values()).issubset(expected))
                    self.assertTrue(all(0 <= x < columns * 2 and 0 <= y < rows * 4 for x, y in canvas.pixels))

    def test_decorations_move_but_mushrooms_are_static_and_cached(self):
        for draw in (seasonal.draw_butterflies, seasonal.draw_dandelions, seasonal.draw_puddles,
                     seasonal.draw_pinwheel, seasonal.draw_rolling_leaves, seasonal.draw_fog):
            first, second = salang.Canvas(90, 32), salang.Canvas(90, 32)
            draw(first, 0)
            draw(second, 0.8)
            self.assertTrue(first.pixels)
            self.assertNotEqual(first.pixels, second.pixels, draw.__name__)
        first, second = salang.Canvas(90, 32), salang.Canvas(90, 32)
        seasonal.draw_mushrooms(first)
        seasonal.draw_mushrooms(second)
        self.assertEqual(first.pixels, second.pixels)
        self.assertIs(seasonal.mushroom_shape(90, 32), seasonal.mushroom_shape(90, 32))

    def test_snowman_scarf_moves_and_snow_accumulates_without_changing_its_face(self):
        for columns, rows in ((50, 18), (76, 28), (90, 32)):
            def figure(elapsed, weather):
                canvas = salang.Canvas(columns, rows)
                salang.draw_snowman(canvas, elapsed, weather)
                return canvas.pixels

            base = figure(0, "clear")
            wind = [figure(elapsed, "wind") for elapsed in (0, 0.8, 2.4)]
            self.assertNotEqual(wind[0], wind[1])
            self.assertNotEqual(wind[1], wind[2])
            for color in (salang.SNOWMAN_HAT, salang.SNOWMAN_NOSE):
                fixed = {point for point, value in base.items() if value == color}
                for pixels in wind:
                    self.assertTrue(all(pixels[point] == color for point in fixed))
            snow = [figure(elapsed, "snow") for elapsed in (0, 12, 24, 24000)]
            white = [{point for point, color in pixels.items() if color == salang.SNOW} for pixels in snow]
            self.assertLess(len(white[0]), len(white[1]))
            self.assertLess(len(white[1]), len(white[2]))
            self.assertEqual(snow[2], snow[3])
            for pixels in snow:
                self.assertTrue(all(pixels[point] == color for point, color in base.items()
                                    if color == salang.SNOWMAN_NOSE))
                self.assertTrue(all(point not in pixels for point in salang.snowman_shape(columns, rows)[1]))

    def test_fog_stays_behind_tree_and_accent_caches_are_bounded(self):
        fog = salang.Canvas(90, 32)
        seasonal.draw_fog(fog, 8)
        self.assertGreater(min(y for _, y in fog.pixels), fog.max_y * 0.75)
        with mock.patch.object(salang, "draw_fog"):
            plain = salang.scene("winter", "cloudy", 8, 90, 32)
        mist = salang.scene("winter", "cloudy", 8, 90, 32)
        for point, color in plain.pixels.items():
            self.assertEqual(mist.pixels[point], color)
        for columns, rows in ((1, 1), (24, 6), (50, 18)):
            for weather in salang.WEATHERS:
                canvas = salang.scene("winter", weather, 32, columns, rows)
                self.assertTrue(all(0 <= x < columns * 2 and 0 <= y < rows * 4 for x, y in canvas.pixels))
        self.assertEqual(salang.snowman_shape.cache_info().maxsize, 32)
        self.assertEqual(seasonal.mushroom_shape.cache_info().maxsize, 8)

    def test_swing_only_appears_in_clear_summer_and_moves_without_detaching(self):
        colors = {salang.SWING_ROPE, salang.SWING_SEAT}
        for season in salang.SEASONS:
            for weather in salang.WEATHERS:
                canvas = salang.scene(season, weather, 0)
                self.assertEqual(bool(colors.intersection(canvas.pixels.values())),
                                 season == "summer" and weather == "clear")
        x1, y1, x2, y2, _ = salang.SWING_BRANCH
        for columns, rows in ((50, 18), (76, 28), (90, 32)):
            centers = []
            for elapsed in (0, 1.4, 4.2):
                canvas = salang.Canvas(columns, rows)
                salang.draw_swing(canvas, elapsed, ((x1, y1), (x2, y2)))
                anchors = {(round(x / 160 * canvas.max_x), round(y / 112 * canvas.max_y))
                           for x, y in ((x1, y1), (x2, y2))}
                self.assertTrue(all(canvas.pixels[point] == salang.SWING_ROPE for point in anchors))
                seat = [point for point, color in canvas.pixels.items() if color == salang.SWING_SEAT]
                centers.append((min(x for x, _ in seat) + max(x for x, _ in seat)) / 2)
                self.assertGreaterEqual(max(x for x, _ in seat) - min(x for x, _ in seat), 6)
                self.assertLess(max(y for _, y in seat), 94 / 112 * canvas.max_y)
                connected, pending = set(), [next(iter(anchors))]
                while pending:
                    point = pending.pop()
                    if point in connected or point not in canvas.pixels:
                        continue
                    connected.add(point)
                    x, y = point
                    pending.extend((x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
                self.assertEqual(connected, set(canvas.pixels))
            self.assertGreater(centers[1], centers[0] + 2)
            self.assertLess(centers[2], centers[0] - 2)

    def test_old_garden_region_is_preserved_and_new_settings_use_salang(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": folder}):
            legacy = Path(folder) / "terminal-garden" / "settings.json"
            legacy.parent.mkdir()
            legacy.write_text('{"version":1,"region_code":"4794000000"}')
            original = legacy.read_bytes()
            self.assertEqual(regions.saved_region(), regions.find_region("울릉도"))
            self.assertEqual(regions.settings_path(), Path(folder) / "salang" / "settings.json")
            selected = regions.find_region("서울 강남구")
            regions.save_region(selected)
            self.assertEqual(regions.saved_region(), selected)
            self.assertEqual(legacy.read_bytes(), original)

    def test_benchmark_includes_native_child_cpu_and_excludes_unrelated_processes(self):
        snapshot = "1 0 200-00:00:00\n100 1 00:00:00.10\n101 100 1-00:00:02.20\n102 101 00:00:00.30\n200 1 10-01:00:00\n"
        with mock.patch("tools.benchmark.subprocess.check_output", return_value=snapshot):
            self.assertAlmostEqual(cpu_seconds(100), 86402.6)
            with self.assertRaises(RuntimeError):
                cpu_seconds(999)

    @unittest.skipUnless(Path("/bin/zsh").is_file(), "Salang.command requires zsh")
    def test_launcher_uses_fresh_native_build_and_falls_back_for_newer_source(self):
        import platform
        system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
        architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / "Salang.command"
            launcher.write_bytes(Path(SCRIPT).with_name("Salang.command").read_bytes())
            launcher.chmod(0o755)
            (root / "salang.py").write_text('import sys; print("source:" + sys.argv[1])')
            region_source = root / "regions.py"
            region_source.touch()
            seasonal_source = root / "seasonal.py"
            seasonal_source.touch()
            old = time.time() - 10
            for path in (root / "salang.py", region_source, seasonal_source):
                os.utime(path, (old, old))
            (root / "dist").mkdir()
            binary = root / "dist" / f"salang-{system}-{architecture}"
            binary.write_text('#!/bin/sh\nprintf "native:%s\\n" "$@"\n')
            binary.chmod(0o755)
            link = root / "salang"
            link.symlink_to(launcher)
            command = [str(link), "--snapshot"]
            self.assertEqual(subprocess.check_output(command, text=True).strip(), "native:--snapshot")
            newer = time.time() + 10
            for source in (region_source, seasonal_source):
                os.utime(source, (newer, newer))
                self.assertEqual(subprocess.check_output(command, text=True).strip(), "source:--snapshot")
                os.utime(source, (old, old))
            binary.rename(binary.with_suffix(".previous"))
            self.assertEqual(subprocess.check_output(command, text=True).strip(), "source:--snapshot")

    def test_sparse_braille_conversion_preserves_masks_and_color_ties(self):
        rng = random.Random(77)
        colors = (salang.BARK, salang.SNOW, salang.SUN, (0, 0, 0))
        for columns, rows in ((1, 1), (17, 8), (90, 32)):
            points = [(x, y) for y in range(rows * 4) for x in range(columns * 2)]
            rng.shuffle(points)
            for density in (0, 0.05, 0.5, 1):
                canvas = salang.Canvas(columns, rows)
                canvas.pixels = {point: rng.choice(colors) for point in points if rng.random() < density}
                canvas.pixels[-1, 0] = salang.SUN
                expected = []
                for row in range(rows):
                    line = []
                    for col in range(columns):
                        mask, present = 0, []
                        for dy in range(4):
                            for dx in range(2):
                                color = canvas.pixels.get((col * 2 + dx, row * 4 + dy))
                                if color:
                                    mask |= salang.BITS[dy][dx]
                                    present.append(color)
                        dominant = Counter(present).most_common(1)[0][0] if present else None
                        line.append((chr(0x2800 + mask) if mask else " ", dominant))
                    expected.append(line)
                self.assertEqual(canvas.cells(), expected)

    def test_falling_sources_and_visible_leaves_cover_both_sides(self):
        self.assertEqual(len(salang.FALL_SOURCES), 36)
        self.assertTrue(set(salang.FALL_SOURCES).issubset(set(salang.LEAVES)))
        self.assertEqual(sum(leaf[0] < 80 for leaf in salang.FALL_SOURCES), 18)
        self.assertLess(min(leaf[0] for leaf in salang.FALL_SOURCES), 30)
        self.assertGreater(max(leaf[0] for leaf in salang.FALL_SOURCES), 125)
        counts = [0, 0]
        for elapsed in range(0, 60, 2):
            canvas = salang.scene("autumn", "clear", elapsed, 90, 32)
            for (x, y), color in canvas.pixels.items():
                if color in salang.PALETTES["autumn"] and 70 / 112 * canvas.max_y < y < 94 / 112 * canvas.max_y:
                    counts[x >= canvas.max_x / 2] += 1
        self.assertGreater(min(counts), 100)
        self.assertLess(max(counts) / min(counts), 1.7)

    def test_partial_screen_updates_clear_old_text_without_redrawing_unchanged_rows(self):
        cells, row, col = [[" "] * 60 for _ in range(12)], 0, 0

        def apply(value):
            nonlocal row, col
            for token in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|[^\x1b]+", value):
                if token.startswith("\x1b["):
                    command, arguments = token[-1], token[2:-1]
                    if command == "H":
                        row, col = (int(value) - 1 for value in arguments.split(";"))
                    elif command == "K":
                        cells[row][col:] = [" "] * (60 - col)
                    elif command == "J":
                        cells[row][col:] = [" "] * (60 - col)
                        for line in cells[row + 1:]:
                            line[:] = [" "] * 60
                else:
                    for char in token:
                        cells[row][col] = char
                        width = salang.text_width(char)
                        if width == 2:
                            cells[row][col + 1] = ""
                        col += width

        previous = None
        for value in ("서울 강남구 │ 맑음\n긴 상태 설명과 일시정지\n나무\n단축키",
                      "서울 강남구 │ 비\n짧음\n나무\n단축키",
                      "서울 강남구 │ 비\n짧음\n나무\n단축키",
                      "창을 키워 주세요", "", "\033[38;5;246m지역 선택\033[0m\n› 서울"):
            update = screen_frame(value, previous)
            if value == previous:
                self.assertEqual(update, "")
            if previous is not None and previous.startswith("서울") and value.startswith("서울"):
                self.assertNotIn("나무", update)
                self.assertNotIn("단축키", update)
            apply(update)
            expected = re.sub(r"\x1b\[[0-9;]*m", "", value).splitlines()
            self.assertEqual(["".join(line).rstrip() for line in cells], expected + [""] * (12 - len(expected)))
            previous = value

    def test_sun_is_symmetric_and_its_center_has_no_holes(self):
        for columns, rows in ((50, 18), (76, 28), (90, 32)):
            with self.subTest(columns=columns, rows=rows):
                canvas = salang.Canvas(columns, rows)
                canvas.sun()
                dots = set(canvas.pixels)
                cx = (min(x for x, _ in dots) + max(x for x, _ in dots)) // 2
                cy = (min(y for _, y in dots) + max(y for _, y in dots)) // 2
                self.assertEqual(dots, {(2 * cx - x, y) for x, y in dots})
                self.assertEqual(dots, {(x, 2 * cy - y) for x, y in dots})
                center, pending = set(), [(cx, cy)]
                while pending:
                    point = pending.pop()
                    if point in center or point not in dots:
                        continue
                    center.add(point)
                    x, y = point
                    pending.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
                self.assertGreaterEqual(len(center), 13)
                for y in {y for _, y in center}:
                    xs = {x for x, py in center if py == y}
                    self.assertEqual(xs, set(range(min(xs), max(xs) + 1)))

    def test_shortcut_keys_are_white_descriptions_gray_and_fit_terminal(self):
        self.assertEqual(salang.color_escape(salang.MUTED, "256"), "\033[38;5;246m")
        for mode in ("rgb", "256", "none"):
            for columns in (24, 48, 78):
                line = salang.shortcut_line(columns, mode)
                visible = re.sub(r"\x1b\[[0-9;]*m", "", line)
                self.assertLessEqual(salang.text_width(visible), columns)
                self.assertIn("s 계절", visible)
                if mode == "none":
                    self.assertNotIn("\033", line)
                else:
                    self.assertIn(salang.color_escape(salang.FOREGROUND, mode) + "s", line)
                    self.assertIn(salang.color_escape(salang.MUTED, mode) + " 계절", line)

    def test_official_regions_aliases_and_ambiguous_names(self):
        records = regions.all_regions()
        self.assertEqual(len(records), 3837)
        self.assertEqual(len({record.code for record in records}), len(records))
        for record in records:
            self.assertTrue(30 < record.latitude < 40 and 124 < record.longitude < 133)
        for name, code in (("울릉도", "4794000000"), ("경북 울릉군", "4794000000"),
                           ("울릉군 독도", "4794033000"), ("서울 강남구 역삼1동", "1168064000"),
                           ("성남 분당구", "4113500000"), ("세종", "3600000000"),
                           ("세종 조치원읍", "3611025000")):
            self.assertEqual(regions.find_region(name).code, code)
        with self.assertRaises(ValueError):
            regions.find_region("중구")
        self.assertIsNone(regions.find_region("없는 지역 이름"))
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network access")):
            self.assertEqual(salang.resolve_location("울릉도"), (37.480575, 130.903788888888))

    def test_region_menu_navigation_and_saved_default(self):
        keys = choice_keys((), "경기도") + choice_keys(("경기도",), "성남시")
        keys += choice_keys(("경기도", "성남시"), "분당구") + ["back", "back", "back", "home"]
        keys += choice_keys((), "경상북도") + choice_keys(("경상북도",), "울릉군")
        keys += choice_keys(("경상북도", "울릉군"), "서면")
        with simulated_menu(keys):
            selected = regions.select_region()
        self.assertEqual(selected.code, "4794031000")
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": folder}):
            regions.save_region(selected)
            self.assertEqual(regions.saved_region(), selected)
            saved = regions.settings_path().read_bytes()
            with mock.patch.object(sys, "argv", ["salang", "--snapshot"]), \
                    mock.patch.object(salang, "fetch_weather", return_value=("winter", "snow", -2)) as fetch, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                salang.main()
            fetch.assert_called_once_with(selected.latitude, selected.longitude)
            self.assertIn(selected.label, output.getvalue())
            with mock.patch.object(sys, "argv", ["salang", "--location", "서울 강남구", "--snapshot"]), \
                    mock.patch.object(salang, "fetch_weather", return_value=("autumn", "clear", 10)) as fetch, \
                    contextlib.redirect_stdout(io.StringIO()):
                salang.main()
            fetch.assert_called_once_with(37.514575, 127.049555555555)
            self.assertEqual(regions.settings_path().read_bytes(), saved)
            regions.settings_path().write_text('{"version":1,"region_code":"missing"}')
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertIsNone(regions.saved_region())
            self.assertIn("기본 강남구", errors.getvalue())
        with simulated_menu(["q"]):
            with self.assertRaises(KeyboardInterrupt):
                regions.select_region()

    def test_live_resume_ignores_stale_response_and_retains_selected_location(self):
        runner = textwrap.dedent('''
            import os, pathlib, sys, time
            sys.path.insert(0, sys.argv.pop(1))
            import salang
            calls = 0
            active = 0
            peak = 0
            gate = pathlib.Path(os.environ["SALANG_TEST_GATE"])
            def fake_fetch(latitude, longitude):
                global calls, active, peak
                assert (latitude, longitude) == (35.1, 129.03)
                calls += 1
                active += 1
                peak = max(peak, active)
                try:
                    if calls == 1:
                        while not gate.exists():
                            time.sleep(0.01)
                        return ("spring", "snow", -9.0)
                    if calls == 2:
                        return ("autumn", "rain", 12.5)
                    if calls == 3:
                        return ("autumn", "rain", 13.0)
                    if calls == 4:
                        return ("winter", "snow", -3.0)
                    raise OSError("offline test")
                finally:
                    active -= 1
            salang.fetch_weather = fake_fetch
            salang.main()
            print("TEST_FETCH_COUNTS", calls, peak, flush=True)
        ''')
        with tempfile.TemporaryDirectory() as folder:
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 100, 0, 0))
            gate = Path(folder) / "release"
            env = dict(os.environ, TERM="xterm-256color", SALANG_TEST_GATE=str(gate), XDG_CONFIG_HOME=folder)
            process = subprocess.Popen([sys.executable, "-c", runner, str(Path(SCRIPT).parent),
                                        "--latitude", "35.1", "--longitude", "129.03", "--location", "부산 테스트"],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            output = bytearray()
            try:
                read_output(master, "날씨 불러오는 중".encode(), output)
                for key, expected in ((b"s", "직접 선택".encode()), (b"l", "날씨 불러오는 중".encode())):
                    start = len(output)
                    os.write(master, key)
                    read_output(master, expected, output, start)
                gate.touch()
                read_output(master, "12.5°C · 비 · 가을".encode(), output)
                self.assertNotIn("-9.0°C".encode(), output)
                start = len(output)
                os.write(master, b" ")
                read_output(master, "일시정지".encode(), output, start)
                os.write(master, b" ")
                read_output(master, "13.0°C · 비 · 가을".encode(), output)
                start = len(output)
                os.write(master, b"s")
                read_output(master, "비 · 겨울".encode(), output, start)
                os.write(master, b"l")
                read_output(master, "-3.0°C · 눈 · 겨울".encode(), output)
                start = len(output)
                os.write(master, b"w")
                read_output(master, "흐림 · 겨울".encode(), output, start)
                os.write(master, b"l")
                read_output(master, "연결 끊김(open-meteo.com) · 마지막으로 확인한 날씨".encode(), output)
                os.write(master, b"lllq")
                read_output(master, b"TEST_FETCH_COUNTS 5 1", output)
                process.wait(timeout=6)
                self.assertEqual(process.returncode, 0)
                restored = termios.tcgetattr(slave)
                restored[3] &= ~termios.PENDIN
                original[3] &= ~termios.PENDIN
                self.assertEqual(restored, original)
            finally:
                gate.touch()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                os.close(slave)

    def test_region_setup_in_real_terminal_saves_selection(self):
        runner = textwrap.dedent('''
            import sys
            sys.path.insert(0, sys.argv.pop(1))
            import salang
            def fake_fetch(latitude, longitude):
                if (latitude, longitude) == (37.480575, 130.903788888888):
                    return ("summer", "rain", 23.0)
                assert (latitude, longitude) == (37.4925833333333, 127.035144444444)
                return ("winter", "clear", 1.5)
            salang.fetch_weather = fake_fetch
            salang.main()
        ''')
        with tempfile.TemporaryDirectory() as folder:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 80, 0, 0))
            env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=folder)
            process = subprocess.Popen([sys.executable, "-c", runner, str(Path(SCRIPT).parent), "--setup", "--demo"],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            output = bytearray()
            try:
                read_output(master, "지역 선택".encode(), output)
                for prefix, answer, expected in (((), "경상북도", "지역 선택  /  경상북도"),
                                                (("경상북도",), "울릉군", "경상북도 → 울릉군"),
                                                (("경상북도", "울릉군"), None, "바람 · 봄")):
                    start = len(output)
                    os.write(master, choice_bytes(prefix, answer))
                    read_output(master, expected.encode(), output, start)
                value = json.loads((Path(folder) / "salang" / "settings.json").read_text())
                self.assertEqual(value["region_code"], "4794000000")
                os.write(master, b"l")
                read_output(master, "23.0°C · 비 · 여름".encode(), output)
                start = len(output)
                os.write(master, b"r")
                read_output(master, "지역 선택".encode(), output, start)
                for prefix, answer, expected in (((), "서울특별시", "지역 선택  /  서울특별시"),
                                                (("서울특별시",), "강남구", "서울특별시 → 강남구"),
                                                (("서울특별시", "강남구"), "역삼1동", "1.5°C · 맑음 · 겨울")):
                    start = len(output)
                    os.write(master, choice_bytes(prefix, answer))
                    read_output(master, expected.encode(), output, start)
                value = json.loads((Path(folder) / "salang" / "settings.json").read_text())
                self.assertEqual(value["region_code"], "1168064000")
                start = len(output)
                os.write(master, b"r")
                read_output(master, "지역 선택".encode(), output, start)
                start = len(output)
                os.write(master, b"q")
                read_output(master, "1.5°C · 맑음 · 겨울".encode(), output, start)
                start = len(output)
                os.write(master, b"q")
                read_output(master, b"\033[?1049l", output, start)
                process.wait(timeout=6)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                os.close(slave)

    def test_dots_seasons_weather_and_animation(self):
        for y, bits in enumerate(salang.BITS):
            for x, bit in enumerate(bits):
                canvas = salang.Canvas(80, 28)
                canvas.point(x, y, salang.BARK)
                self.assertEqual(ord(canvas.cells()[0][0][0]), 0x2800 + bit)
        for season in salang.SEASONS:
            for weather in salang.WEATHERS:
                with self.subTest(season=season, weather=weather):
                    first = salang.scene(season, weather, 1, 66, 24)
                    second = salang.scene(season, weather, 2, 66, 24)
                    self.assertGreater(len(first.pixels), 400)
                    self.assertNotEqual(first.pixels, second.pixels)
                    lines = salang.render(first, "none").splitlines()
                    self.assertEqual(len(lines), 24)
                    self.assertTrue(all(len(line) == 66 for line in lines))
                    self.assertNotIn("\033", "\n".join(lines))
        self.assertGreater(len(salang.scene("summer", "clear", 0).pixels),
                           len(salang.scene("winter", "clear", 0).pixels))
        early = salang.scene("winter", "snow", 0).pixels
        later = salang.scene("winter", "snow", 35).pixels
        self.assertGreater(sum(c == salang.SNOW for c in later.values()),
                           sum(c == salang.SNOW for c in early.values()))

    def test_pipe_snapshot_and_argument_validation(self):
        output = subprocess.check_output([sys.executable, SCRIPT, "--demo"], text=True)
        self.assertIn("바람 · 봄", output)
        self.assertNotIn("\033", output)
        for seconds in ("-1", "nan", "inf"):
            result = subprocess.run([sys.executable, SCRIPT, "--seconds", seconds],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_weather_codes_local_season_and_invalid_data(self):
        def payload(code, wind=0):
            return {"current": {"weather_code": code, "temperature_2m": 12.5,
                                "wind_speed_10m": wind, "time": "2026-04-03T12:00"}}
        for code, expected in ((0, "clear"), (1, "clear"), (3, "cloudy"), (45, "cloudy"),
                               (51, "rain"), (67, "rain"), (95, "rain"), (99, "rain"),
                               (71, "snow"), (77, "snow"), (86, "snow")):
            self.assertEqual(salang.parse_weather(payload(code), 37), ("spring", expected, 12.5))
        self.assertEqual(salang.parse_weather(payload(0, 25), 37)[1], "wind")
        self.assertEqual(salang.parse_weather(payload(71, 25), 37)[1], "snow")
        self.assertEqual(salang.parse_weather(payload(0), -37)[0], "autumn")
        expected = ("winter", "winter", "spring", "spring", "spring", "summer",
                    "summer", "summer", "autumn", "autumn", "autumn", "winter")
        self.assertEqual(tuple(salang.season_for(month) for month in range(1, 13)), expected)
        for value in (None, float("nan"), float("inf"), True, 900):
            with self.subTest(code=value), self.assertRaises(ValueError):
                salang.parse_weather(payload(value), 37)
        for args in (("--latitude", "37"), ("--latitude", "nan"), ("--longitude", "181"),
                     ("--live", "--demo", "--latitude", "37", "--longitude", "127")):
            result = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_location_matching_is_offline_and_rejects_unknown_or_ambiguous_places(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network access")):
            self.assertEqual(salang.resolve_location("서울 강남구"), (37.514575, 127.049555555555))
            for english, korean in (("Seoul", "서울"), ("BUSAN", "부산"), ("Incheon", "인천"),
                                    ("Daegu", "대구"), ("Daejeon", "대전"), ("Ulsan", "울산"),
                                    ("gangnam", "서울 강남구")):
                self.assertEqual(salang.resolve_location(english), salang.resolve_location(korean))
            for name in ("Busanggol", "없는 지역 이름", "중구"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    salang.resolve_location(name)
        self.assertEqual(salang.fit_text("서울 강남구", 6), "서울 ")

    def test_escape_sequences_do_not_leak_into_shortcuts(self):
        reader, writer = os.pipe()
        try:
            for sequence, expected in ((b"\x1bOS", ""), (b"\x1b[1;2S", ""),
                                       (b"\x1b[12345678901;2S", ""), (b"\x1b[[A", ""),
                                       (b"\x1bs", ""), (b"\x1b[A", "up"),
                                       (b"\x1bOB", "down"), (b"\x1b[5~", "page-up"),
                                       (b"\x1b", "back"), (b"S", "s"), (b" ", " ")):
                with self.subTest(sequence=sequence):
                    os.write(writer, sequence)
                    self.assertEqual(read_key(reader), expected)
            os.write(writer, b"\x1bOSs")
            self.assertEqual(read_key(reader), "")
            self.assertEqual(read_key(reader), "s")
        finally:
            os.close(reader)
            os.close(writer)

    def test_paused_screen_is_idle_but_updates_for_weather_resize_and_controls(self):
        runner = textwrap.dedent('''
            import os, sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv.pop(1))
            import salang
            def fake_fetch(latitude, longitude):
                while not Path(os.environ["SALANG_TEST_GATE"]).exists():
                    time.sleep(0.01)
                return ("winter", "snow", -3.0)
            salang.fetch_weather = fake_fetch
            salang.main()
        ''')
        with tempfile.TemporaryDirectory() as folder:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 80, 0, 0))
            gate = Path(folder) / "release"
            env = dict(os.environ, TERM="xterm-256color", XDG_CONFIG_HOME=folder, SALANG_TEST_GATE=str(gate))
            env.pop("COLUMNS", None)
            env.pop("LINES", None)
            process = subprocess.Popen([sys.executable, "-c", runner, str(Path(SCRIPT).parent),
                                        "--latitude", "37", "--longitude", "127"],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            output = bytearray()

            def screen(expected, start):
                marker = expected.encode()
                read_output(master, marker, output, start)
                end = output.index(marker, start) + len(marker)
                read_output(master, b"\033[K\033[J", output, end)

            try:
                screen("날씨 불러오는 중", 0)
                start = len(output)
                os.write(master, b" ")
                screen("일시정지", start)
                self.assertFalse(select.select([master], [], [], 0.25)[0], "Paused screen was redrawn")
                start = len(output)
                os.write(master, b"\x1bOS\x1b[A\x1b[1;2S")
                self.assertFalse(select.select([master], [], [], 0.25)[0], "Function key changed paused screen")
                gate.touch()
                screen("-3.0°C · 눈 · 겨울", start)
                self.assertIn("일시정지".encode(), output[start:])
                self.assertFalse(select.select([master], [], [], 0.25)[0])
                start = len(output)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 18, 0, 0))
                screen("창을 키워", start)
                self.assertFalse(select.select([master], [], [], 0.25)[0])
                start = len(output)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 80, 0, 0))
                screen("-3.0°C · 눈 · 겨울", start)
                start = len(output)
                os.write(master, b"s")
                screen("눈 · 봄", start)
                self.assertIn("일시정지".encode(), output[start:])
                self.assertFalse(select.select([master], [], [], 0.25)[0])
                start = len(output)
                os.write(master, b" ")
                screen("직접 선택", start)
                self.assertNotIn("일시정지".encode(), output[start:])
                self.assertTrue(select.select([master], [], [], 0.5)[0], "Animation did not resume")
                self.assertNotIn(b"\033[H\033[J", output)
                start = len(output)
                os.write(master, b"q")
                read_output(master, b"\033[?1049l", output, start)
                process.wait(timeout=4)
                self.assertEqual(process.returncode, 0)
            finally:
                gate.touch()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                os.close(slave)

    def test_interactive_controls_and_terminal_restore(self):
        for exit_action in (b"q", signal.SIGINT, signal.SIGTERM):
            with self.subTest(exit_action=exit_action):
                master, slave = pty.openpty()
                original = termios.tcgetattr(slave)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 80, 0, 0))
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
                env.pop("NO_COLOR", None)
                process = subprocess.Popen(
                    [sys.executable, SCRIPT, "--season", "spring", "--weather", "clear"],
                    stdin=slave, stdout=slave, stderr=slave, env=env)
                output = bytearray()

                def read_until(expected):
                    deadline = time.monotonic() + 4
                    while expected not in output and time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            output.extend(os.read(master, 65536))
                    self.assertIn(expected, output)

                try:
                    read_until("맑음 · 봄".encode())
                    self.assertIn(b"\033[38;2;", output)
                    os.write(master, b"s")
                    read_until("맑음 · 여름".encode())
                    os.write(master, b"w")
                    read_until("바람 · 여름".encode())
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 18, 0, 0))
                    read_until("창을 키워".encode())
                    if isinstance(exit_action, bytes):
                        os.write(master, exit_action)
                    else:
                        process.send_signal(exit_action)
                    process.wait(timeout=4)
                    self.assertEqual(process.returncode, 0)
                    read_until(b"\033[?1049l")
                    restored = termios.tcgetattr(slave)
                    # macOS sets PENDIN itself when canonical input is restored.
                    restored[3] &= ~termios.PENDIN
                    original[3] &= ~termios.PENDIN
                    self.assertEqual(restored, original)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)


if __name__ == "__main__":
    unittest.main()
