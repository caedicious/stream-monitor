; Stream Monitor Installer Script for Inno Setup
; Download Inno Setup from: https://jrsoftware.org/isinfo.php

#define MyAppName "Stream Monitor"
#define MyAppVersion "1.1"
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
; Allow upgrading without uninstalling
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=no

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
; Run the setup wizard only on fresh install, or main app on upgrade
Filename: "{app}\{#MyAppSetupExeName}"; Description: "Configure Stream Monitor"; Flags: nowait postinstall skipifsilent; Check: IsFirstInstall
Filename: "{app}\{#MyAppExeName}"; Description: "Start Stream Monitor"; Flags: nowait postinstall skipifsilent; Check: IsUpgrade

[UninstallDelete]
; Only delete config on full uninstall, not upgrade
; Config is preserved during upgrade since we're not explicitly deleting it during install

[Code]
var
  ConfigExists: Boolean;

function InitializeSetup(): Boolean;
var
  ConfigPath: String;
begin
  // Check if config exists (indicates existing installation)
  ConfigPath := ExpandConstant('{userappdata}\StreamMonitor\config.json');
  ConfigExists := FileExists(ConfigPath);
  Result := True;
end;

function IsFirstInstall(): Boolean;
begin
  Result := not ConfigExists;
end;

function IsUpgrade(): Boolean;
begin
  Result := ConfigExists;
end;

// Remove startup shortcut on uninstall
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupPath: String;
  ConfigDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    StartupPath := ExpandConstant('{userstartup}\{#MyAppName}.lnk');
    if FileExists(StartupPath) then
      DeleteFile(StartupPath);
    
    // Delete config directory on full uninstall
    ConfigDir := ExpandConstant('{userappdata}\StreamMonitor');
    if DirExists(ConfigDir) then
      DelTree(ConfigDir, True, True, True);
  end;
end;
