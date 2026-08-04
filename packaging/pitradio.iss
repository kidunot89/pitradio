; Inno Setup script for PitRadio.
;
; Build the Nuitka dist first (packaging/build.py), then:
;   iscc /DAppVersion=0.1.0 packaging\pitradio.iss
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

#define AppName "PitRadio"
#define AppPublisher "Geoff Taylor"
#define AppURL "https://github.com/kidunot89/pitradio"
#define AppExe "pitradio.exe"

[Setup]
AppId={{A292DFB9-10EC-463E-B766-771B660524FA}
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
OutputBaseFilename=pitradio-setup-{#AppVersion}
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
Source: "..\build\pitradio.dist\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; shellexec is required, not cosmetic. Inno runs postinstall entries as the
; originating (non-elevated) user via CreateProcess, which refuses to launch a
; requireAdministrator binary and fails with code 740, ERROR_ELEVATION_REQUIRED.
; ShellExecuteEx honours the manifest and raises the UAC prompt instead.
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent shellexec

; For anyone driving Setup silently by hand. A silent install skips every
; postinstall entry, so without this the app is replaced and never started
; again. Runs only when silent, so an interactive install still offers the
; checkbox above and does not launch twice.
;
; The self-updater no longer installs silently — a silent install that fails
; leaves nothing on screen and no exit code anyone reads, which is how two
; releases in a row appeared to do nothing at all. See updater.launch_installer.
Filename: "{app}\{#AppExe}"; Flags: nowait shellexec; Check: LaunchAfterSilentInstall

[UninstallDelete]
; Leave %APPDATA%\pitradio alone: config, logs and the cached model are the
; user's, and an uninstall/reinstall cycle should not cost them a 250MB
; download or their tuned delays.
Type: filesandordirs; Name: "{localappdata}\pitradio\updates"

[Code]
// The scheduled task is created by the app itself (Settings -> Start with
// Windows), so removing it here is the only way it gets cleaned up.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
    Exec(ExpandConstant('{sys}\schtasks.exe'),
         '/delete /f /tn PitRadio', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;

[Code]
function LaunchAfterSilentInstall: Boolean;
begin
  { A silent install skips every postinstall entry, so nothing restarts the
    app — which is why it used to vanish after a self-update. This entry fills
    that gap.

    But CI installs silently too, to prove the installer works, and it must not
    be left with a GUI that never exits: the run hung for twenty-four minutes
    on a step that takes one, then could not uninstall a running exe. CI passes
    /NOLAUNCH=1. }
  Result := WizardSilent and (ExpandConstant('{param:NOLAUNCH|0}') <> '1');
end;
