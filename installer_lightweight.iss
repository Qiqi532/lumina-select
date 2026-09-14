; =============================================================================
; Lumina Select Lightweight installer (OpenCV heuristic backend)
; Builds from dist_light\光影选片助手 and can coexist with the Torch edition.
; =============================================================================

#define MyAppName "光影选片助手轻量版"
#define MyAppNameEn "Lumina Select Lightweight"
#define MyAppVersion "0.5.0"
#define MyAppPublisher "Lumina Select / 光影选片助手"
#define MyAppURL "https://github.com/Qiqi532/lumina-select"
#ifndef MySourceDir
  #define MySourceDir "dist_light\光影选片助手"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "Output_light"
#endif

[Setup]
AppId=LuminaSelect.Lightweight
AppName={#MyAppName}
AppVerName={#MyAppName} {#MyAppVersion}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\光影选片助手.exe
OutputDir={#MyOutputDir}
OutputBaseFilename=LuminaSelect-v0.5-lightweight-dev-candidate
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DirExistsWarning=no
DisableDirPage=auto

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MySourceDir}\_internal\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MySourceDir}\_internal\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\光影选片助手.exe"; Tasks: desktopicon
Name: "{group}\{#MyAppName}"; Filename: "{app}\光影选片助手.exe"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 (Create a desktop shortcut)"; GroupDescription: "附加任务 (Additional tasks):"; Flags: unchecked

[Run]
Filename: "{app}\光影选片助手.exe"; Description: "启动 {#MyAppName} (Launch {#MyAppNameEn})"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\pycache"
