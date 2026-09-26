"""Exercise install.sh dependency logic without touching the host."""

from __future__ import annotations

import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == "win32", "Unix installer")
class InstallDependencyTests(unittest.TestCase):
    def run_script(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f"set -euo pipefail\nsource {shlex.quote(str(ROOT / 'install.sh'))}\n{body}\n"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_every_supported_family_lists_tk_gtk_and_build_tools(self):
        for manager, tk, appindicator, compiler in (
            ("apt-get", "python3-tk", "gir1.2-ayatanaappindicator3-0.1", "build-essential"),
            ("dnf", "python3-tkinter", "libayatana-appindicator-gtk3", "gcc-c++"),
            ("pacman", "tk", "libayatana-appindicator", "base-devel"),
            ("zypper", "python3-tk", "typelib-1_0-AyatanaAppIndicator3-0_1", "gcc-c++"),
        ):
            with self.subTest(manager=manager):
                result = self.run_script(
                    f"PM={manager}; select_packages; "
                    'echo "${RUNTIME_PACKAGES[*]}"; echo "${BUILD_PACKAGES[*]}"'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                runtime, build = result.stdout.splitlines()
                for package in (tk, appindicator, "wl-clipboard", "curl"):
                    self.assertIn(package, runtime.split())
                self.assertIn(compiler, build.split())
                self.assertIn("cmake", build.split())

    def test_unknown_manager_has_no_package_list(self):
        self.assertNotEqual(self.run_script("PM=apk; select_packages").returncode, 0)

    def test_package_manager_detection_order_and_absence(self):
        found = self.run_script(
            'need() { [[ "$1" == dnf || "$1" == zypper ]]; }; detect_package_manager; echo "$PM"'
        )
        self.assertEqual(found.stdout.strip(), "dnf")
        absent = self.run_script("need() { return 1; }; detect_package_manager")
        self.assertNotEqual(absent.returncode, 0)

    def test_unsupported_system_says_what_to_install(self):
        result = self.run_script("unsupported_system")
        self.assertIn("--no-packages", result.stderr)
        self.assertIn("tkinter", result.stderr)
        self.assertIn("whisper-cli", result.stderr)

    def test_ready_machine_installs_nothing(self):
        result = self.run_script(
            "need() { return 0; }; python_has() { return 0; }; typelib_available() { return 0; }; "
            "find_python() { PYTHON=/usr/bin/python3; }; "
            "pm_install() { echo INSTALLED; }; SKIP_MODEL=1; install_packages"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("INSTALLED", result.stdout)
        self.assertIn("already installed", result.stdout)

    def test_missing_tk_triggers_install_then_rechecks(self):
        result = self.run_script(
            "need() { return 0; }; typelib_available() { return 0; }; "
            "find_python() { PYTHON=/usr/bin/python3; }; "
            "detect_package_manager() { PM=dnf; }; "
            'python_has() { [[ -f "$MARK" ]]; }; '
            'pm_install() { echo "INSTALLED $*"; touch "$MARK"; }; '
            'MARK="$(mktemp -u)"; SKIP_MODEL=1; install_packages; rm -f "$MARK"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"INSTALLED .*python3-tkinter")

    def test_failed_install_is_reported_not_ignored(self):
        result = self.run_script(
            "need() { return 0; }; typelib_available() { return 0; }; "
            "find_python() { PYTHON=/usr/bin/python3; }; python_has() { return 1; }; "
            "detect_package_manager() { PM=dnf; }; pm_install() { :; }; "
            "SKIP_MODEL=1; install_packages"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still missing", result.stderr)

    def test_find_python_prefers_interpreter_with_tk(self):
        result = self.run_script(
            'need() { return 0; }; python_has() { [[ "$1" == /usr/bin/python3 ]]; }; '
            'command() { echo /opt/brew/bin/python3; }; find_python; echo "$PYTHON"'
        )
        self.assertEqual(result.stdout.strip(), "/usr/bin/python3")

    def test_only_lightweight_whisper_packages_are_used(self):
        # Fedora's whisper-cpp pulls PyTorch and ROCm (8 GiB); never install it.
        for manager, expected in (
            ("apt-get", "whisper.cpp"),
            ("dnf", ""),
            ("pacman", ""),
            ("zypper", ""),
        ):
            with self.subTest(manager=manager):
                result = self.run_script(f'PM={manager}; select_packages; echo "$WHISPER_PACKAGE"')
                self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
