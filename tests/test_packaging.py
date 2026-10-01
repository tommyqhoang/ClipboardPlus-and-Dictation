"""Checksums for release artifacts must work on every OS runner."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import checksum

REPO_ROOT = Path(__file__).resolve().parents[1]


class ChecksumTests(unittest.TestCase):
    def test_sidecar_hashes_binary_content_and_uses_the_asset_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder) / "Clipboard+-Setup.exe"
            asset.write_bytes(b"\x00release\xff")
            sidecar = checksum.write_checksum(asset)
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="ascii"), f"{digest}  {asset.name}\n")


class MacBundleSigningTests(unittest.TestCase):
    """The DMG must not ship unsigned: Apple Silicon shows a quarantined
    unsigned app as "damaged and can't be opened" with no bypass on macOS
    15+, and dylibbundler invalidates Homebrew ffmpeg's signatures."""

    def test_build_dmg_adhoc_signs_and_verifies_every_macho(self):
        script = (REPO_ROOT / "packaging" / "macos" / "build-dmg.sh").read_text(encoding="utf-8")
        self.assertIn("codesign --force --sign -", script)
        self.assertIn('codesign --verify --deep --strict --verbose=2 "$APP"', script)
        # The per-file signing loop must re-sign dylibbundler-modified
        # binaries, i.e. iterate the whole bundle (executables in Contents/MacOS and the
        # relocated _internal in Contents/Resources), not just the top-level exe.
        self.assertIn('find "$APP/Contents" -type f', script)
        # codesign rejects PyInstaller's _internal folder inside Contents/MacOS, so it
        # lives in Resources with a symlink left behind.
        self.assertIn('ln -s ../Resources/_internal "$APP/Contents/MacOS/_internal"', script)
        # Only executables may remain in Contents/MacOS; data files there fail codesign.
        self.assertIn("grep -q 'Mach-O'", script)
        bundle_sign_pos = script.index('sign "$APP"')
        main_sign_pos = script.index('sign "$APP/Contents/MacOS/menubar"')
        loop_end_pos = script.index("done < <(find", script.index("while IFS= read"))
        verify_pos = script.index("codesign --verify")
        self.assertLess(loop_end_pos, main_sign_pos, "sign the main executable after its siblings")
        self.assertLess(
            main_sign_pos, bundle_sign_pos, "sign the main executable before the app bundle"
        )
        self.assertLess(bundle_sign_pos, verify_pos, "verification must follow bundling signing")

    def test_dmg_ships_a_double_click_installer_that_strips_quarantine(self):
        script = (REPO_ROOT / "packaging" / "macos" / "build-dmg.sh").read_text(encoding="utf-8")
        self.assertIn("Install Clipboard+.command", script)
        self.assertIn("xattr -dr com.apple.quarantine", script)
        # Gatekeeper does not assess .command scripts — the helper is the
        # unsigned build's first-launch story, so it must land in the DMG
        # staging folder, next to the app.
        helper_pos = script.index("Install Clipboard+.command")
        hdiutil_pos = script.index("hdiutil create")
        self.assertLess(
            helper_pos, hdiutil_pos, "installer helper must be staged before the DMG is created"
        )

    def test_developer_id_signing_is_conditional_and_hardened(self):
        script = (REPO_ROOT / "packaging" / "macos" / "build-dmg.sh").read_text(encoding="utf-8")
        self.assertIn("APPLE_SIGNING_IDENTITY", script)
        self.assertIn("--options runtime", script)
        self.assertIn("--timestamp", script)
        self.assertIn("entitlements.plist", script)
        entitlements = REPO_ROOT / "packaging" / "macos" / "entitlements.plist"
        self.assertIn("device.audio-input", entitlements.read_text(encoding="utf-8"))
        plist = (REPO_ROOT / "packaging" / "macos" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("LSMinimumSystemVersion", plist)


@unittest.skipIf(sys.platform == "win32", "installer helper runs with a POSIX shell")
class MacInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "Applications"
        self.destination.mkdir()
        self.target = self.destination / "Clipboard+.app"
        (self.target / "Contents").mkdir(parents=True)
        (self.target / "Contents/Info.plist").write_text("com.apercallc.clipboardplus")
        (self.target / "version").write_text("old")
        source = self.root / "Clipboard+.app"
        (source / "Contents").mkdir(parents=True)
        (source / "Contents/Info.plist").write_text("com.apercallc.clipboardplus")
        (source / "version").write_text("new")
        build = (REPO_ROOT / "packaging/macos/build-dmg.sh").read_text()
        helper = build.split("cat >\"$STAGING/Install Clipboard+.command\" <<'EOF'\n", 1)[1].split(
            "\nEOF", 1
        )[0]
        self.helper = self.root / "Install Clipboard+.command"
        self.helper.write_text(helper)
        commands = self.root / "bin"
        commands.mkdir()
        for name, script in {
            "plutil": 'for last; do :; done\ncat "$last"',
            "osascript": 'echo "${TEST_RUNNING:-false}"',
            "ditto": '[ "${TEST_FAIL:-}" != copy ] || exit 1\ncp -R "$1" "$2"',
            "codesign": '[ "${TEST_FAIL:-}" != signature ]',
            "xattr": "exit 0",
            "open": '[ "${TEST_FAIL:-}" != launch ]',
        }.items():
            command = commands / name
            command.write_text("#!/bin/sh\n" + script + "\n")
            command.chmod(0o755)
        self.env = os.environ | {"PATH": str(commands) + os.pathsep + os.environ.get("PATH", "")}

    def install(self, **environment):
        return subprocess.run(
            [shutil.which("bash"), str(self.helper), str(self.destination)],
            env=self.env | environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_success_replaces_the_bundle_only_after_copy_and_validation(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "version").read_text(), "new")
        self.assertEqual(list(self.destination.glob(".clipboardplus-install.*")), [])

    def test_failed_copy_signature_or_launch_preserves_the_previous_install(self):
        for failure in ("copy", "signature", "launch"):
            with self.subTest(failure=failure):
                result = self.install(TEST_FAIL=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.target / "version").read_text(), "old")
                self.assertEqual(list(self.destination.glob(".clipboardplus-install.*")), [])

    def test_running_app_or_unrelated_bundle_is_not_replaced(self):
        result = self.install(TEST_RUNNING="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Quit Clipboard+", result.stderr)
        (self.target / "Contents/Info.plist").write_text("another.application")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another app", result.stderr)
        self.assertEqual((self.target / "version").read_text(), "old")


