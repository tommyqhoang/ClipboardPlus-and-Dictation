# packaging/windows/clipboardplus.spec
# Run from the repo root: pyinstaller packaging/windows/clipboardplus.spec
import sys
from pathlib import Path

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
            ],
            hiddenimports=[],
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
        name=entry,
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
