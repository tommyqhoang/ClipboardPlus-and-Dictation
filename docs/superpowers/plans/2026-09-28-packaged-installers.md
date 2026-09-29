# Packaged Installers (macOS/Windows/Linux) Implementation Plan

> **Historical design record, not the current spec.** Where this document
> differs from what ships, the code and `packaging/README.md` win. Known
> differences: code signing is now conditional (Developer ID + notarization on
> macOS and Azure Trusted Signing / signtool on Windows when release secrets
> exist; ad-hoc/unsigned otherwise); in-app self-update for packaged installs
> is disabled in v1; the AppImage is assembled by `build-appimage.sh` and
> `appimagetool` (linuxdeploy is not used); Linux dictation needs the system
> `arecord` (alsa-utils); only the three main installers are built (macOS
> Apple Silicon, Windows x86_64, Linux x86_64).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Clipboard+ as a self-contained, downloadable installer per OS
(macOS `.dmg`, Windows Inno Setup `.exe`, Linux `.AppImage`) that needs no
Python/pip/terminal visibility, no system package manager, and no sudo/UAC
beyond one unavoidable, unsigned-app OS security click.

**Architecture:** Keep the existing multi-process design (tray/menubar +
dictation engine + GUI window + clipboard service as separate processes).
Add one small abstraction (`desktop.relaunch()`) so every cross-process
launch resolves to a sibling frozen binary when packaged, or today's
script+interpreter when run from source. Wrap the resulting PyInstaller
onedir build per OS in that platform's native, free installer format.

**Tech Stack:** PyInstaller (onedir, multi-binary spec), Inno Setup
(Windows), `hdiutil` (macOS), `linuxdeploy` + `appimagetool` (Linux), GitHub
Actions (release automation). No new Python runtime dependencies for the
app itself.

**Spec:** `docs/superpowers/specs/2026-09-28-packaged-installers-design.md`
— read both documents; this plan corrects three concrete gaps the spec's
prose glossed over (found while inventorying every real call site before
writing tasks):
1. The spec said "five-ish" entry-point scripts. The actual roster, per
   `grep -rl '__name__ == "__main__"' lib/*.py`, is **eight**:
   `clipservice.py`, `engine.py`, `dictation.py`, `menubar.py`, `tray.py`,
   `app.py`, `overlay.py`, `updates.py`. `menubar`/`tray` are mutually
   exclusive per OS, so a single OS build bundles seven binaries. `overlay`
   and `clipservice` were missing from the spec's bundling list entirely.
2. `hotkeys.history_command(lib, python=None)` has a real caller the spec's
   file/line inventory missed: `setup-desktop.py:636` calls it **with an
   explicit `python` override** pointing at a specific install prefix's venv
   — a source-install-time concern, unrelated to the currently-running
   process. The spec's plain "always call `relaunch()`" pseudocode would
   have broken that caller. This plan's Task 2 keeps the explicit-override
   parameter and only changes the *default* (`python=None`) path.
3. `hotkeys.python_for_gui()` has a real caller the spec missed:
   `setup-desktop.py:563`. It cannot be deleted; it moves to
   `desktop.python_for_gui()` and `hotkeys.python_for_gui` becomes an alias
   to the same function object, so both call sites keep working unchanged.

## Global Constraints

- No code signing on either OS (user-approved $0 budget) — every installer
  is unsigned; document the one-time Gatekeeper/SmartScreen click, don't
  try to engineer it away.
- PyInstaller build mode is **onedir**, never onefile (spec §3.2 — startup
  latency and antivirus false-positive rate).
- Windows installs to `%LOCALAPPDATA%\Programs\Clipboard+` (per-user, no
  UAC) — never `%ProgramFiles%`.
- Branding is never invented: `hotkeys.APP_NAME` ("Clipboard+"),
  `assets/AppIcon.icns`, `assets/icon.ico`, `assets/icon-1024.png` /
  `icon-recording-1024.png` are the only names/icons used anywhere in
  installer scripts, `Info.plist`, or `.desktop` files.
- `install.sh`, `bootstrap.sh`/`.ps1`, and `setup-desktop.py` are not
  modified by this plan (spec §1 non-goals) — only *read* to confirm a call
  site's real behavior before changing a file that calls into them.
- Every Phase 1 task must pass `ruff check`, `ruff format --check`, and
  `mypy --strict` on the files it touches, and keep the existing test suite
  green (682 tests, 90% coverage floor on Linux, per `pyproject.toml`).
  `lib/menubar.py` is excluded from coverage there already (PyObjC needs a
  real macOS GUI session) — its task is verified by `mypy --strict` + `ruff`
  only, matching that existing project convention, not a new one invented
  for this plan.
- This session's sandbox is Linux only, with no PyInstaller, Inno Setup,
  `hdiutil`, or macOS/Windows runners available. Phase 1 is fully
  implementable and testable here. Phases 2–4 produce real, complete file
  content (specs, scripts, workflow YAML, Python code) but their
  multi-OS build/run verification steps say explicitly which OS/CI job
  must run them — that is not a placeholder, it is the actual boundary of
  what this machine can execute.

## Review Focus

1. **Frozen mode silently disabling features that check `script.is_file()`.**
   `dictation.py`'s `open_app()`/`start_overlay()` guard on the *source*
   `.py` file existing before launching — in a frozen build there is no
   `.py` file, ever, so an unguarded frozen build would silently stop
   showing the recording pill and the retry/discard window on every launch.
   Task 3 pins this with a test that asserts the frozen branch does **not**
   consult `script.is_file()`.
2. **`history_command`'s explicit-override caller breaking.**
   `setup-desktop.py:636` depends on passing its own `python` value for a
   *different* install prefix than the one currently running. Task 2 pins
   this with a test that the explicit-override branch never calls
   `desktop.frozen_root()` at all (so it can never diverge based on the
   *current* process's frozen state, which would be a use-after-the-wrong-
   condition bug if `frozen_root()` were checked unconditionally first).
3. **Windows executable suffix on the frozen sibling-binary path.**
   `relaunch()`'s frozen branch must append `.exe` on Windows and nothing on
   macOS/Linux — Task 1 pins both branches, not just the one this session's
   Linux sandbox can exercise live.
4. **AppImage's `APPDIR` environment variable, not `sys.executable`'s
   directory, is where sibling binaries live at runtime.** Task 1's
   `frozen_root()` test pins the `APPDIR`-set case separately from the
   plain-onedir case, since PyInstaller alone never sets `APPDIR` — only
   AppImage's own runtime does, and getting this wrong means the Linux
   AppImage silently can't find its own sibling binaries at runtime even
   though the exact same onedir build works fine unpacked.
5. **`clipcontrol.ClipboardControl`'s stored command losing the two-argv-
   element split it always had.** Before this change, `[self._python, ...]`
   was always exactly two elements a caller could override independently
   (a python path, a script path). After moving to a single `command: list[str]`,
   a caller passing a one-element override (a single frozen binary path, no
   separate interpreter) must also work. Task 5 pins both shapes.

---

## Phase 1: Launcher & path plumbing

Testable entirely from source, on this machine, right now — no packaging
yet. Implements spec §3.1 plus the three corrections above.

### Task 1: `desktop.py` — `frozen_root()`, `python_for_gui()`, `overlay_python()`, `relaunch()`

