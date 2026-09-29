# Uninstalls Clipboard+ for the current user. Run in a normal PowerShell terminal.
#   .\uninstall.ps1            remove the app; keep history, settings, keys, models
#   .\uninstall.ps1 -Purge     ALSO permanently delete all user data (irreversible)
#   -Force                     with -Purge, skip the confirmation
param([switch]$Purge, [switch]$Force)
$ErrorActionPreference = 'Stop'
if ($Purge -and -not $Force) {
    $reply = Read-Host '-Purge permanently deletes all Clipboard+ user data (history, settings, account key, models). Continue? [y/N]'
    if ($reply -notmatch '^[yY]') { $Purge = $false }
}
$prefix = Join-Path $env:LOCALAPPDATA 'WhisperDictation\App'
$lib = Join-Path $prefix 'lib\whisper-dictation'
$venvRoot = Join-Path $prefix 'share\whisper-dictation\venv'
$venvScripts = Join-Path $venvRoot 'Scripts'
$python = Join-Path $venvScripts 'pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $venvScripts 'python.exe' }

if (-not (Test-Path -LiteralPath (Join-Path $lib 'dictation.py'))) {
    Write-Host 'Clipboard+ is not installed for this user.'
    if ($Purge) {
        Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'WhisperDictation') -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $HOME '.local\share\whisper.cpp\models') -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host 'Purged leftover user data.'
    }
    exit 0
}

# Stop the running app and the login item before removing its files. This shells
# out to the already-installed dictation/desktop/hotkeys modules (the checkout
# that ran bootstrap.ps1 no longer exists once quick-install finishes), and
# prints the Start Menu shortcut name(s) to remove: the current one plus any
# earlier names this app used, so a rename never leaves an orphaned shortcut.
$names = @()
if (Test-Path -LiteralPath $python) {
    $stopScript = @'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from dictation import Paths, busy
import desktop
import hotkeys
if busy(Paths()):
    sys.exit("Dictation is active. Stop or cancel it and wait for completion before uninstalling.")
runtime = Paths().runtime
if runtime.is_dir():
    (runtime / "menubar-quit").write_text("quit")
    (runtime / "clip-quit").write_text("quit")
library = Path(sys.argv[1])
prefix = desktop.install_prefix(library / "app.py")
gui = prefix / "share/whisper-dictation/venv/Scripts/pythonw.exe"
hotkeys.set_login_item(False, [str(gui), str(library / "tray.py")])
for name in (hotkeys.APP_NAME, *hotkeys.FORMER_NAMES):
    print(name)
'@
    $tempPy = Join-Path $env:TEMP ('clipboardplus-uninstall-' + [Guid]::NewGuid().ToString('N') + '.py')
    Set-Content -LiteralPath $tempPy -Value $stopScript -Encoding utf8
    try {
        $names = & $python $tempPy $lib
        if ($LASTEXITCODE -ne 0) {
            throw 'Dictation is active, or the installed environment could not be read. Stop it and try again.'
        }
    } finally {
        Remove-Item -LiteralPath $tempPy -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Warning 'The installed Python environment is missing; skipping the running-app check and login item removal.'
}

$programs = [Environment]::GetFolderPath('Programs')
foreach ($name in $names) {
    $link = Join-Path $programs ($name + '.lnk')
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
}

# Only this app's own files.
$modules = @(
    'telemetry', 'dictation', 'desktop', 'onboarding', 'rewriting', 'workflow', 'app',
    'app_service', 'browserauth', 'permissions', 'cues', 'app_styles', 'app_settings', 'menubar_logic', 'logsetup', 'hotkeys', 'shortcut_test', 'shortcut_panel', 'menubar', 'tray', 'traymenu', 'clipboardplus', 'clipstore', 'clipwatch',
    'clipwatch_linux', 'clipwatch_macos', 'clipwatch_windows', 'clipservice', 'clipsync',
    'clipcontrol', 'clipui', 'overlay', 'updates', 'engine'
)
$assets = @(
    'tray-recording.png', 'menubar-icon.png', 'menubar-recording.png',
    'whisper-dictation.png', 'whisper-dictation.ico'
)
foreach ($module in $modules) {
    Remove-Item -LiteralPath (Join-Path $lib "$module.py") -Force -ErrorAction SilentlyContinue
}
foreach ($asset in $assets) {
    Remove-Item -LiteralPath (Join-Path $lib $asset) -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath (Join-Path $lib '__pycache__') -Recurse -Force -ErrorAction SilentlyContinue
if ((Get-ChildItem -LiteralPath $lib -Force -ErrorAction SilentlyContinue | Measure-Object).Count -eq 0) {
    Remove-Item -LiteralPath $lib -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath (Join-Path $prefix 'bin\dictate-toggle.cmd') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $prefix '.dictation-install.json') -Force -ErrorAction SilentlyContinue
# Only the private environment the installer created.
if (Test-Path -LiteralPath (Join-Path $venvRoot 'pyvenv.cfg')) {
    Remove-Item -LiteralPath $venvRoot -Recurse -Force -ErrorAction SilentlyContinue
}
if ($Purge) {
    Start-Sleep -Seconds 1  # Let the tray and clipboard service see their quit files.
    # Config/ holds clipboard/ (history database), the account key, telemetry-id,
    # settings and models/; Cache/ holds recoverable audio.
    Remove-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'WhisperDictation') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $HOME '.local\share\whisper.cpp\models') -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host 'Removed the command and runtime module, and PURGED all user data (history, settings, keys, telemetry id, models).'
} else {
    Write-Host 'Removed the command and runtime module. Models, settings, saved transcripts and clipboard history were retained (re-run with -Purge to delete them).'
}
