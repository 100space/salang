"""Verify all native artifacts and produce the installer's checksum manifest."""

import hashlib
from pathlib import Path
import sys


root = Path(sys.argv[1]).resolve()
expected = {"salang-" + system + "-" + arch for system in ("darwin", "linux") for arch in ("arm64", "amd64")}
actual = {path.name for path in root.glob("salang-*") if path.is_file() and not path.name.endswith(".sha256")}
if actual != expected:
    raise SystemExit(f"Unexpected release artifacts: missing={expected - actual}, extra={actual - expected}")
lines = []
for name in sorted(expected):
    line = hashlib.sha256((root / name).read_bytes()).hexdigest() + "  " + name + "\n"
    if (root / (name + ".sha256")).read_text() != line:
        raise SystemExit(f"Checksum mismatch: {name}")
    lines.append(line)
(root / "SHA256SUMS").write_text("".join(lines))
print("Verified all four release executables")
