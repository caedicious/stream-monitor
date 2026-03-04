; Stream Monitor Installer Script for Inno Setup
; Download Inno Setup from: https://jrsoftware.org/isinfo.php

#define MyAppName "Stream Monitor"
#define MyAppVersion "1.0"
#define MyAppPublisher "Stream Monitor"
#define MyAppExeName "StreamMonitor.exe"
#define MyAppSetupExeName "StreamMonitorSetup.exe"
#define MyAppSettingsExeName "StreamMonitorSettings.exe"

[Setup]
AppId={{8F3B5A91-7D4E-4C8F-9A2B-1E6D3F5C7A8B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=installer_output
OutputBaseFilename=StreamMonitorInstaller
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyAppSetupExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyAppSettingsExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} Settings"; Filename: "{app}\{#MyAppSetupExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Start Stream Monitor with Windows"

[Run]
; Run the setup wizard after installation
Filename: "{app}\{#MyAppSetupExeName}"; Description: "Configure Stream Monitor"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\StreamMonitor"

[Code]
// Remove startup shortcut on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupPath: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    StartupPath := ExpandConstant('{userstartup}\{#MyAppName}.lnk');
    if FileExists(StartupPath) then
      DeleteFile(StartupPath);
  end;
end;
