# Windows x64 installer. Run in a normal PowerShell terminal, not as Administrator.
param([string]$Ref = '')
$ErrorActionPreference = 'Stop'
if ($Ref -and $Ref -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid application ref.' }
$Repo = 'tommyqhoang/ClipboardPlus-and-Dictation'
# Used only when the GitHub API cannot be reached; bump with each release.
$PinnedTag = 'v1.6.7'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Automatic Windows install currently supports x64. ARM64 requires manual dependencies.'
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Install Microsoft App Installer (winget) from Microsoft Store, then rerun this command.'
}
Write-Host 'Installing Python, FFmpeg and the Microsoft C++ runtime. Package managers may request approval.'
foreach ($package in @('Python.Python.3.11', 'Gyan.FFmpeg', 'Microsoft.VCRedist.2015+.x64')) {
    & winget install --id $package --exact --source winget --accept-source-agreements --accept-package-agreements
    # APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE: already installed/current.
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        throw "Dependency installation failed: $package (exit $LASTEXITCODE)"
    }
}
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
function Find-Python {
    # A leftover Python without Tk, or the Microsoft Store stub, must not be chosen.
    $ErrorActionPreference = 'Continue'
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $found = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { $candidates += "$found".Trim() }
    }
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    $candidates += Join-Path $env:ProgramFiles 'Python311\python.exe'
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*\WindowsApps\*') { $candidates += $onPath.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            & $candidate -c 'import tkinter, venv' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}
# winget checks each installer against the SHA-256 in its manifest; confirm FFmpeg works.
if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
    $ffmpeg = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ffmpeg) { throw 'FFmpeg was not found after installation.' }
    $env:Path = $ffmpeg.DirectoryName + ';' + $env:Path
}
$pythonPath = Find-Python
if (-not $pythonPath) {
    throw 'No Python 3 with Tk (tkinter) was found. Install Python 3.11 from python.org with the tcl/tk option, or restart PowerShell and rerun.'
}
$work = Join-Path ([IO.Path]::GetTempPath()) ('whisper-dictation-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    try {
        $whisperZip = Join-Path $work 'whisper.zip'
        # Pin both version and digest, keeping all DLLs beside the executable.
        Invoke-WebRequest -UseBasicParsing 'https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.7/whisper-bin-x64.zip' -OutFile $whisperZip
        if ((Get-FileHash $whisperZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'd9627486e1c34a03745880485593473e047294260ce9a3cb0aa8deaf15b99af6') {
            throw 'Whisper archive checksum mismatch.'
        }
        $engine = Join-Path $env:LOCALAPPDATA 'WhisperDictation\Engines\whisper-1.8.7'
        Expand-Archive -LiteralPath $whisperZip -DestinationPath $engine -Force
        $cli = Get-ChildItem -LiteralPath $engine -Filter whisper-cli.exe -Recurse | Select-Object -First 1
        if (-not $cli) { throw 'Whisper archive did not contain whisper-cli.exe.' }
        $env:Path = $cli.DirectoryName + ';' + $env:Path
    } catch {
        Write-Warning "Speech engine setup failed: $_. Clipboard+ will still install; retry speech setup later."
    }
    if (-not $Ref) {
        try {
            $Ref = (Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest").tag_name
        } catch { $Ref = $null }
        if ($Ref -notmatch '^v?\d+\.\d+\.\d+$') { $Ref = $PinnedTag }
    }
    Write-Host "Installing release $Ref."
    # Fail closed: the zip is unpacked only if it matches the checksum the release published.
    $sums = Join-Path $work 'SHA256SUMS'
    Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/releases/download/$Ref/SHA256SUMS" -OutFile $sums
    $archive = Join-Path $work 'app.zip'
    $expected = $null
    foreach ($line in Get-Content -LiteralPath $sums) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$' -and ($Matches[2] -eq "clipboardplus-source-$Ref.zip" -or $Matches[2] -eq "$Ref.zip")) {
            $expected = $Matches[1].ToLowerInvariant()
            break
        }
    }
    if (-not $expected) { throw "Release $Ref has no published checksum for its source zip; refusing to install." }
    Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/tags/$Ref.zip" -OutFile $archive
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'The downloaded application did not match its published checksum; nothing was installed.'
    }
    $source = Join-Path $work 'source'
    Expand-Archive -LiteralPath $archive -DestinationPath $source
    $app = Get-ChildItem -LiteralPath $source -Directory | Select-Object -First 1
    & $pythonPath (Join-Path $app.FullName 'setup-desktop.py')
    if ($LASTEXITCODE -ne 0) { throw 'Clipboard+ could not install and open.' }
} finally {
    # Only the unique temporary directory allocated above is removed.
    Remove-Item -LiteralPath $work -Recurse -Force
}
