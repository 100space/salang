"""Verify installation and the real terminal menu of a native release binary."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import select
import struct
import subprocess
import termios

from check_salang import choice_bytes, read_output
from check_install import InstallChecks
from salang import SEASONS, WEATHERS, render, scene


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("binary", type=Path)
parser.add_argument("--live-check", action="store_true")
args = parser.parse_args()
binary = args.binary.resolve()
fixture = InstallChecks()
fixture.setUp()
try:
    fixture.env.update(SALANG_TEST_PLATFORM=platform.system(), SALANG_TEST_ARCH=platform.machine())
    (fixture.fixture / "binary").write_bytes(binary.read_bytes())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (fixture.fixture / "SHA256SUMS").write_text("".join(f"{digest}  salang-{system}-{arch}\n"
        for system in ("darwin", "linux") for arch in ("arm64", "amd64")))
    result = fixture.install()
    if result.returncode:
        raise SystemExit(result.stderr)
    installed = fixture.prefix / "bin" / "salang"
    env = dict(fixture.env, XDG_CONFIG_HOME=str(fixture.root / "settings"))
    demo = subprocess.check_output([str(installed), "--demo", "--snapshot"], env=env, text=True)
    assert "바람 · 봄" in demo and "r 지역" in demo
    for season in SEASONS:
        for weather in WEATHERS:
            snapshot = subprocess.check_output([str(installed), "--season", season, "--weather", weather, "--snapshot"],
                                              env=dict(env, COLUMNS="80", LINES="35"), text=True)
            assert render(scene(season, weather, 6, 78, 29), "none") in snapshot, (season, weather)
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 80, 0, 0))
    env["TERM"] = "xterm-256color"
    process = subprocess.Popen([str(installed), "--setup", "--demo"], stdin=slave, stdout=slave, stderr=slave, env=env)
    output = bytearray()
    try:
        read_output(master, "지역 선택".encode(), output)
        for prefix, child, expected in (((), "경상북도", "지역 선택  /  경상북도"),
                                        (("경상북도",), "울릉군", "경상북도 → 울릉군"),
                                        (("경상북도", "울릉군"), None, "바람 · 봄")):
            start = len(output)
            os.write(master, choice_bytes(prefix, child))
            read_output(master, expected.encode(), output, start)
        settings = json.loads((fixture.root / "settings" / "salang" / "settings.json").read_text())
        assert settings["region_code"] == "4794000000"
        start = len(output)
        os.write(master, b"\x1bOS ")
        read_output(master, "일시정지".encode(), output, start)
        end = output.index("일시정지".encode(), start) + len("일시정지".encode())
        read_output(master, b"\033[K\033[J", output, end)
        assert "계절 미리보기".encode() in output[start:]
        assert "여름".encode() not in output[start:]
        assert not select.select([master], [], [], 0.25)[0], "Paused binary kept drawing"
        start = len(output)
        os.write(master, b" ")
        read_output(master, "계절 미리보기".encode(), output, start)
        start = len(output)
        os.write(master, b"r")
        read_output(master, "지역 선택".encode(), output, start)
        start = len(output)
        os.write(master, b"q")
        read_output(master, "바람 · 봄".encode(), output, start)
        start = len(output)
        os.write(master, b"q")
        read_output(master, b"\033[?1049l", output, start)
        process.wait(timeout=10)
        assert process.returncode == 0
        assert b"\033[H\033[J" not in output, "Screen was cleared before drawing"
        restored = termios.tcgetattr(slave)
        restored[3] &= ~termios.PENDIN
        original[3] &= ~termios.PENDIN
        assert restored == original
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
    print("PASS native bundle: 20 season/weather scenes, installer, command, arrow menu, F4, pause/resume, overwrite rendering, terminal restoration")
    if args.live_check:
        live = subprocess.check_output([str(installed), "--location", "울릉도", "--snapshot"], env=env, text=True, timeout=15)
        assert "울릉도" in live.splitlines()[0] and "실시간" in live.splitlines()[1], live[:200]
        print("PASS native bundle actual weather:", live.splitlines()[0].strip())
finally:
    fixture.tearDown()
