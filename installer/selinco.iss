#define AppName "色林错预警预报平台"
#define AppVersion "0.3.0"
#ifndef AppSource
  #define AppSource "..\build\verified-dist\SelincoPlatform"
#endif
[Setup]
AppId={{8CCB9CE5-4353-48F0-8AD3-2F5687B792D0}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\SelincoPlatform
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\releases
OutputBaseFilename=色林错预警预报平台-v0.3.0-Setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma/fast
SolidCompression=yes
WizardStyle=modern
UninstallFilesDir={app}\uninstall
UninstallDisplayIcon={app}\SelincoPlatform.exe
CloseApplications=yes
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Excludes: "\workspace\*,\restored-workspaces\*,\config\active-workspace.json"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "start_platform.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\start_platform.ps1"""; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\start_platform.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon
[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then MsgBox('卸载保留 workspace、restored-workspaces 中的用户资料。请在迁移前通过平台完成备份。', mbInformation, MB_OK);
end;
