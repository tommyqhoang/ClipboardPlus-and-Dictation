# packaging/windows/clipboardplus.spec
# Run from the repo root: pyinstaller packaging/windows/clipboardplus.spec
import sys
from pathlib import Path

# clipboardplus loads the OS keychain with importlib, so PyInstaller cannot see it:
# bundle keyring, its backends and its metadata (entry-point discovery) explicitly.
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

KEYRING_IMPORTS = collect_submodules("keyring")
try:
    KEYRING_DATA = copy_metadata("keyring")
except Exception:  # keyring is optional; without it the key file fallback is used.
    KEYRING_DATA = []

block_cipher = None
ROOT = Path(SPECPATH).resolve().parents[1]
LIB = ROOT / "lib"
# Shadows _pyinstaller_hooks_contrib's stock hook-workflow.py, written for an
# unrelated PyPI package also named "workflow" — see packaging/hooks/hook-workflow.py.
# (Found by actually running the Linux build in Task 10; carried into every OS spec.)
HOOKS = [str(ROOT / "packaging" / "hooks")]

# Same entry points as Linux — Windows also uses tray.py, not menubar.py.
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
                *KEYRING_DATA,
            ],
            hiddenimports=[*KEYRING_IMPORTS],
            hookspath=HOOKS,
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
        # The tray is the visible program: name it for the product, not "tray.exe".
        name="Clipboard+" if entry == "tray" else entry,
        icon=str(ROOT / "assets" / "icon.ico"),
        # No console window on Windows for any of these — including the
        # background workers, which would otherwise flash a black window.
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
