"""Measure Salang CPU time and PTY output without contacting the weather API."""

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time


def cpu_seconds(pid):
    snapshot = subprocess.check_output(["ps", "-A", "-o", "pid=,ppid=,time="], text=True)
    processes = {}
    for line in snapshot.splitlines():
        process, parent, value = line.split()
        elapsed, days = 0.0, 0
        if "-" in value:
            days, value = value.split("-", 1)
        for part in value.split(":"):
            elapsed = elapsed * 60 + float(part)
        processes[int(process)] = (int(parent), int(days) * 86400 + elapsed)
    if pid not in processes:
        raise RuntimeError("Salang exited before its CPU time could be read")
    selected, pending = set(), [pid]
    while pending:
        current = pending.pop()
        if current in selected:
            continue
        selected.add(current)
        pending.extend(child for child, (parent, _) in processes.items() if parent == current)
    return sum(processes[process][1] for process in selected)


def measure(command, season, weather, paused, seconds):
    with tempfile.TemporaryDirectory(prefix="salang-benchmark-") as folder:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor",
                   XDG_CONFIG_HOME=folder, PYTHONDONTWRITEBYTECODE="1")
        for name in ("COLUMNS", "LINES", "NO_COLOR"):
            env.pop(name, None)
        process = subprocess.Popen(command + ["--season", season, "--weather", weather],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        frames, tail = 0, b""

        def receive(timeout):
            nonlocal frames, tail
            if not select.select([master], [], [], timeout)[0]:
                return b""
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                return b""
            frames += (tail + data).count(b"\033[K\033[J")
            tail = (tail + data)[-5:]
            return data

        def drain(duration):
            count, deadline = 0, time.monotonic() + duration
            while time.monotonic() < deadline:
                count += len(receive(min(0.05, max(0, deadline - time.monotonic()))))
                if process.poll() is not None:
                    raise RuntimeError("Salang exited during the measurement")
            return count

        try:
            output, deadline = bytearray(), time.monotonic() + 10
            while b"\033[?25l" not in output and time.monotonic() < deadline:
                output.extend(receive(0.1))
                if process.poll() is not None:
                    raise RuntimeError(output.decode(errors="replace"))
            if b"\033[?25l" not in output:
                raise RuntimeError("Salang did not open its terminal screen")
            drain(1.0)
            if paused:
                os.write(master, b" ")
                output, deadline = bytearray(), time.monotonic() + 5
                while "일시정지".encode() not in output and time.monotonic() < deadline:
                    output.extend(receive(0.1))
                if "일시정지".encode() not in output:
                    raise RuntimeError("Salang did not pause")
                drain(0.3)
            cpu_start = cpu_seconds(process.pid)
            frames_start = frames
            started = time.monotonic()
            count = drain(seconds)
            cpu_end = cpu_seconds(process.pid)
            duration = time.monotonic() - started
            return {"season": season, "weather": weather, "paused": paused,
                    "seconds": round(duration, 3), "cpu_percent_one_core": round((cpu_end - cpu_start) / duration * 100, 3),
                    "output_bytes_per_second": round(count / duration, 1),
                    "output_frames_per_second": round((frames - frames_start) / duration, 3)}
        finally:
            if process.poll() is None:
                os.write(master, b"q")
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    receive(0.05)
                if process.poll() is None:
                    process.kill()
            process.wait()
            os.close(master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or not 1 <= args.repeat <= 10:
        parser.error("--seconds must be 1..60 and --repeat must be 1..10")
    command = ([str(args.executable.resolve())] if args.executable else
               [sys.executable, "-B", str(args.project.resolve() / "salang.py")])
    records = []
    for season, weather, paused in (("summer", "rain", False), ("autumn", "clear", False),
                                    ("winter", "snow", False), ("summer", "rain", True)):
        for repetition in range(args.repeat):
            result = measure(command, season, weather, paused, args.seconds)
            result["repetition"] = repetition + 1
            records.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.output:
        args.output.write_text(json.dumps({"command": command, "terminal": "100x40, truecolor",
                                          "results": records}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
