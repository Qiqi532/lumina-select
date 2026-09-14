; =============================================================================
; 光影选片助手 installer.iss —— Inno Setup 安装脚本（Pascal 脚本）
;
; 作用：把 PyInstaller onedir 产物 dist\光影选片助手\ 文件夹封装为单个
;       setup.exe，安装后提供：桌面快捷方式 + 开始菜单项 + 标准卸载。
;
; 使用（中文/English）：
;   1) 先执行 build_dist.bat 生成 dist\光影选片助手\
;   2) 用 Inno Setup Compiler 打开本文件并编译（或命令行：
;      ISCC.exe installer.iss）
;   3) 产出 Output\LuminaSelect-v0.5-standard-dev-candidate.exe
;
; 说明：本脚本不打包模型权重；首次运行由各 exe 自动下载到安装目录
;       .hf_cache / .torch_cache（见 dist_runtime_hook.py）。
;       注意安装目录需有写入权限（模型会下载进安装目录）。
; =============================================================================

#define MyAppName "光影选片助手"
#define MyAppNameEn "Lumina Select"
#define MyAppVersion "0.5.0"
#define MyAppPublisher "Lumina Select / 光影选片助手"
#define MyAppURL "https://github.com/Qiqi532/lumina-select"
; 自包含 onedir 产物目录（相对本 .iss 文件）
#ifndef MySourceDir
  #define MySourceDir "dist\光影选片助手"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "Output"
#endif

[Setup]
; 基本信息
AppId=LuminaSelect.Standard
AppName={#MyAppName}
AppVerName={#MyAppName} {#MyAppVersion}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; 单一安装包（非管理员也可装到用户目录；如需机器级安装去掉 PrivilegesRequired）
PrivilegesRequired=lowest
; AppId 用于区分标准版与轻量版的升级/卸载记录
UninstallDisplayIcon={app}\光影选片助手.exe
; 输出
OutputDir={#MyOutputDir}
OutputBaseFilename=LuminaSelect-v0.5-standard-dev-candidate
Compression=lzma2
SolidCompression=yes
; 本应用由 64 位 Python 构建，仅允许支持 x64 应用的 Windows
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 允许在已安装目录写入模型缓存（程序运行时需要写 .hf_cache/.torch_cache）
DirExistsWarning=no
DisableDirPage=auto

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"
; 若需英文界面，取消下一行注释并确保已安装 English.isl
; Name: "english"; MessagesFile: "compiler:English.isl"

[Files]
; 递归拷贝整个 onedir 文件夹（含 exe + 全部依赖）
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MySourceDir}\_internal\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MySourceDir}\_internal\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 桌面快捷方式
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\光影选片助手.exe"; Tasks: desktopicon
; 开始菜单快捷方式
Name: "{group}\{#MyAppName}"; Filename: "{app}\光影选片助手.exe"
; 开始菜单 → 卸载
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 (Create a desktop shortcut)"; GroupDescription: "附加任务 (Additional tasks):"; Flags: unchecked

[Run]
; 完成页勾选"运行程序"
Filename: "{app}\光影选片助手.exe"; Description: "启动 {#MyAppName} (Launch {#MyAppNameEn})"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时一并清理模型缓存（可选；若想保留已下载模型请删除此段）
Type: filesandordirs; Name: "{app}\.hf_cache"
Type: filesandordirs; Name: "{app}\.torch_cache"
Type: filesandordirs; Name: "{app}\pycache"
