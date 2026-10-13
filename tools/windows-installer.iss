; 原生 Windows x64 安装器；仅替换程序文件，升级和卸载均保留用户运行数据。
#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
; 固定 AppId 让后续版本升级同一安装，不随版本号创建另一份部署。
AppId={{379D6B51-BAED-48B4-A39D-FB1E5A424042}
AppName=AgentBot
AppVersion={#AppVersion}
AppPublisher=AgentBot
DefaultDirName={localappdata}\Programs\AgentBot
DefaultGroupName=AgentBot
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=AgentBot-{#AppVersion}-windows-x64-Setup
SetupIconFile=..\assets\desktop\agentbot-icon-v2.ico
UninstallDisplayIcon={app}\AgentBot.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
; 替换双入口和共享依赖前提示关闭本实例，不在安装后自动重启后端。
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; 白名单避免将构建输出中的 .env、data、logs 或媒体递归打入安装器。
Source: "{#BundleDir}\AgentBot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\agent-bot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BundleDir}\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\WINDOWS_README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\AgentBot"; Filename: "{app}\AgentBot.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\AgentBot"; Filename: "{app}\AgentBot.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 静默安装不启动应用；首次配置仍由用户启动程序时独立生成。
Filename: "{app}\AgentBot.exe"; Description: "Launch AgentBot (requires Microsoft Edge WebView2 Runtime)"; Flags: nowait postinstall skipifsilent unchecked

; 不定义删除运行目录的 InstallDelete/UninstallDelete，保留配置、SQLite、日志和媒体。
