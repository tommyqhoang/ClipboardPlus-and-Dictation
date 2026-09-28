; packaging/windows/clipboardplus.iss
; Run after: pyinstaller packaging/windows/clipboardplus.spec
; Build with: iscc /DAppVersion=<desktop.APP_VERSION> packaging\windows\clipboardplus.iss
; CI reads desktop.APP_VERSION and supplies it via /DAppVersion (release.yml).
#ifndef AppVersion
  #error AppVersion must be supplied from desktop.APP_VERSION
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
UninstallDisplayIcon={app}\Clipboard+.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\clipboardplus\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\Clipboard+"; Filename: "{app}\Clipboard+.exe"
Name: "{userdesktop}\Clipboard+"; Filename: "{app}\Clipboard+.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Clipboard+.exe"; Flags: nowait postinstall skipifsilent
