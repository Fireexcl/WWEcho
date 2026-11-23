import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FAKE_PYTHON = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
    -c)
        case "$2" in
            'import sys;'*) [ "${FAKE_BAD_VERSION:-0}" != 1 ] ;;
            'import numpy,'*) [ -f "$FAKE_DEPENDENCIES" ] ;;
            *) exit 1 ;;
        esac
        ;;
    -m)
        case "$2" in
            venv)
                mkdir -p "$3/bin"
                cp "$0" "$3/bin/python"
                ;;
            pip)
                [ "${FAKE_PIP_FAIL:-0}" != 1 ] || exit 1
                if [ "${FAKE_PIP_NO_DEPS:-0}" != 1 ]; then
                    touch "$FAKE_DEPENDENCIES"
                fi
                ;;
            src.*.echo_gui) exit 0 ;;
            *) exit 1 ;;
        esac
        ;;
    *) exit 1 ;;
esac
"""


@unittest.skipIf(os.name == "nt", "POSIX launch scripts")
class LaunchScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "bin").mkdir()
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        python = self.fake_bin / "python3"
        python.write_text(FAKE_PYTHON, encoding="utf-8")
        python.chmod(0o755)
        self.log = self.root / "calls.log"
        self.dependencies = self.root / "dependencies"
        self.env = {
            **os.environ,
            "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log),
            "FAKE_DEPENDENCIES": str(self.dependencies),
        }

    def run_script(self, edition, **environment):
        script = self.root / "bin" / f"run-{edition}.sh"
        shutil.copy2(ROOT / "bin" / script.name, script)
        return subprocess.run(
            ["bash", str(script), "--test-argument"],
            cwd=self.fake_bin,
            env={**self.env, **environment},
            text=True,
            capture_output=True,
        )

    def test_creates_and_reuses_environment_and_installs_only_missing_dependencies(self):
        for edition in ("lite", "full"):
            with self.subTest(edition=edition):
                self.log.unlink(missing_ok=True)
                self.dependencies.unlink(missing_ok=True)
                first = self.run_script(edition)
                self.assertEqual(first.returncode, 0, first.stderr)
                second = self.run_script(edition)
                self.assertEqual(second.returncode, 0, second.stderr)
                calls = self.log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(sum(call.startswith("-m venv ") for call in calls), 1)
                self.assertEqual(sum(call.startswith("-m pip install ") for call in calls), 1)
                expected_requirements = (
                    "requirements.txt" if edition == "lite"
                    else "src/full/requirements-ocr.txt"
                )
                self.assertIn(f"-m pip install -r {self.root / expected_requirements}", calls)
                self.assertEqual(calls.count(f"-m src.{edition}.echo_gui --test-argument"), 2)
                self.dependencies.unlink()
                third = self.run_script(edition)
                self.assertEqual(third.returncode, 0, third.stderr)
                calls = self.log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(sum(call.startswith("-m venv ") for call in calls), 1)
                self.assertEqual(sum(call.startswith("-m pip install ") for call in calls), 2)

    def test_failed_installation_does_not_launch(self):
        for edition in ("lite", "full"):
            for failure in ("FAKE_PIP_FAIL", "FAKE_PIP_NO_DEPS"):
                with self.subTest(edition=edition, failure=failure):
                    self.log.unlink(missing_ok=True)
                    result = self.run_script(edition, **{failure: "1"})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("echo_gui", self.log.read_text(encoding="utf-8"))

    def test_incompatible_python_does_not_create_environment(self):
        for edition in ("lite", "full"):
            with self.subTest(edition=edition):
                self.log.unlink(missing_ok=True)
                result = self.run_script(edition, FAKE_BAD_VERSION="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("-m venv", self.log.read_text(encoding="utf-8"))
