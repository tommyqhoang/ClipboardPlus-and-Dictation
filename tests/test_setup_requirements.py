"""setup-desktop.py installs its GUI dependencies from the pinned requirements file."""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def setup_module():
    spec = importlib.util.spec_from_file_location("setup_desktop_req", ROOT / "setup-desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuiRequirementsTests(unittest.TestCase):
    def test_pip_installs_from_the_pinned_requirements_file(self):
        setup = setup_module()
        for platform in ("macos", "linux", "windows"):
            self.assertEqual(
                setup.gui_install_arguments(platform),
                ["-r", str(ROOT / "requirements-gui.txt")],
            )

    def test_every_requirement_is_an_exact_pin(self):
        lines = [
            line.split(";")[0].strip()
            for line in (ROOT / "requirements-gui.txt").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9._-]+==\d[\w.]*$")

    def test_inline_fallback_pins_match_the_file(self):
        setup = setup_module()
        text = (ROOT / "requirements-gui.txt").read_text()
        for pins in setup.GUI_REQUIREMENTS.values():
            for pin in pins:
                self.assertTrue(re.search(r"^" + re.escape(pin) + r"\b", text, re.M), pin)

    def test_without_the_file_the_inline_pins_are_used(self):
        setup = setup_module()
        with patch.object(setup, "GUI_REQUIREMENTS_FILE", ROOT / "missing-requirements.txt"):
            self.assertEqual(
                setup.gui_install_arguments("linux"), list(setup.GUI_REQUIREMENTS["linux"])
            )

    def test_the_environment_build_passes_the_file_to_pip(self):
        setup = setup_module()
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "base_python", return_value="python3"),
                patch.object(setup.subprocess, "run") as run,
            ):
                setup.gui_environment(prefix)
        pip = next(c.args[0] for c in run.call_args_list if "pip" in c.args[0])
        self.assertIn("-r", pip)
        self.assertIn(str(ROOT / "requirements-gui.txt"), pip)


if __name__ == "__main__":
    unittest.main()
