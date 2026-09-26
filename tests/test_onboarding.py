from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation
import onboarding


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.paths = dictation.Paths()
        self.model = root / "custom.bin"
        self.model.write_bytes(b"lmgg-test")

    def wizard(self, answers, platform="linux"):
        with (
            patch("builtins.input", side_effect=answers),
            patch.object(onboarding.desktop, "platform_name", return_value=platform),
            patch.object(onboarding.subprocess, "run") as runner,
            patch.object(dictation.Config, "check"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            onboarding.run(self.paths)
        return runner, output.getvalue()

    def test_custom_model_and_platform_walkthroughs(self):
        for platform in ("linux", "macos", "windows"):
            self.paths.config.unlink(missing_ok=True)
            runner, output = self.wizard(["2", str(self.model), "USB Mic", "Codex", "y"], platform)
            saved = json.loads(self.paths.config.read_text())
            self.assertEqual(saved["language"], "auto")
            self.assertEqual(saved["device"], "USB Mic")
            self.assertEqual(saved["model"], str(self.model.resolve()))
            self.assertTrue(saved["live"])
            self.assertFalse(saved["allow_remote"])
            self.assertEqual(runner.call_count, 1)  # device enumeration only
            args = runner.call_args.args[0]
            self.assertTrue("-L" in args or "-list_devices" in args)
            self.assertIn("Nothing has been recorded", output)

    def test_download_default_and_existing_settings_preserved(self):
        # Desktop installation creates blank defaults; first-run setup must not
        # mistake them for a completed configuration and default to cancelling.
        dictation.private_dir(self.paths.config.parent)
        dictation.atomic(self.paths.config, json.dumps(dictation.DEFAULTS))
        with patch.object(onboarding, "download_model", return_value=self.model) as download:
            self.wizard(["", "", "", "", ""])
            self.assertEqual(download.call_args.args[1], "en")
        before = self.paths.config.read_bytes()
        self.wizard([""])
        self.assertEqual(self.paths.config.read_bytes(), before)
        self.wizard(["y", "1", str(self.model), "", "", ""])

    def test_invalid_inputs_do_not_write_settings(self):
        for answers, platform in (
            (["3"], "linux"),
            (["1", "/missing/model"], "linux"),
            (["1", str(self.model), ""], "windows"),
        ):
            with self.assertRaises(dictation.DictationError):
                self.wizard(answers, platform)
            self.assertFalse(self.paths.config.exists())
        invalid_model = Path(self.temp.name) / "not-a-model.bin"
        invalid_model.write_bytes(b"not a GGML file")
        with self.assertRaisesRegex(dictation.DictationError, "not a whisper.cpp GGML"):
            self.wizard(["1", str(invalid_model)])
        self.assertFalse(self.paths.config.exists())
        with patch.object(dictation, "busy", return_value=True):
            with self.assertRaises(dictation.DictationError):
                self.wizard([])

    def test_verified_download_reuse_and_mismatch(self):
        data = b"lmgg-model-test"
        folder = Path(self.temp.name) / "models"
        with (
            patch.dict(onboarding.MODELS, {"en": ("base.en", hashlib.sha256(data).hexdigest())}),
            patch.object(
                onboarding.urllib.request, "urlopen", return_value=io.BytesIO(data)
            ) as request,
        ):
            path = onboarding.download_model(folder, "en")
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(onboarding.download_model(folder, "en"), path)
            self.assertEqual(request.call_count, 1)
            path.write_bytes(b"corrupt")
            with self.assertRaises(dictation.DictationError):
                onboarding.download_model(folder, "en")
            path.unlink()
        with patch.object(onboarding.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaises(dictation.DictationError):
                onboarding.download_model(folder, "en")
        self.assertEqual(list(folder.iterdir()), [])

    def test_download_network_failure_cleans_partial(self):
        folder = Path(self.temp.name) / "models"
        with patch.object(onboarding.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                onboarding.download_model(folder, "auto")
        self.assertEqual(list(folder.iterdir()), [])

    def test_cli_setup_errors_are_friendly(self):
        result = subprocess.run(
            [sys.executable, str(Path(dictation.__file__)), "--setup"],
            input="3\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Choose 1 or 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