def read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def app_version() -> str:
    match = re.search(r'^APP_VERSION = "([^"]+)"', read("lib", "desktop.py"), re.M)
    assert match
    return match.group(1)


class VersionConsistencyTests(unittest.TestCase):
    def test_pyproject_version_matches_app_version(self):
        match = re.search(r'^version = "([^"]+)"', read("pyproject.toml"), re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), app_version())

    def test_installers_take_their_version_from_app_version(self):
        # Info.plist is a template the DMG script fills from desktop.APP_VERSION.
        plist = read("packaging", "macos", "Info.plist")
        self.assertEqual(plist.count("__APP_VERSION__"), 2)
        self.assertIn("desktop.APP_VERSION", read("packaging", "macos", "build-dmg.sh"))
        # The Inno script refuses to build without /DAppVersion, which the release
        # workflow reads from desktop.APP_VERSION; nothing is hard-coded.
        iss = read("packaging", "windows", "clipboardplus.iss")
        self.assertIn("#error AppVersion must be supplied", iss)
        self.assertIn("AppVersion={#AppVersion}", iss)
        self.assertNotIn(app_version(), iss)
        self.assertIn("desktop.APP_VERSION", read(".github", "workflows", "release.yml"))

    def test_changelog_mentions_the_current_version(self):
        self.assertIn(f"[{app_version()}]", read("CHANGELOG.md"))


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.release = read(".github", "workflows", "release.yml")

    def test_two_release_runs_never_overlap_or_cancel_each_other(self):
        # The draft job deletes and recreates the draft, so runs queue instead.
        self.assertRegex(
            self.release,
            r"(?m)^concurrency:\n  group: release\n  cancel-in-progress: false\n",
        )

    def test_a_version_bump_on_main_releases_automatically(self):
        self.assertIn("branches: [main]", self.release)
        # No paths filter: a release that failed is retried by the next push.
        self.assertNotIn("paths:", self.release)
        self.assertIn("needs.plan.outputs.release == 'true'", self.release)
        self.assertIn("uses: ./.github/workflows/quality.yml", self.release)
        # Built as a draft and published last, so no half-built release is public.
        self.assertIn("--draft", self.release)
        self.assertIn("--draft=false", self.release)
        self.assertIn("workflow_call", read(".github", "workflows", "quality.yml"))

    def test_frozen_builds_bundle_the_keychain_library(self):
        for platform in ("linux", "macos", "windows"):
            spec = read("packaging", platform, "clipboardplus.spec")
            self.assertIn('collect_submodules("keyring")', spec)
            self.assertIn("*KEYRING_DATA", spec)

    def test_bump_script_updates_every_version_place(self):
        script = read("tools", "bump_version.py")
        for place in (
            "lib/desktop.py",
            "pyproject.toml",
            "bootstrap.sh",
            "bootstrap.ps1",
            "CHANGELOG.md",
        ):
            self.assertIn(place, script)

    def test_bump_command_updates_app_metadata_changelog_and_bootstrap_fallbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools").mkdir()
            script = root / "tools/bump_version.py"
            script.write_text(read("tools", "bump_version.py"), encoding="utf-8")
            sources = {
                "lib/desktop.py": 'APP_VERSION = "1.0.0"\n',
                "pyproject.toml": 'version = "1.0.0"\n',
                "bootstrap.sh": 'PINNED_TAG="v1.0.0"\n',
                "bootstrap.ps1": "$PinnedTag = 'v1.0.0'\n",
                "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n",
            }
            for relative, source in sources.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(script), "2.3.4"], capture_output=True, text=True
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('APP_VERSION = "2.3.4"', (root / "lib/desktop.py").read_text())
            self.assertIn('version = "2.3.4"', (root / "pyproject.toml").read_text())
            self.assertIn('PINNED_TAG="v2.3.4"', (root / "bootstrap.sh").read_text())
            self.assertIn("$PinnedTag = 'v2.3.4'", (root / "bootstrap.ps1").read_text())
            self.assertIn("## [2.3.4] - ", (root / "CHANGELOG.md").read_text())

    def test_downloads_are_pinned_and_hash_verified(self):
        self.assertNotIn("/continuous/", self.release)
        self.assertNotIn("ffmpeg-release-amd64-static", self.release)
        self.assertNotIn("ffmpeg-release-essentials", self.release)
        for name in ("FFMPEG_LINUX_SHA256", "FFMPEG_WINDOWS_SHA256", "APPIMAGETOOL_SHA256"):
            self.assertRegex(self.release, rf"{name}: [0-9a-f]{{64}}")
        self.assertIn("sha256sum -c -", self.release)
        self.assertIn("Get-FileHash", self.release)

    def test_gui_and_build_pins_live_in_requirements_files_only(self):
        gui = read("requirements-gui.txt")
        self.assertRegex(read("requirements-release.txt"), r"pyinstaller==\d")
        self.assertIn("-r requirements-gui.txt", read("requirements-release.txt"))
        self.assertIn("Pillow==", gui)
        quality = read(".github", "workflows", "quality.yml")
        for workflow in (self.release, quality):
            self.assertNotIn("pyobjc-framework-Cocoa==", workflow)
            self.assertNotIn("pystray==", workflow)
        self.assertIn("requirements-gui.txt", quality)

    def test_macos_releases_build_native_installers_for_both_architectures(self):
        quality = read(".github", "workflows", "quality.yml")
        self.assertIn("macos-latest", self.release)  # Apple silicon build.
        self.assertIn("macos-15-intel", self.release)
        self.assertIn("Clipboard+-macOS-arm64.dmg", self.release)
        self.assertIn("Clipboard+-macOS-x86_64.dmg", self.release)
        self.assertIn("macos-15-intel", quality)

    def test_packaging_docs_match_release_architectures_and_triggers(self):
        docs = read("packaging", "README.md")
        self.assertIn("macOS Intel", docs)
        self.assertIn("Apple silicon (arm64)", docs)
        self.assertNotIn("There is no Intel Mac", docs)
        self.assertIn("A push to `main` or a manual `workflow_dispatch`", docs)
        self.assertIn("only after all installer assets and checksums are ready", docs)
        self.assertNotIn("A `v*` tag runs `release.yml`", docs)

    def test_signing_steps_only_run_when_secrets_exist(self):
        for secret in (
            "APPLE_CERTIFICATE",
            "APPLE_ID",
            "AZURE_CLIENT_SECRET",
            "WINDOWS_CERTIFICATE",
        ):
            self.assertIn(f"secrets.{secret}", self.release)
        self.assertIn("notarytool submit", self.release)
        self.assertIn("stapler staple", self.release)
        self.assertIn("steps.signing.outputs.apple == 'true'", self.release)

    def test_ci_security_workflows_exist(self):
        self.assertIn("python", read(".github", "workflows", "codeql.yml"))
        dependabot = read(".github", "dependabot.yml")
        for ecosystem in ("github-actions", "pip"):
            self.assertIn(ecosystem, dependabot)
        self.assertIn("pip-audit", read(".github", "workflows", "quality.yml"))


