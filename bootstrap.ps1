# Windows x64 installer. Run in a normal PowerShell terminal, not as Administrator.
param([string]$Ref = 'main')
$ErrorActionPreference = 'Stop'
if ($Ref -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid application ref.' }
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
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$knownPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
if (Test-Path -LiteralPath $knownPython) { $pythonPath = $knownPython }
elseif ($python) { $pythonPath = $python.Source }
else { throw 'Python was installed but cannot be located. Restart PowerShell and rerun.' }
$work = Join-Path ([IO.Path]::GetTempPath()) ('whisper-dictation-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $whisperZip = Join-Path $work 'whisper.zip'
    # Pin both version and digest, keeping all DLLs beside the executable.
    Invoke-WebRequest -UseBasicParsing 'https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.3/whisper-bin-x64.zip' -OutFile $whisperZip
    if ((Get-FileHash $whisperZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'd824b1e37599f882b396e73f1ee0bfd5d0529f700314c48311dcbd00b803321d') {
        throw 'Whisper archive checksum mismatch.'
    }
    $engine = Join-Path $env:LOCALAPPDATA 'WhisperDictation\Engines\whisper-1.8.3'
    Expand-Archive -LiteralPath $whisperZip -DestinationPath $engine -Force
    $cli = Get-ChildItem -LiteralPath $engine -Filter whisper-cli.exe -Recurse | Select-Object -First 1
    if (-not $cli) { throw 'Whisper archive did not contain whisper-cli.exe.' }
    $env:Path = $cli.DirectoryName + ';' + $env:Path
    $archive = Join-Path $work 'app.zip'
    Invoke-WebRequest -UseBasicParsing "https://github.com/tommyqhoang/wayland-whisper-dictation/archive/$Ref.zip" -OutFile $archive
    $source = Join-Path $work 'source'
    Expand-Archive -LiteralPath $archive -DestinationPath $source
    $app = Get-ChildItem -LiteralPath $source -Directory | Select-Object -First 1
    & $pythonPath (Join-Path $app.FullName 'setup-desktop.py')
    if ($LASTEXITCODE -ne 0) { throw 'Application setup failed.' }
    & $pythonPath (Join-Path $app.FullName 'setup-desktop.py') --launch-only
    if ($LASTEXITCODE -ne 0) { throw 'Could not open the app. Open Whisper Dictation from the Start Menu.' }
} finally {
    # Only the unique temporary directory allocated above is removed.
    Remove-Item -LiteralPath $work -Recurse -Force
}
