; packaging/windows/clipboardplus.iss
; Run after: pyinstaller packaging/windows/clipboardplus.spec
; Build with: iscc packaging\windows\clipboardplus.iss
; CI passes the real version via /DAppVersion=... (see release.yml); this
; fallback only fires for a manual local build run without that define.
#ifndef AppVersion
  #define AppVersion "1.2.1"
#endif
[Setup]
AppName=Clipboard+
AppVersion={#AppVersion}
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