class LicensingTests(unittest.TestCase):
    def test_license_files_exist_and_are_declared(self):
        self.assertIn("MIT License", read("LICENSE"))
        notices = read("THIRD-PARTY-NOTICES.md")
        for needle in ("whisper.cpp", "FFmpeg", "GNU General Public License", "Source offer"):
            self.assertIn(needle, notices)
        self.assertIn('license = "MIT"', read("pyproject.toml"))

    def test_every_installer_ships_the_notices(self):
        for path in (
            ("packaging", "linux", "build-appimage.sh"),
            ("packaging", "macos", "build-dmg.sh"),
            (".github", "workflows", "release.yml"),
        ):
            self.assertIn("THIRD-PARTY-NOTICES.md", read(*path), path)
        iss = read("packaging", "windows", "clipboardplus.iss")
        self.assertIn("LicenseFile=", iss)
        self.assertIn("THIRD-PARTY-NOTICES.md", iss)


class InstallerScriptTests(unittest.TestCase):
    def test_inno_setup_identity_and_uninstall_prompt(self):
        iss = read("packaging", "windows", "clipboardplus.iss")
        self.assertRegex(iss, r"AppId=\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}")
        for key in ("AppPublisherURL=", "VersionInfoVersion=", "AppSupportURL="):
            self.assertIn(key, iss)
        self.assertIn("CurUninstallStepChanged", iss)
        self.assertIn("MB_DEFBUTTON2", iss)  # Keeping the data is the default answer.

    def test_uninstallers_cover_every_module_and_offer_a_purge(self):
        modules = {p.stem for p in (REPO_ROOT / "lib").glob("*.py")}
        for script in ("uninstall.sh", "uninstall.ps1"):
            text = read(script)
            missing = {m for m in modules if not re.search(rf"\b{m}\b", text)}
            self.assertFalse(missing, f"{script} does not remove {sorted(missing)}")
        self.assertIn("--purge", read("uninstall.sh"))
        self.assertIn("-Purge", read("uninstall.ps1"))
        self.assertIn("telemetry-id", read("uninstall.sh") + read("uninstall.ps1"))

    def test_packaged_uninstallers_are_shipped(self):
        self.assertIn("uninstall-appimage.sh", read("packaging", "linux", "build-appimage.sh"))
        self.assertIn("--uninstall", read("packaging", "linux", "AppDir", "AppRun"))
        self.assertIn("Uninstall Clipboard+.command", read("packaging", "macos", "build-dmg.sh"))


if __name__ == "__main__":
    unittest.main()
