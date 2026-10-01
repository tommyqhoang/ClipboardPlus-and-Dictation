"""Exercise install.sh dependency logic without touching the host."""

from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
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
                for package in (tk, appindicator, "wl-clipboard", "xclip", "curl"):
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
            "python_supported() { return 0; }; "
            'need() { return 0; }; python_has() { [[ "$1" == /usr/bin/python3 ]]; }; '
            'command() { echo /opt/brew/bin/python3; }; find_python; echo "$PYTHON"'
        )
        self.assertEqual(result.stdout.strip(), "/usr/bin/python3")

    def test_find_python_skips_old_system_python_even_without_gui_packages(self):
        result = self.run_script(
            "need() { return 0; }; python_has() { return 1; }; "
            'python_supported() { [[ "$1" != /usr/bin/python3 ]]; }; '
            'command() { echo /opt/brew/bin/python3; }; find_python; echo "$PYTHON"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/opt/brew/bin/python3")

    def test_find_python_fails_when_no_supported_interpreter_exists(self):
        result = self.run_script(
            "need() { return 0; }; python_supported() { return 1; }; "
            "command() { echo /opt/brew/bin/python3; }; find_python"
        )
        self.assertNotEqual(result.returncode, 0)

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

    def test_speech_failure_keeps_desktop_install_available(self):
        result = self.run_script(
            "runtime_missing() { return 1; }; install_optional_packages() { :; }; "
            "install_whisper() { return 1; }; install_packages; "
            'echo "skip download=$SKIP_DOWNLOAD"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still install", result.stderr)
        self.assertIn("skip download=1", result.stdout)

    def test_optional_helpers_fail_independently(self):
        result = self.run_script(
            "detect_package_manager() { PM=apt-get; }; need() { return 1; }; "
            'pm_install() { echo "ATTEMPT $*"; return 1; }; '
            "XDG_CURRENT_DESKTOP=GNOME; install_optional_packages"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for package in (
            "wtype",
            "xdotool",
            "x11-xserver-utils",
            "gnome-shell-extension-appindicator",
        ):
            self.assertIn("ATTEMPT " + package, result.stdout)

    def test_main_continues_after_missing_engine_and_failed_model(self):
        result = self.run_script(
            'find_python() { PYTHON="' + sys.executable + '"; }; '
            "need() { return 1; }; install_model() { return 1; }; "
            "install_script() { echo DESKTOP_INSTALLED; }; install_gnome_shortcut() { :; }; "
            "main --no-packages"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DESKTOP_INSTALLED", result.stdout)
        self.assertIn("Speech model installation failed", result.stderr)

    def test_model_validation_failure_never_selects_invalid_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "ggml-base.en.bin"
            model.write_bytes(b"not a model")
            result = self.run_script(
                f"MODEL_DIR={shlex.quote(folder)}; "
                f"MODEL_DEST={shlex.quote(str(model))}; "
                f"MODEL_LINK={shlex.quote(str(root / 'selected.bin'))}; "
                f"PYTHON={shlex.quote(sys.executable)}; "
                "if ! install_model; then echo REJECTED; fi"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("REJECTED", result.stdout)
            self.assertFalse((root / "selected.bin").is_symlink())

    def model_script(self, folder: str, extra: str = "") -> str:
        root = Path(folder)
        return (
            f"MODEL_DIR={shlex.quote(folder)}; MODEL_NAME=ggml-custom.bin; "
            f"MODEL_DEST={shlex.quote(str(root / 'ggml-custom.bin'))}; "
            f"MODEL_LINK={shlex.quote(str(root / 'selected.bin'))}; "
            f"PYTHON={shlex.quote(sys.executable)}; "
            "download_model() { printf 'lmggDATA' > \"$2\"; }; " + extra
        )

    def test_custom_model_without_a_checksum_is_refused_unless_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            refused = self.run_script(
                self.model_script(folder, "set +e; install_model; echo RC=$?")
            )
            self.assertIn("RC=1", refused.stdout)
            self.assertIn("--allow-unverified-model", refused.stderr)
            self.assertFalse((Path(folder) / "ggml-custom.bin").exists())
            allowed = self.run_script(
                self.model_script(
                    folder, "set +e; ALLOW_UNVERIFIED_MODEL=1; install_model; echo RC=$?"
                )
            )
            self.assertIn("RC=0", allowed.stdout)
            self.assertTrue((Path(folder) / "ggml-custom.bin").exists())

    def test_the_flag_is_accepted_by_main(self):
        result = self.run_script("main --help; echo $ALLOW_UNVERIFIED_MODEL")
        self.assertIn("--allow-unverified-model", result.stdout)

    def run_resume(self, server: str, partial: bytes = b"lmgg-AAAA") -> tuple[str, bytes]:
        """download_model against a fake curl; returns (stdout, final file)."""
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "m.part"
            dest.write_bytes(partial)
            curl = (
                "curl() { local out='' hdr='' range='' prev='' a; "
                'for a in "$@"; do [[ "$prev" == --output ]] && out="$a"; '
                '[[ "$prev" == --dump-header ]] && hdr="$a"; '
                '[[ "$prev" == --header ]] && range="$a"; prev="$a"; done; ' + server + "; }; "
            )
            result = self.run_script(
                curl + f"download_model https://x/model {shlex.quote(str(dest))}; echo rc=$?"
            )
            self.assertIn("rc=0", result.stdout, result.stderr)
            return result.stdout, dest.read_bytes()

    def test_resume_appends_only_on_a_matching_206(self):
        _, data = self.run_resume(
            "printf 'HTTP/2 206\\r\\ncontent-range: bytes 9-13/14\\r\\n\\r\\n' > \"$hdr\"; "
            'printf BBBB > "$out"'
        )
        self.assertEqual(data, b"lmgg-AAAABBBB")

    def test_resume_restarts_when_the_range_does_not_match(self):
        for headers in (
            "HTTP/2 206\\r\\ncontent-range: bytes 0-3/4\\r\\n\\r\\n",
            "HTTP/2 416\\r\\n\\r\\n",
        ):
            with self.subTest(headers=headers):
                out, data = self.run_resume(
                    'if [[ -n "$range" ]]; then '
                    f'printf \'{headers}\' > "$hdr"; printf JUNK > "$out"; '
                    'else printf FULLFILE > "$out"; fi'
                )
                self.assertEqual(data, b"FULLFILE")
                self.assertIn("starting over", out)

    def test_resume_takes_the_whole_file_when_the_server_ignores_the_range(self):
        _, data = self.run_resume(
            'printf \'HTTP/2 200\\r\\n\\r\\n\' > "$hdr"; printf FULLFILE > "$out"'
        )
        self.assertEqual(data, b"FULLFILE")

    def test_whisper_source_is_fetched_by_pinned_commit(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertRegex(text, r'WHISPER_COMMIT="[0-9a-f]{40}"')
        self.assertIn(
            'fetch -q --depth 1 https://github.com/ggml-org/whisper.cpp.git "$WHISPER_COMMIT"', text
        )
        self.assertIn('rev-parse HEAD)" != "$WHISPER_COMMIT"', text)


if __name__ == "__main__":
    unittest.main()
