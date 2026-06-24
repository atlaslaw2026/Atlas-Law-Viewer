#define MyAppName "Atlas Law Viewer"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Atlas Law"
#define MyAppExeName "launch_atlas_standard.cmd"
#define MyAppURL "http://127.0.0.1:8080/"

[Setup]
AppId={{BF9B7541-8758-47D4-8AFA-0E6731316A0C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Atlas Law Viewer
DefaultGroupName=Atlas Law Viewer
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir=..\dist_installer
OutputBaseFilename=AtlasLawViewerSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "bootstrap"; Description: "Run dependency bootstrap after install (fallback when bundled runtime is missing)"; GroupDescription: "Post-install:"; Flags: unchecked

[Files]
Source: "..\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: ".git\*;.venv\*;__pycache__\*;.pytest_cache\*;logs\*;ninth_pdfs\*;central_pdfs\*;*.lnk;*.pyc;*.pyo;atlas_law.backup*.db;dist_installer\*;installer\*"
Source: "..\installer\AtlasLawViewer.iss"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "..\runtime\python\*"; DestDir: "{app}\runtime\python"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\Atlas Law Viewer\Atlas Law Viewer"; Filename: "{cmd}"; Parameters: "/c ""{app}\{#MyAppExeName}"""; WorkingDir: "{app}"
Name: "{autoprograms}\Atlas Law Viewer\Update All Courts"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\atlas_daily_refresh.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\Atlas Law Viewer\Open Atlas in Browser"; Filename: "{#MyAppURL}"
Name: "{autodesktop}\Atlas Law Viewer"; Filename: "{cmd}"; Parameters: "/c ""{app}\{#MyAppExeName}"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\atlas_bootstrap.ps1"""; Description: "Install runtime dependencies"; Flags: runhidden waituntilterminated; Tasks: bootstrap
Filename: "{cmd}"; Parameters: "/c ""{app}\{#MyAppExeName}"""; Description: "Launch Atlas Law Viewer"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\.venv"
