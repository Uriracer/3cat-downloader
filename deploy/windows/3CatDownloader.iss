; 3Cat Downloader — Inno Setup 6 script (per-user install, no admin rights needed).
; Build after PyInstaller:
;   ISCC.exe /DMyAppVersion=0.2.0 deploy\windows\3CatDownloader.iss
; Relative paths are relative to this file.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "..\..\dist\3CatDownloader"
#endif
#define MyAppName "3Cat Downloader"
#define MyAppExe "3CatDownloader.exe"

[Setup]
AppId={{6C1F2E7A-3CA7-4D1B-9B5E-3CA7D0A1B2C3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=Oriol Serrat
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=3CatDownloader-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
CloseApplications=yes

[Languages]
Name: "catalan"; MessagesFile: "compiler:Languages\Catalan.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

; Settings, queue and logs live in %APPDATA%\3CatDownloader and are kept on uninstall,
; and downloaded videos are never touched.
