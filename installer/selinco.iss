#define AppName "色林错预警预报平台"
#ifndef AppVersion
  #define AppVersion "0.12.0"
#endif
#ifndef AppSource
  #define AppSource "..\build\verified-dist\SelincoPlatform"
#endif
[Setup]
#ifdef VerificationInstall
AppId={{2B4C2395-4945-4697-8215-1B448A30FBA2}
#else
AppId={{8CCB9CE5-4353-48F0-8AD3-2F5687B792D0}
#endif
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\SelincoPlatform
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\releases
#ifdef VerificationInstall
OutputBaseFilename=色林错预警预报平台-v{#AppVersion}-Verification-Setup
#else
OutputBaseFilename=色林错预警预报平台-v{#AppVersion}-Setup
#endif
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
#ifndef VerificationInstall
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
#endif
[Icons]
#ifndef VerificationInstall
Name: "{userprograms}\{#AppName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\start_platform.ps1"""; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\start_platform.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon
#endif
[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then MsgBox('卸载保留 workspace、restored-workspaces 中的用户资料。请在迁移前通过平台完成备份。', mbInformation, MB_OK);
end;
