import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


INSTALLER = Path(__file__).with_name("install.sh")


class InstallChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="salang-install-check-")
        self.root = Path(self.temporary.name)
        self.fake_bin = self.root / "tools"
        self.fake_bin.mkdir()
        self.prefix = self.root / "한글 폴더 ' 인용" / "installation"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.env = dict(os.environ, PATH=str(self.fake_bin) + os.pathsep + os.environ["PATH"],
                        SALANG_TEST_FIXTURE=str(self.fixture), SALANG_TEST_PLATFORM="Darwin", SALANG_TEST_ARCH="arm64")
        self.write_fixture("0.1.0")
        self.tool("uname", '''#!/bin/sh
case "$1" in -s) printf '%s\\n' "$SALANG_TEST_PLATFORM" ;; -m) printf '%s\\n' "$SALANG_TEST_ARCH" ;; *) exit 1 ;; esac
''')
        self.tool("curl", "#!" + sys.executable + "\n" + textwrap.dedent('''
            from pathlib import Path
            import os, sys
            args=sys.argv[1:]
            target=Path(args[args.index("-o")+1])
            url=next(value for value in args if value.startswith("https://"))
            assert url.startswith("https://github.com/100space/salang/releases/")
            name=url.rsplit("/",1)[-1]
            fixture=Path(os.environ["SALANG_TEST_FIXTURE"])
            with (fixture/"requests").open("a") as output: output.write(name+"\\n")
            source=fixture/("SHA256SUMS" if name=="SHA256SUMS" else "binary")
            target.write_bytes(source.read_bytes())
        '''))

    def tearDown(self):
        self.temporary.cleanup()

    def tool(self, name, source):
        path = self.fake_bin / name
        path.write_text(source)
        path.chmod(0o755)

    def write_fixture(self, version):
        binary = self.fixture / "binary"
        binary.write_text("#!/bin/sh\nprintf '%s\\n' 'salang " + version + "'\n")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        (self.fixture / "SHA256SUMS").write_text("".join(
            f"{digest}  salang-{system}-{arch}\n" for system in ("darwin", "linux") for arch in ("arm64", "amd64")))

    def install(self, *arguments):
        return subprocess.run(["/bin/sh", str(INSTALLER), "--prefix", str(self.prefix), "--no-path", *arguments],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def test_platform_detection_installation_and_upgrade(self):
        for system, arch, asset in (("Darwin", "arm64", "darwin-arm64"), ("Darwin", "x86_64", "darwin-amd64"),
                                    ("Linux", "aarch64", "linux-arm64"), ("Linux", "x86_64", "linux-amd64")):
            with self.subTest(system=system, arch=arch):
                self.env.update(SALANG_TEST_PLATFORM=system, SALANG_TEST_ARCH=arch)
                self.write_fixture("0.1.0")
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
                executable = self.prefix / "bin" / "salang"
                self.assertTrue(executable.is_symlink())
                self.assertEqual(subprocess.check_output([str(executable), "--version"], text=True).strip(), "salang 0.1.0")
                self.assertIn("salang-" + asset, (self.fixture / "requests").read_text())
        original = (self.prefix / "share" / "salang" / "salang").read_bytes()
        self.write_fixture("0.1.1")
        result = self.install("--version", "v0.1.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prefix / "share" / "salang" / "salang.previous").read_bytes(), original)

    def test_checksum_failure_preserves_existing_installation(self):
        self.assertEqual(self.install().returncode, 0)
        executable = self.prefix / "share" / "salang" / "salang"
        original = executable.read_bytes()
        (self.fixture / "binary").write_text("bad download must never run")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("검증에 실패", result.stderr)
        self.assertEqual(executable.read_bytes(), original)

    def test_unrelated_command_is_not_overwritten(self):
        binary = self.prefix / "bin" / "salang"
        binary.parent.mkdir(parents=True)
        binary.write_text("unrelated command")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(binary.read_text(), "unrelated command")
        self.assertFalse((self.fixture / "requests").exists())

    def test_unsupported_platform_and_invalid_version_stop_before_download(self):
        self.env["SALANG_TEST_PLATFORM"] = "MINGW64_NT"
        self.assertNotEqual(self.install().returncode, 0)
        self.env["SALANG_TEST_PLATFORM"] = "Darwin"
        self.assertNotEqual(self.install("--version", "../../unexpected").returncode, 0)
        self.assertFalse((self.fixture / "requests").exists())


if __name__ == "__main__":
    unittest.main()