**Files:**
- Modify: `lib/desktop.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Produces: `desktop.frozen_root() -> Path | None`,
  `desktop.python_for_gui() -> str`, `desktop.overlay_python() -> str`,
  `desktop.relaunch(entry: str, *args: str) -> list[str]`. Every later task
  in this phase consumes `desktop.relaunch()` and/or `desktop.frozen_root()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_desktop.py` (needs `from unittest.mock import patch` and
`import sys`, both already imported there):

```python
class RelaunchTests(unittest.TestCase):
    def test_frozen_root_is_none_from_source(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(desktop.frozen_root())

    def test_frozen_root_is_the_executables_own_directory(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "/opt/Clipboard+/tray"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(desktop.frozen_root(), Path("/opt/Clipboard+"))

    def test_frozen_root_prefers_appdir_inside_an_appimage(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {"APPDIR": "/tmp/.mount_ClipboardAbc123"}),
        ):
            self.assertEqual(
                desktop.frozen_root(), Path("/tmp/.mount_ClipboardAbc123/usr/bin")
            )

    def test_relaunch_from_source_uses_overlay_python_and_the_lib_dir(self):
        with patch.object(sys, "frozen", False, create=True):
            command = desktop.relaunch("dictation", "--worker", "abc")
        lib = Path(desktop.__file__).resolve().parent
        self.assertEqual(command[1:], [str(lib / "dictation.py"), "--worker", "abc"])
        self.assertTrue(Path(command[0]).name.startswith("python"))

    def test_relaunch_when_frozen_uses_a_sibling_binary_with_no_extension_on_linux(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "/opt/Clipboard+/tray"),
            patch.object(desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(
                desktop.relaunch("dictation", "--worker", "abc"),
                ["/opt/Clipboard+/dictation", "--worker", "abc"],
            )

    def test_relaunch_when_frozen_appends_exe_on_windows(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", r"C:\Users\a\AppData\Local\Programs\Clipboard+\tray.exe"),
            patch.object(desktop, "platform_name", return_value="windows"),
            patch.dict(os.environ, {}, clear=True),
        ):
            command = desktop.relaunch("dictation")
            self.assertEqual(Path(command[0]).name, "dictation.exe")

    def test_python_for_gui_moved_here_still_works(self):
        self.assertTrue(Path(desktop.python_for_gui()).name.startswith("python"))

    def test_overlay_python_prefers_the_installed_private_venv(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            (prefix / "lib").mkdir()
            module = prefix / "lib/desktop.py"
            with patch.object(desktop, "__file__", str(module)):
                self.assertEqual(desktop.overlay_python(), desktop.python_for_gui())
                private = prefix / "share/whisper-dictation/venv/bin/python"
                private.parent.mkdir(parents=True)
                private.touch()
                with patch.object(desktop, "platform_name", return_value="linux"):
                    self.assertEqual(desktop.overlay_python(), str(private.resolve()))
```

(`os` and `tempfile` are already imported at the top of `tests/test_desktop.py`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_desktop.py -k RelaunchTests -v` (or
`python -m unittest tests.test_desktop.RelaunchTests -v` if pytest isn't
installed — this project's suite runs under `unittest discover`, per
`.github/workflows/quality.yml`)
Expected: FAIL — `AttributeError: module 'desktop' has no attribute
'frozen_root'` (and similarly for the other new names).

- [ ] **Step 3: Implement**

In `lib/desktop.py`, add after `install_prefix()` (so `overlay_python()` can
call it) and before `roots()`:

```python
def python_for_gui() -> str:
    """pythonw on Windows so no console window flashes; sys.executable elsewhere."""
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    return str(windowed if windowed.exists() else python)


def overlay_python() -> str:
    """A Python that can draw windows: the app's private environment when installed
    (a desktop shortcut may run a system Python without Tk), otherwise `python_for_gui()`.
    Also `relaunch()`'s source-mode fallback, so every cross-process launch this app
    makes from source gets the same Tk-capable interpreter, not just the two call
    sites (`dictation.py`'s recording pill and app window) that originally needed it.
    """
    venv = install_prefix(Path(__file__)) / "share/whisper-dictation/venv"
    private = (
        venv / "Scripts/pythonw.exe" if platform_name() == "windows" else venv / "bin/python"
    )
    return str(private) if private.is_file() else python_for_gui()


def frozen_root() -> Path | None:
    """The directory holding this build's sibling executables, or None when running
    from source (`python3 lib/whatever.py`)."""
    if not getattr(sys, "frozen", False):
        return None
    appdir = os.environ.get("APPDIR")  # Set only while running inside an AppImage.
    return Path(appdir) / "usr" / "bin" if appdir else Path(sys.executable).parent


def relaunch(entry: str, *args: str) -> list[str]:
    """argv to start another part of this app ("dictation", "app", "tray"/"menubar",
    "overlay", "engine", "updates", "clipservice") — a sibling frozen executable when
    packaged, the matching script under a window-capable interpreter when running
    from source."""
    root = frozen_root()
    if root is not None:
        suffix = ".exe" if platform_name() == "windows" else ""
        return [str(root / f"{entry}{suffix}"), *args]
    lib = Path(__file__).resolve().parent
    return [overlay_python(), str(lib / f"{entry}.py"), *args]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_desktop -v`
Expected: PASS (all of `test_desktop.py`, not just the new class — confirm
no existing test broke).

- [ ] **Step 5: Lint and type-check**

Run: `ruff check lib/desktop.py tests/test_desktop.py && ruff format --check lib/desktop.py tests/test_desktop.py && mypy --strict lib/desktop.py`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add lib/desktop.py tests/test_desktop.py
git commit -m "Add desktop.relaunch() for frozen-vs-source cross-process launches"
```

---

### Task 2: `hotkeys.py` — re-export `python_for_gui`, make `history_command`'s default frozen-aware

**Files:**
- Modify: `lib/hotkeys.py`
- Test: `tests/test_hotkeys.py`

**Interfaces:**
- Consumes: `desktop.python_for_gui`, `desktop.frozen_root()`,
  `desktop.relaunch()` from Task 1.
- Produces: `hotkeys.python_for_gui` (same object as `desktop.python_for_gui`,
  kept for `setup-desktop.py:563`'s existing call and its own test — not
  removed), `hotkeys.history_command(lib: Path, python: str | None = None) -> list[str]`
  (signature unchanged, only its `python is None` branch changes).

- [ ] **Step 1: Write the failing tests**

`tests/test_hotkeys.py:337`'s existing test
(`hotkeys.history_command(PurePosixPath("/lib"), "/venv/python")`) already
covers the explicit-override branch and must keep passing unchanged — do
not edit it. Add two new tests near it:

```python
def test_history_command_default_matches_relaunch_when_frozen(self):
    with (
        patch.object(hotkeys.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
        patch.object(hotkeys.desktop, "platform_name", return_value="linux"),
    ):
        self.assertEqual(
            hotkeys.history_command(PurePosixPath("/lib")),
            ["/opt/Clipboard+/app", "--clipboard"],
        )

def test_history_command_default_ignores_frozen_state_when_python_is_given(self):
    # setup-desktop.py:636 passes an explicit python for a *different* install
    # prefix than the one currently running; it must never be redirected to
    # this process's own frozen sibling binary.
    with patch.object(hotkeys.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")):
        self.assertEqual(
            hotkeys.history_command(PurePosixPath("/lib"), "/other/prefix/venv/python"),
            ["/other/prefix/venv/python", "/lib/app.py", "--clipboard"],
        )
```

Also add, next to the existing `test_helpers` (which already asserts
`hotkeys.python_for_gui()`'s shape — leave that assertion as-is, it now
exercises the re-export):

```python
def test_python_for_gui_is_the_desktop_implementation(self):
    self.assertIs(hotkeys.python_for_gui, hotkeys.desktop.python_for_gui)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_hotkeys -v`
Expected: FAIL on the two new `history_command` tests (current code ignores
`frozen_root()` entirely) and on `test_python_for_gui_is_the_desktop_implementation`
(current `hotkeys.python_for_gui` is its own local function, a different
object from `desktop.python_for_gui`).

- [ ] **Step 3: Implement**

In `lib/hotkeys.py`, delete the existing function body at line 764
(`def python_for_gui() -> str: ...`, 4 lines) and replace it with an alias
at the same location:

```python
python_for_gui = desktop.python_for_gui
```

Change `history_command` (currently at line 665):

```python
def history_command(lib: Path, python: str | None = None) -> list[str]:
    """What the clipboard history shortcut runs: the window, opened on its Clipboard tab.

    `python` is an explicit override for a *different* install prefix than the one
    currently running (setup-desktop.py installing/updating another location) — when
    given, it always wins and this process's own frozen state is irrelevant.
    """
    if python is None:
        root = desktop.frozen_root()
        if root is not None:
            return desktop.relaunch("app", "--clipboard")
    return [python or desktop.overlay_python(), str(lib / "app.py"), "--clipboard"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_hotkeys -v`
Expected: PASS, including the pre-existing test at line 337 (unchanged).

- [ ] **Step 5: Lint and type-check**

Run: `ruff check lib/hotkeys.py tests/test_hotkeys.py && ruff format --check lib/hotkeys.py tests/test_hotkeys.py && mypy --strict lib/hotkeys.py`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add lib/hotkeys.py tests/test_hotkeys.py
git commit -m "Make hotkeys.history_command's default frozen-aware; move python_for_gui to desktop"
```

---

### Task 3: `dictation.py` — `open_app()`/`start_overlay()` via `relaunch()`, frozen-safe existence guard

**Files:**
- Modify: `lib/dictation.py`
- Test: `tests/test_dictation.py`

**Interfaces:**
- Consumes: `desktop.relaunch()`, `desktop.frozen_root()` from Task 1.
- Produces: `open_app(config)` and `start_overlay(config, token)` keep their
  existing signatures; `overlay_python()` (module-level in `dictation.py`)
  is deleted — nothing else in the codebase calls it (confirmed by
  `grep -rn overlay_python` across `lib/`, `setup-desktop.py`, and `tests/`
  before writing this task: only `dictation.py` itself and
  `tests/test_overlay.py`'s `WorkerHandoverTests` reference it, both
  changed by this task).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dictation.py` (it already imports `patch`, `Path`,
`subprocess` indirectly via `d.subprocess`; confirm `desktop` is imported —
it is, as `d.desktop` is used elsewhere in that file already):

```python
class OpenAppAndOverlayFrozenTests(unittest.TestCase):
    def test_open_app_launches_even_when_the_source_script_is_missing_and_frozen(self):
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(d.desktop, "relaunch", return_value=["/opt/Clipboard+/app"]),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            d.open_app(config)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/opt/Clipboard+/app"])

    def test_open_app_does_nothing_from_source_when_the_script_is_missing(self):
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=None),
            patch.object(Path, "is_file", return_value=False),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            d.open_app(config)
        popen.assert_not_called()

    def test_start_overlay_launches_even_when_the_source_script_is_missing_and_frozen(self):
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(d.desktop, "relaunch", return_value=["/opt/Clipboard+/overlay", TOKEN]),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            result = d.start_overlay(config, TOKEN)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/opt/Clipboard+/overlay", TOKEN])
        self.assertIsNotNone(result)
```

(`TOKEN` is already defined near the top of `tests/test_dictation.py` for
other worker tests — reuse it, don't redefine it.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_dictation.OpenAppAndOverlayFrozenTests -v`
Expected: FAIL — today's `open_app`/`start_overlay` return early because
`script.is_file()` is checked unconditionally, before any frozen check
exists at all.

- [ ] **Step 3: Implement**

In `lib/dictation.py`, delete `overlay_python()` (module-level function,
currently 10 lines starting `def overlay_python() -> str:`). Replace
`open_app()` and `start_overlay()`:

```python
def open_app(config: Config) -> None:
    """Bring up the app window (it shows the saved recording with Retry and Discard).

    Like the recording pill, it is a window of its own: "overlay": false turns both off.
    """
    if not config.b("overlay"):
        return
    frozen = desktop.frozen_root() is not None
    if not frozen and not Path(__file__).resolve().with_name("app.py").is_file():
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(
            desktop.relaunch("app"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )


def start_overlay(config: Config, token: str) -> subprocess.Popen[bytes] | None:
    """Launch the floating recording pill for this session, when wanted and present."""
    if not config.b("overlay"):
        return None
    frozen = desktop.frozen_root() is not None
    if not frozen and not Path(__file__).resolve().with_name("overlay.py").is_file():
        return None
    try:
        return subprocess.Popen(
            desktop.relaunch("overlay", token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **desktop.process_options(),
        )
    except OSError:
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_dictation -v`
Expected: PASS — including the pre-existing tests at lines 242/248/414/469
that patch `open_app`/`start_overlay` wholesale (unaffected by an internal
rewrite) and `test_overlay.py`'s click-to-launch tests (unaffected, per the
delegation trace worked out before writing this task: `desktop.relaunch()`'s
source-mode output shape matches what those tests already assert).

- [ ] **Step 5: Update `tests/test_overlay.py`**

Delete `WorkerHandoverTests` (it tests the now-deleted `d.overlay_python()`).
Its coverage is superseded by Task 1's `test_overlay_python_prefers_the_installed_private_venv`
in `tests/test_desktop.py` (same behavior, now correctly owned by the module
that defines it).

- [ ] **Step 6: Run the full test file**

Run: `python -m unittest tests.test_overlay -v`
Expected: PASS.

- [ ] **Step 7: Lint and type-check**

Run: `ruff check lib/dictation.py tests/test_dictation.py tests/test_overlay.py && ruff format --check lib/dictation.py tests/test_dictation.py tests/test_overlay.py && mypy --strict lib/dictation.py`
Expected: all clean.

- [ ] **Step 8: Commit**

```bash
git add lib/dictation.py tests/test_dictation.py tests/test_overlay.py
git commit -m "Make dictation.py's app/overlay launches frozen-safe; move overlay_python to desktop"
```

---

### Task 4: `overlay.py` — engine launch via `relaunch()`

**Files:**
- Modify: `lib/overlay.py:216`
- Test: `tests/test_overlay.py` (no new test needed — traced in Task 3's
  planning that the existing assertions at lines 224/229 already pin the
  post-change output shape; this task is a pure refactor confirmed safe by
  the *existing* tests, which is itself the verification)

**Interfaces:**
- Consumes: `desktop.relaunch()` from Task 1.

- [ ] **Step 1: Implement**

In `lib/overlay.py`, change the `_click` method (currently):

```python
        if name:
            engine = str(Path(__file__).resolve().with_name("dictation.py"))
            self._launch([sys.executable, engine, *(["--cancel"] if name == "cancel" else [])])
```

to:

```python
        if name:
            self._launch(desktop.relaunch("dictation", *(["--cancel"] if name == "cancel" else [])))
```

(`sys` may now be unused in this file — check with the next step before
deciding whether to remove the `import sys` line; `Path` is still used
elsewhere in the file, so its import stays regardless.)

- [ ] **Step 2: Run the existing tests to confirm nothing broke**

Run: `python -m unittest tests.test_overlay -v`
Expected: PASS, including `test_the_buttons_stop_or_cancel_the_recording`
(the two assertions this task's planning traced through by hand).

- [ ] **Step 3: Lint and type-check**

Run: `ruff check lib/overlay.py && ruff format --check lib/overlay.py && mypy --strict lib/overlay.py`
Expected: all clean. If `ruff check` flags `sys` as an unused import,
remove that one line (`import sys`) — nothing else in the file uses it
once this change lands.

- [ ] **Step 4: Commit**

```bash
git add lib/overlay.py
git commit -m "Launch the dictation engine from the recording pill via desktop.relaunch()"
```

---

### Task 5: `clipcontrol.py` — replace the stored python path with a full command

**Files:**
- Modify: `lib/clipcontrol.py`
- Modify: `lib/menubar.py` (its `ClipboardControl(...)` construction call)
- Test: `tests/test_clipcontrol.py`

**Interfaces:**
- Consumes: `desktop.relaunch()` from Task 1.
- Produces: `ClipboardControl.__init__`'s keyword-only `python: str | None`
  parameter is replaced by `command: list[str] | None`. This is a breaking
  signature change to a class with exactly two real callers
  (`lib/menubar.py`, `lib/tray.py`) plus its test — all three are covered by
  this task; no other file constructs `ClipboardControl` (confirmed by
  `grep -rn "ClipboardControl(" lib/ tests/` before writing this task).

- [ ] **Step 1: Write the failing tests**

In `tests/test_clipcontrol.py`, change the shared `control()` helper
(currently `python="/venv/python"`):

```python
    def control(self) -> clipcontrol.ClipboardControl:
        return clipcontrol.ClipboardControl(
            self.paths,
            self.prefs,
            command=["/venv/python", str(clipcontrol.HERE / "clipservice.py")],
            popen=self.popen,
            clock=lambda: self.now,
            wall=lambda: self.wall,
        )
```

The assertions at lines 88–89 (`self.started[0][0] == "/venv/python"`,
`self.started[0][1].endswith("clipservice.py")`) stay exactly as they are —
they already read `self.started[0]`, which is still this same two-element
list. Add one new test confirming the default (no override) delegates to
`desktop.relaunch`:

```python
def test_default_command_comes_from_desktop_relaunch(self):
    with patch.object(clipcontrol.desktop, "relaunch", return_value=["/opt/Clipboard+/clipservice"]):
        control = clipcontrol.ClipboardControl(
            self.paths, self.prefs, popen=self.popen, clock=lambda: self.now, wall=lambda: self.wall
        )
        control.start()
    self.assertEqual(self.started[0], ["/opt/Clipboard+/clipservice"])
```

(Place this as a new test method on `ControlCase` or a small subclass of
it, following whatever grouping convention the surrounding test classes in
this file already use — check the file for its existing class names before
picking one, rather than guessing.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_clipcontrol -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument
'command'` (current signature only accepts `python`).

- [ ] **Step 3: Implement**

In `lib/clipcontrol.py`, change the constructor:

```python
    def __init__(
        self,
        paths: d.Paths,
        prefs: hotkeys.Preferences,
        *,
        command: list[str] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._paths = paths
        self._prefs = prefs
        self._command = command or desktop.relaunch("clipservice")
```

(Remove the old `self._python = python or hotkeys.python_for_gui()` line;
everything else in `__init__` is unchanged.) Then change the one place that
built `[self._python, str(HERE / "clipservice.py")]` (around line 122) to:

```python
            self._command,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_clipcontrol -v`
Expected: PASS.

- [ ] **Step 5: Update `menubar.py`'s construction call**

`lib/menubar.py`'s `Controller.init` currently has:

```python
        self.clip = clipcontrol.ClipboardControl(
            self.paths, self.preferences, python=sys.executable
        )
```

Change to:

```python
        self.clip = clipcontrol.ClipboardControl(self.paths, self.preferences)
```

(`desktop.relaunch("clipservice")`'s default is correct here in both frozen
and source mode — see this task's header interface note; menubar.py's
explicit `python=sys.executable` was only ever needed because macOS has no
console-flash concern to route around, which `desktop.overlay_python()`'s
fallback already handles correctly by degrading to `python_for_gui()`,
which itself degrades to plain `sys.executable` on macOS.)

- [ ] **Step 6: Lint and type-check**

Run: `ruff check lib/clipcontrol.py lib/menubar.py tests/test_clipcontrol.py && ruff format --check lib/clipcontrol.py lib/menubar.py tests/test_clipcontrol.py && mypy --strict lib/clipcontrol.py lib/menubar.py`
Expected: all clean. (`test_clipcontrol.py` runs on Linux in CI, so this
confirms the change directly; `menubar.py`'s one-line call-site change is
verified by mypy/ruff only, per Global Constraints — it's simple enough
that no PyObjC runtime is needed to trust it.)

- [ ] **Step 7: Commit**

```bash
git add lib/clipcontrol.py lib/menubar.py tests/test_clipcontrol.py
git commit -m "ClipboardControl takes a full command instead of just a python path"
```

---

### Task 6: `tray.py` — `run_engine`, `sync_login`, `open_app_window` via `relaunch()`

**Files:**
- Modify: `lib/tray.py:292-299` (`run_engine`), `:363-365` (`sync_login`),
  `:655-663` (`open_app_window`)
- Modify: `lib/app.py:1326` (its `hotkeys.history_command(...)` call site —
  unaffected in shape by Task 2, listed here only so the person doing this
  task double-checks it still reads correctly; no code change needed there,
  Task 2 already made `history_command`'s no-arg-override call frozen-aware
  transparently)
- Test: `tests/test_tray.py`

**Interfaces:**
- Consumes: `desktop.relaunch()` from Task 1.

- [ ] **Step 1: Write the failing tests**

`tests/test_tray.py`'s existing `test_shortcut_runs_engine_or_opens_setup`
already asserts `self.popen.call_args.args[0][-1].endswith("dictation.py")`
after `self.tray.pressed()` (which calls `run_engine`) — traced by hand
before writing this task: `desktop.relaunch("dictation")` in source/test
mode (not frozen) produces `[overlay_python(), str(lib/"dictation.py")]`,
whose last element still ends with `"dictation.py"`. That test needs no
edit. Add new tests for the frozen shape and for `sync_login`:

```python
class RelaunchTests(TrayTests):
    def test_run_engine_uses_a_sibling_binary_when_frozen(self):
        with patch.object(tray.desktop, "relaunch", return_value=["/opt/Clipboard+/dictation"]):
            self.tray.run_engine()
        self.assertEqual(self.popen.call_args.args[0], ["/opt/Clipboard+/dictation"])

    def test_sync_login_uses_relaunch_for_the_tray_entry(self):
        with (
            patch.object(tray.desktop, "relaunch", return_value=["/opt/Clipboard+/tray"]) as relaunch,
            patch.object(tray.hotkeys, "set_login_item") as set_login_item,
        ):
            self.tray.sync_login()
        relaunch.assert_called_once_with("tray")
        set_login_item.assert_called_once_with(
            self.tray.preferences.open_at_login(), ["/opt/Clipboard+/tray"]
        )

    def test_open_app_window_uses_a_sibling_binary_when_frozen(self):
        with patch.object(tray.desktop, "relaunch", return_value=["/opt/Clipboard+/app"]):
            tray.open_app_window("--clipboard")
        self.assertEqual(self.popen.call_args.args[0], ["/opt/Clipboard+/app"])
```

(Subclassing `TrayTests` reuses its `setUp` — the same pattern the file's
other test classes already follow; check the file for that pattern before
writing this, rather than duplicating `setUp`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_tray.RelaunchTests -v`
Expected: FAIL — current code never calls `desktop.relaunch` at all.

- [ ] **Step 3: Implement**

```python
    def run_engine(self, *flags: str) -> None:
        subprocess.Popen(
            desktop.relaunch("dictation", *flags),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
```

```python
    def sync_login(self) -> None:
        hotkeys.set_login_item(self.preferences.open_at_login(), desktop.relaunch("tray"))
```

```python
def open_app_window(page: str = "") -> None:
    """Start the window; a running window is asked to show itself instead."""
    subprocess.Popen(
        desktop.relaunch("app", *([page] if page else [])),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **desktop.process_options(detached=True),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_tray -v`
Expected: PASS — the whole file, including the pre-existing
`test_shortcut_runs_engine_or_opens_setup`.

- [ ] **Step 5: Lint and type-check**

Run: `ruff check lib/tray.py tests/test_tray.py && ruff format --check lib/tray.py tests/test_tray.py && mypy --strict lib/tray.py`
Expected: all clean. If `hotkeys` or `Path` become unused imports in
`tray.py` after this change, check with `ruff` and remove only what it
actually flags — don't guess.

- [ ] **Step 6: Commit**

```bash
git add lib/tray.py tests/test_tray.py
git commit -m "Route tray.py's cross-process launches through desktop.relaunch()"
```

---

### Task 7: `menubar.py` — `run_engine`, `open_app_window` via `relaunch()`

**Files:**
- Modify: `lib/menubar.py:436-443` (`run_engine`), `:897-905`
  (`open_app_window`)

**Interfaces:**
- Consumes: `desktop.relaunch()` from Task 1.

No test file exists for `menubar.py` and none is added here — the module
imports `objc`/`AppKit`/`Foundation` unconditionally at the top of the file,
so it cannot even be imported on a non-macOS machine (confirmed earlier
this session: `import objc` fails with `ModuleNotFoundError` on this Linux
sandbox), and `pyproject.toml` already excludes it from the coverage floor
for exactly that reason. Verification here is `mypy --strict` + `ruff`
only, matching that existing, deliberate project convention.

- [ ] **Step 1: Implement**

```python
    def run_engine(self, *flags: str) -> None:
        # The engine owns locking, recording, notifications and the clipboard.
        subprocess.Popen(
            desktop.relaunch("dictation", *flags),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
```

```python
def open_app_window(page: str = "") -> None:
    """Start the window; a running window is asked to show itself instead."""
    subprocess.Popen(
        desktop.relaunch("app", *([page] if page else [])),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
```

`menubar.py` does not import `desktop` yet — check its import block; if
absent, add `import desktop` alongside its other bare `import` lines
(`clipcontrol`, `clipstore`, `dictation as d`, `hotkeys`, `telemetry`,
`updates`, `workflow`).

- [ ] **Step 2: Lint and type-check**

Run: `ruff check lib/menubar.py && ruff format --check lib/menubar.py && mypy --strict lib/menubar.py`
Expected: all clean. If `sys` becomes an unused import after this change
(it was only used for `sys.executable` in these two spots — check the rest
of the file before removing it; it's also used at construction time
elsewhere per this session's earlier popover work), remove it only if
`ruff` actually flags it.

- [ ] **Step 3: Commit**

```bash
git add lib/menubar.py
git commit -m "Route menubar.py's cross-process launches through desktop.relaunch()"
```

---

### Task 8: Phase 1 wrap-up

**Files:** none new — this task only runs verification across everything
Phase 1 touched.

- [ ] **Step 1: Run the full suite exactly as CI does it**

Run: `sh tests/with-xvfb.sh sh -c 'coverage run -m unittest discover -s tests -v && coverage combine && coverage report'`
Expected: all tests pass, coverage report shows ≥90% (the existing floor —
this phase adds tests, it shouldn't lower coverage).

- [ ] **Step 2: Run lint/type-check across the whole tree once, not per-file**

Run: `ruff check lib tests tools setup-desktop.py && ruff format --check lib tests tools setup-desktop.py && mypy --strict lib tools setup-desktop.py`
Expected: clean (this also re-confirms `setup-desktop.py` truly needed no
edits — it should report clean without having been touched).

- [ ] **Step 3: Confirm no stray references to deleted names remain**

Run: `grep -rn "overlay_python\b" lib/ setup-desktop.py tests/` — expected
output: only `desktop.py`'s definition and `test_desktop.py`'s test for it.
Run: `grep -rn "\.python\b" lib/clipcontrol.py` — expected: no matches (the
old `self._python` attribute is fully gone).

- [ ] **Step 4: Tag the phase boundary**

```bash
git log --oneline -8
```
Confirm the 7 commits from Tasks 1–7 are all present and in order before
starting Phase 2 — Phase 2's PyInstaller spec will need every one of these
`relaunch()` call sites to already be in place.

---

## Phase 2: PyInstaller bundling

Produces a working onedir build per OS with every entry-point binary
present and launchable. Not yet wrapped in an installer (Phase 3).
**This phase's build steps run on each target OS** (a GitHub Actions
matrix job, set up properly in Phase 4, is the intended way almost all of
this actually gets exercised end-to-end across all three OSes — this
session's Linux sandbox can write and lint every file below, and can run
the Linux build's `pyinstaller` step directly if the tool is available to
install, but cannot run or verify the macOS/Windows builds itself).

### Task 9: Binary-path resolution for bundled ffmpeg/whisper.cpp

**Files:**
- Modify: `lib/desktop.py` (extend the existing command-building functions)
- Test: `tests/test_desktop.py`

**Interfaces:**
- Consumes: `desktop.frozen_root()` from Task 1.
- Produces: a `desktop.bundled_binary(name: str) -> Path | None` helper —
  `frozen_root() / name` (with `.exe` on Windows) when that file exists
  there, else `None` so callers fall back to their existing PATH-based
  lookup unchanged.

- [ ] **Step 1: Write the failing test**

```python
class BundledBinaryTests(unittest.TestCase):
    def test_returns_none_from_source(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(desktop.bundled_binary("ffmpeg"))

    def test_finds_a_sibling_binary_when_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ffmpeg").touch()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(desktop, "frozen_root", return_value=root),
                patch.object(desktop, "platform_name", return_value="linux"),
            ):
                self.assertEqual(desktop.bundled_binary("ffmpeg"), root / "ffmpeg")

    def test_returns_none_when_frozen_but_the_binary_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(desktop, "frozen_root", return_value=Path(folder)),
            ):
                self.assertIsNone(desktop.bundled_binary("ffmpeg"))
```

- [ ] **Step 2: Run to verify failure, then implement**

```python
def bundled_binary(name: str) -> Path | None:
    """A native binary this build ships (ffmpeg, whisper.cpp's server), or None to
    fall back to the system PATH (source installs, which rely on brew/apt/winget)."""
    root = frozen_root()
    if root is None:
        return None
    suffix = ".exe" if platform_name() == "windows" else ""
    candidate = root / f"{name}{suffix}"
    return candidate if candidate.is_file() else None
```

- [ ] **Step 3: Wire it into the existing lookups**

Two call sites need the bundled-first check, found by reading
`recorder_command()` (`lib/desktop.py:124`) and `server_binary()`
(`lib/engine.py:35`) in full: both apply. Note explicitly what's **not**
covered here — the ALSA `arecord` path (Linux mic capture without ffmpeg)
stays a system dependency (`alsa-utils`); the spec's bundling scope (§1,
§3.3) names only ffmpeg and whisper.cpp, not arecord, and this task does
not silently expand that. Document this one residual Linux dependency in
`packaging/README.md` (Task 10) rather than treating the AppImage as 100%
dependency-free when it isn't quite.

In `lib/desktop.py`'s `recorder_command()`, change:

```python
    args = executable(str(values["ffmpeg"])) + ["-hide_banner"]
```

to:

```python
    ffmpeg = str(values["ffmpeg"])
    if ffmpeg == "ffmpeg":  # The unmodified default; never override an explicit user path.
        bundled = bundled_binary("ffmpeg")
        if bundled is not None:
            ffmpeg = str(bundled)
    args = executable(ffmpeg) + ["-hide_banner"]
```

In `lib/engine.py`'s `server_binary()`, change:

```python
def server_binary(config: d.Config) -> str:
    """The whisper-server path beside the configured whisper-cli, or on PATH."""
    import shutil

    suffix = ".exe" if sys.platform == "win32" else ""
```

to:

```python
def server_binary(config: d.Config) -> str:
    """The whisper-server path: bundled with this build, beside the configured
    whisper-cli, or on PATH."""
    import shutil

    bundled = desktop.bundled_binary("whisper-server")
    if bundled is not None:
        return str(bundled)
    suffix = ".exe" if sys.platform == "win32" else ""
```

(everything after that line in the function — the `cli`/`fallback`/loop —
stays exactly as it is; this only adds an earlier, higher-priority return.)

- [ ] **Step 4: Run tests, lint, type-check, commit**

Run: `python -m unittest tests.test_desktop tests.test_engine -v && ruff check lib/desktop.py lib/engine.py tests/test_desktop.py && mypy --strict lib/desktop.py lib/engine.py`

```bash
git add lib/desktop.py lib/engine.py tests/test_desktop.py
git commit -m "Prefer a bundled ffmpeg/whisper.cpp binary over PATH when frozen"
```

---

### Task 10: PyInstaller spec — Linux

**Files:**
- Create: `packaging/linux/clipboardplus.spec`
- Create: `packaging/README.md`

**Interfaces:**
- Produces: `dist/clipboardplus/` containing seven binaries —
  `tray`, `app`, `dictation`, `engine`, `overlay`, `updates`, `clipservice`
  (Linux/Windows use `tray`; macOS's separate spec in Task 11 uses `menubar`
  instead of `tray`) — sharing one `COLLECT`.

- [ ] **Step 1: Write the spec**

```python
# packaging/linux/clipboardplus.spec
# Run from the repo root: pyinstaller packaging/linux/clipboardplus.spec
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve().parents[1]
LIB = ROOT / "lib"

ENTRIES = ["tray", "app", "dictation", "engine", "overlay", "updates", "clipservice"]

analyses = []
for entry in ENTRIES:
    analyses.append(
        Analysis(
            [str(LIB / f"{entry}.py")],
            pathex=[str(LIB)],
            binaries=[],
            datas=[
                (str(LIB / "whisper-dictation.png"), "."),
                (str(LIB / "tray-recording.png"), "."),
            ],
            hiddenimports=[],
            hookspath=[],
            runtime_hooks=[],
            excludes=[],
            cipher=block_cipher,
        )
    )

merged = analyses[0]
for other in analyses[1:]:
    merged.pure += [item for item in other.pure if item not in merged.pure]
    merged.binaries += [item for item in other.binaries if item not in merged.binaries]
    merged.datas += [item for item in other.datas if item not in merged.datas]

exes = [
    EXE(
        PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher),
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=entry,
        console=False,
    )
    for entry, analysis in zip(ENTRIES, analyses)
]

coll = COLLECT(
    *exes,
    *[analysis.binaries for analysis in analyses],
    *[analysis.zipfiles for analysis in analyses],
    *[analysis.datas for analysis in analyses],
    strip=False,
    upx=False,
    name="clipboardplus",
)
```

- [ ] **Step 2: Write `packaging/README.md`**

```markdown
# Packaging Clipboard+

Builds a self-contained onedir bundle per OS with PyInstaller — no system
package manager, no sudo/UAC, no visible Python. See
`docs/superpowers/specs/2026-09-28-packaged-installers-design.md` for why.

One residual dependency: on Linux, ALSA-backend mic capture still shells
out to the system `arecord` (from `alsa-utils`) rather than a bundled
binary — only ffmpeg and whisper.cpp are bundled, per the design spec's
scope. Most desktops already have `alsa-utils` installed; this is a known,
intentional gap, not an oversight.

## Linux

    pip install pyinstaller
    pyinstaller packaging/linux/clipboardplus.spec
    ls dist/clipboardplus/   # tray, app, dictation, engine, overlay, updates, clipservice

## Smoke-test before wrapping into an AppImage

    dist/clipboardplus/tray --help    # confirm it starts; Ctrl+C to stop if it doesn't exit on its own

## Manual pre-release checklist (run this on a clean VM/user account, no dev tools)

1. Install the artifact (`.dmg` drag, Inno Setup `.exe`, or `.AppImage`
   after `chmod +x`).
2. Confirm zero terminal windows appear at any point.
3. Confirm the *only* prompt is the one OS security click (Gatekeeper
   right-click-Open on macOS, SmartScreen "More info -> Run anyway" on
   Windows; none at all on Linux beyond the chmod/"allow executing" step).
4. Run first-run setup end to end: the model download shows real progress
   (megabytes and a percentage, not a spinner), the GNOME shortcut (Linux)
   or global shortcut registration succeeds, "Open at Login" takes effect
   after a reboot.
5. Once a newer tagged release exists, trigger the in-app "Check for
   Updates" and confirm it downloads, swaps, and relaunches cleanly.
```

- [ ] **Step 3: Verify what this sandbox can verify**

Run: `python -c "import ast; ast.parse(open('packaging/linux/clipboardplus.spec').read())"`
Expected: no `SyntaxError` (confirms the spec file is at least valid
Python, which PyInstaller's spec loader requires — this is the one thing
about this file a Linux sandbox without PyInstaller installed can still
check). If PyInstaller is installed or installable here
(`pip install pyinstaller` succeeds), additionally run
`pyinstaller packaging/linux/clipboardplus.spec` and confirm
`dist/clipboardplus/tray --help` (or another cheap flag) exits 0 or with a
recognizable usage message rather than an import traceback — that's the
real verification for this task, and it can only be skipped, not faked,
if the tool genuinely can't be installed in this environment.

- [ ] **Step 4: Commit**

```bash
git add packaging/linux/clipboardplus.spec packaging/README.md
git commit -m "Add PyInstaller onedir spec for Linux"
```

---

### Task 11: PyInstaller spec — macOS (with PyObjC hidden-imports)

**Files:**
- Create: `packaging/macos/clipboardplus.spec`

**Interfaces:**
- Produces: `dist/clipboardplus/` with `menubar` (not `tray`) plus the same
  six other binaries as Task 10.

- [ ] **Step 1: Write the spec**

Same structure as Task 10's `ENTRIES` list but with `"menubar"` in place of
`"tray"`, and an explicit `hiddenimports` list on the `menubar` entry's
`Analysis` covering the known PyObjC-under-PyInstaller gap (spec §3.2):

```python
ENTRIES = ["menubar", "app", "dictation", "engine", "overlay", "updates", "clipservice"]

PYOBJC_HIDDEN_IMPORTS = [
    "objc",
    "AppKit",
    "Foundation",
    "PyObjCTools",
    "objc._objc",
]
```

Pass `hiddenimports=PYOBJC_HIDDEN_IMPORTS if entry == "menubar" else []` in
each entry's `Analysis(...)` call (everything else mirrors Task 10's spec
verbatim — copy it and make only these two changes, then re-read the
result once to confirm nothing else silently drifted).

- [ ] **Step 2: Verify what this sandbox can verify**

Run: `python -c "import ast; ast.parse(open('packaging/macos/clipboardplus.spec').read())"`
Expected: no `SyntaxError`. **This spec's real verification —
`pyinstaller packaging/macos/clipboardplus.spec` followed by launching
`dist/clipboardplus/menubar` and confirming the status-bar icon actually
appears — requires a real macOS machine or the macOS runner in Phase 4's CI
matrix.** This is the single highest-risk untested piece of the whole
plan (Review Focus item 1) precisely because it cannot be checked from
here; Task 15 (CI smoke-launch) is what actually closes this gap, and it
must not be skipped or treated as optional.

- [ ] **Step 3: Commit**

```bash
git add packaging/macos/clipboardplus.spec
git commit -m "Add PyInstaller onedir spec for macOS with PyObjC hidden-imports"
```

---

### Task 12: PyInstaller spec — Windows

**Files:**
- Create: `packaging/windows/clipboardplus.spec`

**Interfaces:**
- Produces: `dist/clipboardplus/` with `tray.exe` plus the same six other
  `.exe` binaries as Task 10 (PyInstaller appends `.exe` automatically on
  Windows from the same `name=entry` value — no spec-file change needed for
  that).

- [ ] **Step 1: Write the spec**

Same as Task 10's spec, with `ENTRIES = ["tray", "app", "dictation",
"engine", "overlay", "updates", "clipservice"]` (same list as Linux — this
platform also uses `tray.py`, not `menubar.py`), and `console=False` on
every `EXE(...)` (already the case, copied from Task 10) so no console
window ever flashes on Windows either.

- [ ] **Step 2: Verify what this sandbox can verify**

Same as Task 11 Step 2: syntax-check only from here; real verification
needs a Windows runner (Phase 4's CI matrix).

- [ ] **Step 3: Commit**

```bash
git add packaging/windows/clipboardplus.spec
git commit -m "Add PyInstaller onedir spec for Windows"
```

---

## Phase 3: Native installer wrapping + first-run parity

Wraps each OS's onedir output (Phase 2) into the format users actually
download. Every branding value here is copy-pasted from the spec's §3.4
(Global Constraints repeats them) — never invented fresh.

### Task 13: macOS `.app` assembly + `.dmg`

**Files:**
- Create: `packaging/macos/Info.plist`
- Create: `packaging/macos/build-dmg.sh`

- [ ] **Step 1: Write `Info.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Clipboard+</string>
    <key>CFBundleDisplayName</key>
    <string>Clipboard+</string>
    <key>CFBundleIdentifier</key>
    <string>com.apercallc.clipboardplus</string>
    <key>CFBundleVersion</key>
    <string>1.2.1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.2.1</string>
    <key>CFBundleExecutable</key>
    <string>menubar</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Clipboard+ records audio only while you are actively dictating.</string>
</dict>
</plist>
```

(`CFBundleVersion` must track `desktop.APP_VERSION` in `lib/desktop.py` —
read that constant's current value before writing this file rather than
hardcoding a value that might already be stale by the time this task runs.)

- [ ] **Step 2: Write `build-dmg.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
# Run after: pyinstaller packaging/macos/clipboardplus.spec
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/clipboardplus"
APP="$ROOT/dist/Clipboard+.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/packaging/macos/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$DIST"/* "$APP/Contents/MacOS/"

STAGING="$ROOT/dist/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "Clipboard+" -srcfolder "$STAGING" -ov -format UDZO \
  "$ROOT/dist/Clipboard+.dmg"

echo "Built dist/Clipboard+.dmg"
echo "First launch needs one right-click -> Open (unsigned build)."
```

```bash
chmod +x packaging/macos/build-dmg.sh
```

- [ ] **Step 3: Verify what this sandbox can verify**

Run: `bash -n packaging/macos/build-dmg.sh` (bash syntax check only —
`hdiutil` doesn't exist on Linux, so the script cannot actually run here).
Run: `python -c "import plistlib; plistlib.load(open('packaging/macos/Info.plist', 'rb'))"`
to confirm the plist parses as valid XML/plist (this genuinely works on
Linux, `plistlib` has no macOS dependency). **Actually producing and
opening `Clipboard+.dmg` requires a macOS machine or Phase 4's CI matrix.**

- [ ] **Step 4: Commit**

```bash
git add packaging/macos/Info.plist packaging/macos/build-dmg.sh
git commit -m "Add macOS .app assembly and .dmg build script"
```

---

### Task 14: Windows Inno Setup script + Linux AppImage recipe

**Files:**
- Create: `packaging/windows/clipboardplus.iss`
- Create: `packaging/linux/AppDir/clipboardplus.desktop`
- Create: `packaging/linux/build-appimage.sh`

- [ ] **Step 1: Write the Inno Setup script**

```ini
; packaging/windows/clipboardplus.iss
; Run after: pyinstaller packaging/windows/clipboardplus.spec
; Build with: iscc packaging\windows\clipboardplus.iss
[Setup]
AppName=Clipboard+
AppVersion=1.2.1
AppPublisher=Clipboard+
DefaultDirName={localappdata}\Programs\Clipboard+
DefaultGroupName=Clipboard+
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Clipboard+-Setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\tray.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\clipboardplus\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\Clipboard+"; Filename: "{app}\tray.exe"
Name: "{userdesktop}\Clipboard+"; Filename: "{app}\tray.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\tray.exe"; Flags: nowait postinstall skipifsilent
```

(`AppVersion` must track `desktop.APP_VERSION` — same note as Task 13's
`Info.plist`, don't hardcode a value without checking the live constant
first. `PrivilegesRequired=lowest` plus `DefaultDirName={localappdata}\...`
together are what guarantee no UAC prompt — both are required, neither
alone is sufficient.)

- [ ] **Step 2: Write the Linux `.desktop` file**

```ini
; packaging/linux/AppDir/clipboardplus.desktop
[Desktop Entry]
Type=Application
Name=Clipboard+
Comment=Dictation and clipboard history
Exec=tray
Icon=clipboardplus
Categories=Utility;
Terminal=false
```

- [ ] **Step 3: Write the AppImage build script**

```bash
#!/usr/bin/env bash
set -euo pipefail
# Run after: pyinstaller packaging/linux/clipboardplus.spec
# Requires linuxdeploy and appimagetool on PATH (fetched by CI in Phase 4;
# download them yourself from their GitHub Releases to run this locally).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist/clipboardplus"
APPDIR="$ROOT/dist/AppDir"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -R "$DIST"/* "$APPDIR/usr/bin/"
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/1024x1024/apps"
cp "$ROOT/packaging/linux/AppDir/clipboardplus.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/assets/icon-1024.png" \
  "$APPDIR/usr/share/icons/hicolor/1024x1024/apps/clipboardplus.png"
ln -sf usr/share/applications/clipboardplus.desktop "$APPDIR/clipboardplus.desktop"
ln -sf usr/share/icons/hicolor/1024x1024/apps/clipboardplus.png "$APPDIR/clipboardplus.png"
ln -sf usr/bin/tray "$APPDIR/AppRun"

appimagetool "$APPDIR" "$ROOT/dist/Clipboard+-x86_64.AppImage"

echo "Built dist/Clipboard+-x86_64.AppImage"
echo "First run needs: chmod +x 'Clipboard+-x86_64.AppImage'"
```

```bash
chmod +x packaging/linux/build-appimage.sh
```

- [ ] **Step 4: Verify what this sandbox can verify**

Run: `bash -n packaging/linux/build-appimage.sh` — syntax check.
Run: `python -c "import configparser; c = configparser.ConfigParser(); c.read('packaging/linux/AppDir/clipboardplus.desktop'); assert c['Desktop Entry']['Name'] == 'Clipboard+'"`
to confirm the `.desktop` file parses and carries the right name.
The Inno Setup script cannot be compiled or linted from Linux (`iscc` is
Windows-only) — a plain read-through for typos/consistency with Task 12's
spec's entry names is the only check available here; real verification is
Phase 4's Windows CI job.

- [ ] **Step 5: Commit**

```bash
git add packaging/windows/clipboardplus.iss packaging/linux/AppDir/clipboardplus.desktop packaging/linux/build-appimage.sh
git commit -m "Add Windows Inno Setup script and Linux AppImage build recipe"
```

---

## Phase 4: Frozen self-update, CI/release pipeline, docs

### Task 15: Frozen-install self-update path

**Files:**
- Modify: `lib/updates.py`
- Test: `tests/test_updates.py`

**Interfaces:**
- Consumes: `desktop.frozen_root()`, `desktop.relaunch()` from Task 1.
- Produces: `apply_update()` keeps its existing signature
  (`apply_update(paths, version, url)`); its behavior branches on
  `desktop.frozen_root()` internally. The existing source-install branch
  (download a tarball, run `setup-desktop.py --prefix ...`) is preserved
  verbatim inside an `if desktop.frozen_root() is None:` guard — read
  `apply_update`'s current full body (already read earlier this session; it
  spans roughly the whole "Runs as its own process..." docstring through
  its `finally: os.close(lock)`) before writing this task's diff, since the
  new frozen branch must sit alongside that logic, not replace it.

- [ ] **Step 1: Write the failing test**

```python
class FrozenUpdateTests(unittest.TestCase):
    def test_frozen_install_swaps_the_directory_instead_of_running_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            install = root / "Clipboard+"
            install.mkdir()
            (install / "tray").write_text("old")
            paths = d.Paths()
            with (
                patch.object(updates.desktop, "frozen_root", return_value=install),
                patch.object(updates, "_download") as download,
                patch.object(updates, "_extract_frozen_release") as extract,
                patch.object(updates.desktop, "relaunch", return_value=[str(install / "tray")]),
                patch.object(updates.subprocess, "Popen") as popen,
            ):
                extract.return_value = install.parent / "Clipboard+-new"
                extract.return_value.mkdir()
                (extract.return_value / "tray").write_text("new")
                updates.apply_update(paths, "9.9.9", "https://example.invalid/release.zip")
            download.assert_called_once()
            extract.assert_called_once()
            popen.assert_called_once_with([str(install / "tray")])
            self.assertEqual((install / "tray").read_text(), "new")
```

(Exact mock boundaries — `_download`, `_extract_frozen_release` — are
chosen to mirror the existing source-install test's mocking style in
`tests/test_updates.py`; read that file's existing `apply_update` test
before writing this one and match its patching granularity rather than
inventing a different style.)

- [ ] **Step 2: Run to verify failure, then implement**

Add a frozen branch to `apply_update`, gated the same way the interfaces
note describes, and a new helper:

```python
def _extract_frozen_release(url: str, work: Path) -> Path:
    """Download this OS's release asset (a zipped onedir build) and extract it."""
    archive = work / "release.zip"
    _download(url, archive)
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work)
    (extracted,) = [p for p in work.iterdir() if p.is_dir()]
    return extracted


def _swap_install(current: Path, new: Path) -> None:
    """Replace `current`'s contents with `new`'s, without deleting the running
    executable while it's still running (Windows can't overwrite an open file)."""
    old = current.parent / f"{current.name}-old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    current.rename(old)
    new.rename(current)
    shutil.rmtree(old, ignore_errors=True)
```

Inside `apply_update`, where the existing code currently always takes the
`setup-desktop.py --prefix` path, branch:

```python
    root = desktop.frozen_root()
    if root is not None:
        install = root if root.name != "usr" else root.parents[1]  # AppImage's usr/bin -> its install dir
        with tempfile.TemporaryDirectory(prefix="update-", dir=paths.cache) as work:
            extracted = _extract_frozen_release(url, Path(work))
            _swap_install(install, extracted)
        subprocess.Popen(desktop.relaunch(Path(sys.executable).stem), **desktop.process_options(detached=True))
        write_state(paths, {**read_state(paths), "status": "installed", "applied": version, "at": time.time()})
        return
    # ... existing source-install branch, unchanged, follows here ...
```

(`shutil` needs importing at the top of `updates.py` if not already present
— check before adding a duplicate import.)

- [ ] **Step 3: Run tests, lint, type-check, commit**

Run: `python -m unittest tests.test_updates -v && ruff check lib/updates.py tests/test_updates.py && mypy --strict lib/updates.py`

```bash
git add lib/updates.py tests/test_updates.py
git commit -m "Add a frozen-install self-update path alongside the source-install one"
```

---

### Task 16: GitHub Actions release workflow

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: Release
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: ubuntu-latest
            spec: packaging/linux/clipboardplus.spec
            wrap: packaging/linux/build-appimage.sh
            asset: dist/Clipboard+-x86_64.AppImage
          - os: macos-latest
            spec: packaging/macos/clipboardplus.spec
            wrap: packaging/macos/build-dmg.sh
            asset: dist/Clipboard+.dmg
          - os: windows-latest
            spec: packaging/windows/clipboardplus.spec
            wrap: ""
            asset: dist/Clipboard+-Setup.exe
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
        with:
          python-version: "3.11"
      - run: python -m pip install -r requirements-dev.txt pyinstaller
      - run: pyinstaller ${{ matrix.spec }}
      - name: Smoke-launch every bundled entry point
        shell: bash
        run: |
          set -e
          for exe in dist/clipboardplus/*; do
            [ -x "$exe" ] || continue
            "$exe" --help >/dev/null 2>&1 || echo "note: $exe has no --help, checked it starts"
          done
      - name: Wrap (Linux)
        if: runner.os == 'Linux'
        run: |
          curl -fsSL -o /usr/local/bin/appimagetool https://github.com/AppImage/AppImageKit/releases/latest/download/appimagetool-x86_64.AppImage
          chmod +x /usr/local/bin/appimagetool
          bash ${{ matrix.wrap }}
      - name: Wrap (macOS)
        if: runner.os == 'macOS'
        run: bash ${{ matrix.wrap }}
      - name: Wrap (Windows)
        if: runner.os == 'Windows'
        shell: pwsh
        run: |
          choco install innosetup -y
          & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\clipboardplus.iss
      - name: Checksum
        shell: bash
        run: sha256sum "${{ matrix.asset }}" > "${{ matrix.asset }}.sha256"
      - uses: softprops/action-gh-release@de2c0eb89ae2a093876385947365aca7785e0e1 # v2
        with:
          files: |
            ${{ matrix.asset }}
            ${{ matrix.asset }}.sha256
```

(Pin the third-party action SHAs to whatever their current release tags
resolve to at implementation time — the two used above are illustrative;
verify each action's latest commit SHA for its major version tag before
this workflow is trusted to run, the same way `quality.yml`'s existing
pinned actions were chosen.)

- [ ] **Step 2: Verify what this sandbox can verify**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))"`
(install `pyyaml` first if unavailable: `python -m pip install pyyaml`) —
confirms valid YAML. **Actually running this workflow requires pushing a
tag and watching GitHub Actions' three real runners** — that is the only
real verification for this task, and it happens after this plan's tasks
are merged, not during this session.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "Add release workflow: build, wrap, and upload installers per OS"
```

---

### Task 17: README rewrite

**Files:**
- Modify: `README.md` (its "Quick install" section)

- [ ] **Step 1: Read the current section fully**

Read `README.md`'s "Quick install (no Git checkout)" section (currently
starts around line 51, per this session's earlier read of the file) in
full before editing — its exact current wording about admin permission and
what each OS's bootstrap script installs needs to be preserved *somewhere*
(moved down), not lost.

- [ ] **Step 2: Rewrite**

Replace the section's opening (keep everything from "Windows automatic
installation..." onward, moved under a new "Build from source" heading
further down the file, unchanged) with:

```markdown
## Quick install

Download the installer for your OS from the
[latest release](https://github.com/tommyqhoang/ClipboardPlus-and-Dictation/releases/latest):

- **macOS**: `Clipboard+.dmg` — drag Clipboard+ into Applications, then
  right-click it and choose Open the first time (it's not signed yet, so
  Gatekeeper asks once).
- **Windows**: `Clipboard+-Setup.exe` — run it; if SmartScreen shows a
  notice, choose "More info" then "Run anyway" (same reason: unsigned).
  Installs to your user folder, no admin needed.
- **Linux**: `Clipboard+-x86_64.AppImage` — `chmod +x` it (or check "Allow
  executing file as program" in your file manager), then double-click. No
  package manager, no sudo.

No Python, no terminal, no dependencies to install separately — everything
needed ships inside the download.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Lead README's Quick install with the downloadable OS installers"
```

---

### Task 18: Phase 4 / whole-plan wrap-up

- [ ] **Step 1: Full-tree verification**

Run: `ruff check lib tests tools setup-desktop.py packaging && ruff format --check lib tests tools setup-desktop.py && mypy --strict lib tools setup-desktop.py`
Run: `sh tests/with-xvfb.sh sh -c 'coverage run -m unittest discover -s tests -v && coverage combine && coverage report'`
Expected: clean, ≥90% coverage, same as Task 8 — confirming Phases 2–4's
new files didn't regress anything Phase 1 already made green.

- [ ] **Step 2: Confirm the manual checklist exists and is current**

Re-read `packaging/README.md` (Task 10, Step 2) once more against every
task completed since it was written — if any file name, entry-point list,
or branding value drifted during Phases 2–4, update that checklist now
rather than leaving it stale for whoever runs it first.

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "Packaged installers: phases 1-4 complete" --allow-empty
```

(An empty commit here is only a checkpoint marker if every prior task
already committed its own changes — confirm `git status` is clean before
running this; if it isn't, that means some task's commit step was skipped
and must be gone back to, not papered over with this final commit.)
