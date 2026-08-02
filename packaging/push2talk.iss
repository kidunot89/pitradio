; Inno Setup script for Push2Talk.
;
; Build the Nuitka dist first (packaging/build.py), then:
;   iscc /DAppVersion=0.1.0 packaging\push2talk.iss
;
; Notes that matter:
; * PrivilegesRequired=admin, because the app cannot type into an elevated sim
;   without it and the failure is silent.
; * Nothing is written to {app} at runtime — config lives in %APPDATA% and logs
;   and the Whisper model in %LOCALAPPDATA% — so an update can replace this
;   directory wholesale without losing settings or re-downloading 250MB.
; * CloseApplications lets the self-updater hand over to this installer while
;   the app is running.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Push2Talk"
#define AppPublisher "Geoff Taylor"
#define AppURL "https://github.com/kidunot89/push2talk"
#define AppExe "push2talk.exe"

[Setup]
AppId={{8E6F1C24-9A3D-4B77-8C1E-3F5A2D7B9E04}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=push2talk-setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\build\push2talk.dist\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %APPDATA%\push2talk alone: config, logs and the cached model are the
; user's, and an uninstall/reinstall cycle should not cost them a 250MB
; download or their tuned delays.
Type: filesandordirs; Name: "{localappdata}\push2talk\updates"

[Code]
// The scheduled task is created by the app itself (Settings -> Start with
// Windows), so removing it here is the only way it gets cleaned up.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
    Exec(ExpandConstant('{sys}\schtasks.exe'),
         '/delete /f /tn Push2Talk', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;
