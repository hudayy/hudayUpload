; Inno Setup 6 script for hudayUpload
; Download Inno Setup: https://jrsoftware.org/isinfo.php
; Build: ISCC installer.iss   (or run build.bat, which calls ISCC if it is found)

#define MyAppName      "hudayUpload"
#define MyAppVersion   "1.7.0"
#define MyAppPublisher "huday"
#define MyAppURL       "https://github.com/hudayy/hudayUpload"
#define MyAppExeName   "hudayUpload.exe"

[Setup]
; Unique ID — do not change between releases (used for upgrade detection)
AppId={{F7A2C9D5-3B1E-4F82-A6C0-8D4E2F0B7A91}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Install to Program Files\hudayUpload
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Allow users to skip the Start Menu folder
AllowNoIcons=yes
; Output
OutputDir=dist
OutputBaseFilename=hudayUpload_Setup_{#MyAppVersion}
SetupIconFile=assets\icon.ico
; Compression
Compression=lzma2/ultra64
SolidCompression=yes
; Modern wizard UI
WizardStyle=modern
; Require admin so we can write to Program Files
PrivilegesRequired=admin
; Show the app icon in Add/Remove Programs
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
  Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; \
  Flags: unchecked

[Files]
; The main executable — mark ignoreversion so upgrades always overwrite
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}";                   Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";         Filename: "{uninstallexe}"
; Optional desktop shortcut
Name: "{autodesktop}\{#MyAppName}";             Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch the app after installation
Filename: "{app}\{#MyAppExeName}"; \
  Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the app data folder on uninstall (optional — comment out to keep settings)
; Type: filesandordirs; Name: "{userappdata}\RLReplayUploader"
