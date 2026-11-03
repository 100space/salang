"""Build and smoke-test a standalone Salang executable for the current host."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile


root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=root / "dist")
args = parser.parse_args()
operating_system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
if not operating_system or not architecture:
    parser.error("Builds currently support macOS/Linux on arm64/amd64")
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
build_root = root / ".build"
build_root.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix="release-", dir=build_root) as value:
    work = Path(value)
    licences = work / "licenses"
    shutil.copytree(root / "licenses", licences)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit("Python redistribution licence was not found; refusing to build")
    shutil.copy2(python_license, licences / "PYTHON.txt")
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-m", "PyInstaller", "--onefile", "--name", "salang", "--noconfirm", "--noupx",
               "--log-level", "WARN", "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
               "--specpath", str(work), "--add-data", str(root / "korea-regions.json") + ":.",
               "--add-data", str(root / "THIRD_PARTY_NOTICES.md") + ":.",
               "--add-data", str(licences) + ":licenses", str(root / "salang.py")]
    subprocess.run(command, cwd=root, env=env, check=True)
    binary = work / "dist" / "salang"
    version = subprocess.check_output([str(binary), "--version"], cwd=work, env=env, text=True).strip()
    assert version.startswith("salang "), version
    demo = subprocess.check_output([str(binary), "--demo", "--snapshot"], cwd=work, env=env, text=True)
    assert "바람 · 봄" in demo and "r 지역" in demo
    notices = subprocess.check_output([str(binary), "--licenses"], cwd=work, env=env, text=True)
    assert "python software foundation" in notices.lower() and "Open-Meteo" in notices
    target = output / f"salang-{operating_system}-{architecture}"
    shutil.copy2(binary, target)
    target.chmod(0o755)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(version)
    print(target)
    print("PASS standalone version, embedded region catalog, Korean UI, licence texts")
