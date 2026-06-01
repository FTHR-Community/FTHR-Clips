; FTHR Clips — Windows Installer (Inno Setup 6)
; Windows-only: bundles the DXGI capture engine. Does NOT include the Linux engine.
; Build: iscc installer_windows.iss
; Prerequisite: redist\vc_redist.x64.exe — https://aka.ms/vs/17/release/vc_redist.x64.exe

#define MyAppName      "FTHR Clips"
#define MyAppVersion   "1.0.0-alpha"
#define MyAppPublisher "FTHR"
#define MyAppExeName   "FTHRClips.exe"
#define MyAppURL       "https://github.com/fthr/clips"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\FTHRClips
DefaultGroupName={#MyAppName}
OutputBaseFilename=FTHRClips_Setup_Windows
OutputDir=Output

; Compression — maximum, solid
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=4

; Visual style
WizardStyle=modern
WizardResizable=yes
WizardSizePercent=120
WizardImageFile=installer_assets\wizard_banner.bmp
WizardSmallImageFile=installer_assets\wizard_small.bmp

; Icon
SetupIconFile=FTHR_UI\assets\fthr_logo.ico
UninstallDisplayIcon={app}\FTHRClips.exe

; Platform
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0

; Uninstall
UninstallDisplayName={#MyAppName}
CreateUninstallRegKey=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.WelcomeLabel1=Welcome to FTHR Clips Setup
english.WelcomeLabel2=FTHR Clips is a lightweight game capture tool for Windows.%n%nThis will install version {#MyAppVersion} on your computer.%n%nClose all running applications before continuing.
english.FinishedHeadingLabel=FTHR Clips is installed!
english.FinishedLabel=FTHR Clips has been installed on your computer. Use the desktop shortcut or Start Menu to launch it.%n%nFor global hotkeys to work, no additional setup is required on Windows.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Windows bundle — produced by: pyinstaller FTHR.spec --clean
Source: "dist\FTHRClips\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; \
    Comment: "Launch FTHR Clips game capture"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon; Comment: "Launch FTHR Clips game capture"

[Run]
; VC++ Runtime (silent)
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/quiet /norestart"; \
    StatusMsg: "Installing Visual C++ Runtime..."; Flags: waituntilterminated

; Optional launch after install
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\fthr"
