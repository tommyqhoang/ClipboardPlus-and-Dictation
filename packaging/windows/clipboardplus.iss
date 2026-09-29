; packaging/windows/clipboardplus.iss
; Run after: pyinstaller packaging/windows/clipboardplus.spec
; Build with: iscc /DAppVersion=<desktop.APP_VERSION> packaging\windows\clipboardplus.iss
; CI reads desktop.APP_VERSION and supplies it via /DAppVersion (release.yml).
#ifndef AppVersion
  #error AppVersion must be supplied from desktop.APP_VERSION
#endif
[Setup]
; AppId identifies this app across upgrades and uninstall. NEVER change it.
AppId={{28F136BA-E9C1-4A6F-BEC1-A6E8AEEB90B2}
AppName=Clipboard+
AppVersion={#AppVersion}
AppVerName=Clipboard+ {#AppVersion}
AppPublisher=Tommy Hoang
AppPublisherURL=https://github.com/tommyqhoang/ClipboardPlus-and-Dictation
AppSupportURL=https://github.com/tommyqhoang/ClipboardPlus-and-Dictation/issues
AppUpdatesURL=https://github.com/tommyqhoang/ClipboardPlus-and-Dictation/releases
AppCopyright=Copyright (c) 2026 Tommy Hoang
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Clipboard+
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Tommy Hoang
VersionInfoDescription=Clipboard+ Setup
LicenseFile=..\..\LICENSE
CloseApplications=yes
RestartApplications=no
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
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Clipboard+"; Filename: "{app}\Clipboard+.exe"
Name: "{userdesktop}\Clipboard+"; Filename: "{app}\Clipboard+.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Clipboard+.exe"; Flags: nowait postinstall skipifsilent

[Code]
{ Uninstall keeps the user's data by default, like the other platforms. The
  question is skipped for silent uninstalls, which always keep it. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ClipboardPlus');
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) then
  begin
    if MsgBox('Also delete your Clipboard+ data (clipboard history, settings, account key and downloaded models)?' + #13#10 + #13#10 +
              'Choose No to keep it for a future reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\WhisperDictation'), True, True, True);
      DelTree(ExpandConstant('{%USERPROFILE}\.local\share\whisper.cpp\models'), True, True, True);
    end;
  end;
end;
