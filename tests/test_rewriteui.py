"""In-app setup and error feedback for concise drafts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import rewriteui
import rewriting


class SettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        import tkinter as tk

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "HOME": str(root / "home"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.config.parent)
        d.atomic(
            self.paths.config,
            json.dumps(
                {
                    "rewrite_endpoint": rewriteui.SettingsDialog.DEFAULT_ENDPOINT,
                    "rewrite_model": "llama3",
                }
            ),
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.on_saved = Mock()

    def dialog(self, *, make_after_save=False):
        return rewriteui.SettingsDialog(
            self.root,
            d.Config(self.paths),
            self.paths,
            self.on_saved,
            make_after_save=make_after_save,
        )

    def test_save_persists_settings_and_private_key_then_calls_back(self):
        dialog = self.dialog(make_after_save=True)
        self.assertEqual(dialog.endpoint.get(), rewriteui.SettingsDialog.DEFAULT_ENDPOINT)
        self.assertEqual(dialog.model.get(), "llama3")
        self.assertEqual(dialog.window.title(), "Concise drafts")
        self.assertEqual(dialog.endpoint_entry.cget("show"), "")
        dialog.api_key.set("  local-secret  ")
        dialog.save()

        self.assertEqual(rewriting.key_path(self.paths).read_text(), "local-secret")
        saved = d.read_json(self.paths.config)
        self.assertEqual(saved["rewrite_model"], "llama3")
        self.on_saved.assert_called_once_with()
        self.assertFalse(dialog.window.winfo_exists())

    def test_invalid_remote_service_stays_open_and_shows_the_problem(self):
        dialog = self.dialog()
        dialog.endpoint.set("https://api.example.com/v1/chat/completions")
        dialog.save()

        self.assertIn("allow sending", dialog.error.get())
        self.assertTrue(dialog.window.winfo_exists())
        self.on_saved.assert_not_called()

    def test_saved_key_can_be_kept_or_removed_explicitly(self):
        rewriting.key_path(self.paths).write_text("existing-secret")
        dialog = self.dialog()
        labels = [
            str(widget.cget("text"))
            for widget in dialog.window.winfo_children()[0].winfo_children()
            if widget.winfo_class() in ("TLabel", "TCheckbutton")
        ]
        self.assertTrue(any("key is saved privately" in label for label in labels))
        self.assertIn("Remove the saved key", labels)
        self.assertEqual(dialog.api_key.get(), "")
        dialog.save()
        self.assertEqual(rewriting.key_path(self.paths).read_text(), "existing-secret")

        dialog = self.dialog()
        dialog.clear_key.set(True)
        dialog.save()
        self.assertFalse(rewriting.has_saved_key(self.paths))
        self.assertEqual(self.on_saved.call_count, 2)

    def test_save_reports_filesystem_errors_and_cancel_is_safe_twice(self):
        dialog = self.dialog()
        with patch.object(rewriting, "save_settings", side_effect=OSError("read only")):
            dialog.save()
        self.assertEqual(dialog.error.get(), "read only")
        self.on_saved.assert_not_called()
        dialog.close()
        dialog.close()
        self.assertFalse(dialog.window.winfo_exists())


if __name__ == "__main__":
    unittest.main()
