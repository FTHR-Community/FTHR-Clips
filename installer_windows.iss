; FTHR Clips — Windows Installer (Inno Setup 6)
; Windows-only: bundles the DXGI capture engine. Does NOT include the Linux engine.
; Build: iscc installer_windows.iss
; Prerequisite: redist\vc_redist.x64.exe — https://aka.ms/vs/17/release/vc_redist.x64.exe

#define MyAppName      "FTHR Clips"
#define MyAppVersion   "1.1.0-alpha"
#define MyAppPublisher "FTHR Community"
#define MyAppExeName   "FTHRClips.exe"
#define MyAppURL       "https://github.com/fthr/clips"
#define MyAppId        "{{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}"
; The source is the Microsoft permalink documented in BUILDING.md.  Its binary
; version is read at compile time, so the runtime check never claims that an
; older VC++ runtime is sufficient for the redistributable actually bundled.
#define VCRedistVersion GetVersionNumbersString(AddBackslash(SourcePath) + "redist\vc_redist.x64.exe")

#if VCRedistVersion == ""
  #error "redist\\vc_redist.x64.exe has no readable version resource; run python tools/fetch_third_party.py --vcredist"
#endif

#ifndef BundleDir
#define BundleDir "dist\FTHRClips"
#endif

[Setup]
; Do not change this AppId. It is the update/uninstall identity of the
; already-installed public-alpha predecessor.
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright=Copyright (c) 2026 FTHR Community
DefaultDirName={autopf}\FTHRClips
DefaultGroupName={#MyAppName}
UsePreviousAppDir=yes
UsePreviousGroup=yes
OutputBaseFilename=FTHRClips-Setup-{#MyAppVersion}-x64
OutputDir=Output

; Keep Installer metadata visible and consistent with the frozen executable.
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=FTHR Clips Windows Setup
VersionInfoProductName={#MyAppName}
; PE ProductVersion is numeric; keep the alpha suffix in the textual field.
VersionInfoProductVersion=1.1.0.0
VersionInfoTextVersion={#MyAppVersion}
VersionInfoVersion=1.1.0.0

; Compression — maximum, solid
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=2

; The installer is rendered as the same square, black popup used by the app.
; Pascal code supplies the frameless title bar and the only visible artwork.
WizardStyle=modern dark hidebevels
WizardSizePercent=100
DisableWelcomePage=yes
DisableFinishedPage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
WizardBackColor=#000000

; Icon
SetupIconFile=FTHR_UI\assets\favicon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; Platform
#ifdef PreviewBuild
PrivilegesRequired=lowest
#else
PrivilegesRequired=admin
#endif
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; Never force-kill a capture session. Restart Manager offers to close the
; processes holding FTHR files; an unresolved lock leaves the user in control.
CloseApplications=yes
CloseApplicationsFilter=FTHRClips.exe
RestartApplications=no

; Uninstall
UninstallDisplayName={#MyAppName}
CreateUninstallRegKey=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.WelcomeLabel1=Welcome to FTHR Clips Setup
english.WelcomeLabel2=FTHR Clips is a lightweight game capture tool for Windows.%n%nThis will install or update version {#MyAppVersion} on your computer. An existing matching version is repaired in place.%n%nClose FTHR Clips before continuing so capture can shut down cleanly.
english.FinishedHeadingLabel=FTHR Clips is installed!
english.FinishedLabel=FTHR Clips has been installed on your computer. Use the desktop shortcut or Start Menu to launch it.%n%nFor global hotkeys to work, no additional setup is required on Windows.
english.SelectDirLabel3=Choose where the FTHR Clips application files should live, then continue.
english.SelectTasksLabel2=Choose whether Setup should create a desktop shortcut.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; PyInstaller owns the complete _internal tree. Replace it atomically at the
; installer boundary so an in-place update cannot retain incompatible DLLs,
; retired modules, or removed assets from an older bundle. This deliberately
; does not delete {app} itself and cannot reach clips or user settings.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Windows bundle — produced by: pyinstaller FTHR.spec --clean --noconfirm
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The installer uses the same Oswald face as the application while it is open.
Source: "FTHR_UI\assets\fonts\Oswald-Bold.ttf"; Flags: dontcopy noencryption
; The title bar uses only the exact application favicon and existing close icon.
Source: "FTHR_UI\assets\favicon.ico"; Flags: dontcopy noencryption
Source: "FTHR_UI\assets\icons\close.png"; Flags: dontcopy noencryption
; Licence paperwork (AUDIT-005/AUDIT-013). The installed app carries the
; project licence and all bundled third-party notices locally.
Source: "LICENSE";                DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; \
    Comment: "Launch FTHR Clips game capture"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{group}\Licences and Third-Party Notices"; Filename: "{app}\THIRD_PARTY_NOTICES.md"
Name: "{commondesktop}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon; Comment: "Launch FTHR Clips game capture"

[Run]
; VC++ Runtime: only run when the installed 2015–2022 x64 runtime is missing
; or older than the Microsoft redistributable bundled with this installer.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/quiet /norestart"; \
    StatusMsg: "Installing Visual C++ Runtime..."; Flags: waituntilterminated runhidden; \
    Check: NeedsVCRedist

[UninstallDelete]
; Optional packages are activated outside Program Files only after in-app
; consent. Remove those executables on uninstall; uploader preferences remain
; governed by the existing settings-cleanup prompt below.
Type: filesandordirs; Name: "{localappdata}\FTHR Clips\plugins"
; Clips, screenshots, exports, and sidecar manifests are deliberately outside
; the installer tree at %USERPROFILE%\FTHR_Clips and are never an uninstall
; target. The app-owned settings/cache directory is opt-in only.
Type: filesandordirs; Name: "{code:UserProfilePath}\.fthr"; Check: ShouldRemoveSettingsAndCache

[Code]
const
  ProductUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1';
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\FTHR Clips';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValueName = 'FTHRClips';
  LegacyInstallDir = '{localappdata}\Programs\FTHR Clips';
  LegacyProgramsDir = '{userprograms}\FTHR Clips';
  FTHRMutex = 'Local\FTHR_Clips_SingleInstance_v1';

var
  InstalledVersion: String;
  PreserveAutostart: Boolean;
  RemoveSettingsAndCache: Boolean;
  LegacyCleanupPage: TInputOptionWizardPage;
  ConsentPage: TWizardPage;
  LocationPage: TWizardPage;
  FinishPage: TWizardPage;
  ConsentCheck: TNewCheckBox;
  ConsentError: TNewStaticText;
  InstallDirEdit: TNewEdit;
  ClipDirEdit: TNewEdit;
  LocationError: TNewStaticText;
  FinishPathLabel: TNewStaticText;
  InstallerBackground: TColor;
  InstallerSurface: TColor;
  InstallerHeader: TColor;
  InstallerText: TColor;
  InstallerTextDim: TColor;
  InstallerAccent: TColor;
  InstallerBorder: TColor;
  FooterPanel: TPanel;
  TitleBarPanel: TPanel;
  NavBackBorder: TPanel;
  NavBackFill: TPanel;
  NavBackLabel: TNewStaticText;
  NavNextBorder: TPanel;
  NavNextFill: TPanel;
  NavNextLabel: TNewStaticText;
  InstallingTitle: TNewStaticText;
  InstallingCopy: TNewStaticText;

function AddFontResourceEx(FileName: String; Flags: Cardinal; Reserved: Integer): Integer;
  external 'AddFontResourceExW@gdi32.dll stdcall';

function SendMessage(hWnd: HWND; Msg: LongWord; wParam: Longint;
  lParam: Longint): Longint;
  external 'SendMessageW@user32.dll stdcall';

procedure LoadInstallerAssets;
begin
  try
    ExtractTemporaryFile('Oswald-Bold.ttf');
    AddFontResourceEx(ExpandConstant('{tmp}\Oswald-Bold.ttf'), $10, 0);
    ExtractTemporaryFile('favicon.ico');
    ExtractTemporaryFile('close.png');
  except
    Log('One or more installer presentation assets could not be loaded.');
  end;
end;

procedure ApplyTextStyle(Control: TNewStaticText; Size: Integer; Color: TColor;
  Bold: Boolean);
begin
  Control.StyleElements := Control.StyleElements - [seFont];
  Control.Font.Name := 'Oswald';
  Control.Font.Size := Size;
  Control.Font.Color := Color;
  if Bold then
    Control.Font.Style := [fsBold]
  else
    Control.Font.Style := [];
end;

procedure ApplyDisplayStyle(Control: TNewStaticText; Size: Integer; Color: TColor);
begin
  Control.StyleElements := Control.StyleElements - [seFont];
  Control.Font.Name := 'Oswald';
  Control.Font.Size := Size;
  Control.Font.Color := Color;
  Control.Font.Style := [fsBold];
end;

procedure ApplyCheckStyle(Control: TNewCheckBox; Size: Integer; Color: TColor);
begin
  Control.StyleElements := Control.StyleElements - [seFont];
  Control.Font.Name := 'Oswald';
  Control.Font.Size := Size;
  Control.Font.Color := Color;
end;

procedure StyleEdit(Control: TNewEdit);
begin
  Control.StyleElements := Control.StyleElements - [seFont, seClient];
  Control.Font.Name := 'Oswald';
  Control.Font.Size := 10;
  Control.Font.Color := InstallerText;
  Control.Color := InstallerSurface;
  Control.ParentColor := False;
  Control.BorderStyle := bsSingle;
end;

procedure StyleBoxButton(BorderPanel, FillPanel: TPanel;
  LabelControl: TNewStaticText;
  Primary: Boolean; Enabled: Boolean);
begin
  BorderPanel.Enabled := Enabled;
  FillPanel.Enabled := Enabled;
  LabelControl.Enabled := Enabled;
  if not Enabled then begin
    BorderPanel.Color := InstallerBorder;
    FillPanel.Color := InstallerSurface;
    LabelControl.Font.Color := StrToColor('#555555');
  end else if Primary then begin
    BorderPanel.Color := InstallerAccent;
    FillPanel.Color := InstallerAccent;
    LabelControl.Font.Color := InstallerBackground;
  end else begin
    BorderPanel.Color := InstallerBorder;
    FillPanel.Color := InstallerBackground;
    LabelControl.Font.Color := InstallerText;
  end;
end;

procedure BackPanelClick(Sender: TObject);
begin
  if WizardForm.BackButton.Enabled then
    WizardForm.BackButton.OnClick(WizardForm.BackButton);
end;

procedure NextPanelClick(Sender: TObject);
begin
  if WizardForm.NextButton.Enabled then
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

procedure CloseSetupClick(Sender: TObject);
begin
  if (FinishPage <> nil) and (WizardForm.CurPageID = FinishPage.ID) then
    WizardForm.NextButton.OnClick(WizardForm.NextButton)
  else if WizardForm.CancelButton.Enabled then
    WizardForm.CancelButton.OnClick(WizardForm.CancelButton);
end;

procedure CreateBoxButton(Owner: TComponent; ParentControl: TWinControl;
  var BorderPanel, FillPanel: TPanel; var LabelControl: TNewStaticText;
  Left, Top, Width: Integer; Caption: String; Primary: Boolean);
begin
  BorderPanel := TPanel.Create(Owner);
  BorderPanel.Left := Left;
  BorderPanel.Top := Top;
  BorderPanel.Width := Width;
  BorderPanel.Height := ScaleY(32);
  BorderPanel.BevelOuter := bvNone;
  BorderPanel.ParentBackground := False;
  BorderPanel.StyleElements := [];
  BorderPanel.Cursor := crHand;
  BorderPanel.Parent := ParentControl;

  FillPanel := TPanel.Create(Owner);
  FillPanel.Left := ScaleX(1);
  FillPanel.Top := ScaleY(1);
  FillPanel.Width := BorderPanel.Width - ScaleX(2);
  FillPanel.Height := BorderPanel.Height - ScaleY(2);
  FillPanel.BevelOuter := bvNone;
  FillPanel.ParentBackground := False;
  FillPanel.StyleElements := [];
  FillPanel.Cursor := crHand;
  FillPanel.Parent := BorderPanel;

  LabelControl := TNewStaticText.Create(Owner);
  LabelControl.Left := 0;
  LabelControl.Top := ScaleY(6);
  LabelControl.Width := FillPanel.Width;
  LabelControl.Height := ScaleY(20);
  LabelControl.AutoSize := False;
  LabelControl.Alignment := taCenter;
  LabelControl.Caption := Caption;
  LabelControl.Cursor := crHand;
  ApplyDisplayStyle(LabelControl, 9, InstallerText);
  LabelControl.Parent := FillPanel;
  StyleBoxButton(BorderPanel, FillPanel, LabelControl, Primary, True);
end;

procedure CreateNavigation;
var
  Gap, BackWidth, NextWidth, RightEdge: Integer;
begin
  FooterPanel := TPanel.Create(WizardForm);
  FooterPanel.Left := ScaleX(1);
  FooterPanel.Top := WizardForm.ClientHeight - ScaleY(53);
  FooterPanel.Width := WizardForm.ClientWidth - ScaleX(2);
  FooterPanel.Height := ScaleY(52);
  FooterPanel.BevelOuter := bvNone;
  FooterPanel.Color := InstallerBackground;
  FooterPanel.ParentBackground := False;
  FooterPanel.StyleElements := [];
  FooterPanel.Parent := WizardForm;

  Gap := ScaleX(8);
  BackWidth := ScaleX(82);
  NextWidth := ScaleX(112);
  RightEdge := FooterPanel.Width - ScaleX(15);

  CreateBoxButton(WizardForm, FooterPanel,
    NavNextBorder, NavNextFill, NavNextLabel,
    RightEdge - NextWidth, ScaleY(10), NextWidth, 'NEXT', True);
  NavNextBorder.OnClick := @NextPanelClick;
  NavNextFill.OnClick := @NextPanelClick;
  NavNextLabel.OnClick := @NextPanelClick;

  CreateBoxButton(WizardForm, FooterPanel,
    NavBackBorder, NavBackFill, NavBackLabel,
    RightEdge - NextWidth - Gap - BackWidth, ScaleY(10),
    BackWidth, 'BACK', False);
  NavBackBorder.OnClick := @BackPanelClick;
  NavBackFill.OnClick := @BackPanelClick;
  NavBackLabel.OnClick := @BackPanelClick;

  { Keep the native buttons active for validation and keyboard defaults, while
    the visible controls stay inside the FTHR popup shell. }
  WizardForm.BackButton.Left := -ScaleX(300);
  WizardForm.NextButton.Left := -ScaleX(300);
  WizardForm.CancelButton.Left := -ScaleX(300);
end;

procedure CreateTitleBar;
var
  AppIcon: TBitmapImage;
  TitleLabel: TNewStaticText;
  CloseButton: TBitmapButton;
begin
  TitleBarPanel := TPanel.Create(WizardForm);
  TitleBarPanel.Left := ScaleX(1);
  TitleBarPanel.Top := ScaleY(1);
  TitleBarPanel.Width := WizardForm.ClientWidth - ScaleX(2);
  TitleBarPanel.Height := ScaleY(38);
  TitleBarPanel.BevelOuter := bvNone;
  TitleBarPanel.Color := InstallerHeader;
  TitleBarPanel.ParentBackground := False;
  TitleBarPanel.StyleElements := [];
  TitleBarPanel.Parent := WizardForm;

  AppIcon := TBitmapImage.Create(WizardForm);
  AppIcon.Left := ScaleX(12);
  AppIcon.Top := ScaleY(11);
  AppIcon.Width := ScaleX(16);
  AppIcon.Height := ScaleY(16);
  AppIcon.BackColor := InstallerHeader;
  AppIcon.Parent := TitleBarPanel;
  InitializeBitmapImageFromIcon(AppIcon,
    ExpandConstant('{tmp}\favicon.ico'), InstallerHeader, [16, 24, 32]);

  TitleLabel := TNewStaticText.Create(WizardForm);
  TitleLabel.Left := ScaleX(38);
  TitleLabel.Top := ScaleY(10);
  TitleLabel.Width := TitleBarPanel.Width - ScaleX(82);
  TitleLabel.Height := ScaleY(20);
  TitleLabel.AutoSize := False;
  TitleLabel.Caption := 'FTHR CLIPS SETUP';
  ApplyDisplayStyle(TitleLabel, 9, InstallerText);
  TitleLabel.Parent := TitleBarPanel;

  CloseButton := TBitmapButton.Create(WizardForm);
  CloseButton.Left := TitleBarPanel.Width - ScaleX(34);
  CloseButton.Top := ScaleY(7);
  CloseButton.Width := ScaleX(24);
  CloseButton.Height := ScaleY(24);
  CloseButton.BackColor := InstallerHeader;
  CloseButton.Caption := 'Close setup';
  CloseButton.Hint := 'Close setup';
  CloseButton.ShowHint := True;
  CloseButton.Cursor := crHand;
  CloseButton.Stretch := True;
  CloseButton.PngImage.LoadFromFile(ExpandConstant('{tmp}\close.png'));
  CloseButton.OnClick := @CloseSetupClick;
  CloseButton.Parent := TitleBarPanel;
end;

procedure ApplyPageSurface(Page: TWizardPage);
begin
  Page.Surface.Color := InstallerBackground;
  Page.Surface.ParentBackground := False;
  Page.Surface.StyleElements := Page.Surface.StyleElements - [seClient];
end;

procedure ApplyWizardShell;
var
  OldWidth, OldHeight: Integer;
begin
  OldWidth := WizardForm.Width;
  OldHeight := WizardForm.Height;
  WizardForm.ClientWidth := ScaleX(560);
  WizardForm.ClientHeight := ScaleY(390);
  WizardForm.Left := WizardForm.Left + (OldWidth - WizardForm.Width) div 2;
  WizardForm.Top := WizardForm.Top + (OldHeight - WizardForm.Height) div 2;
  WizardForm.BorderStyle := bsNone;
  WizardForm.Color := InstallerBorder;
  WizardForm.StyleElements := [];
  WizardForm.Font.Name := 'Oswald';
  WizardForm.Font.Color := InstallerText;
  WizardForm.Caption := 'FTHR Clips Setup';

  WizardForm.MainPanel.Visible := False;
  WizardForm.Bevel.Visible := False;
  WizardForm.PageNameLabel.Visible := False;
  WizardForm.PageDescriptionLabel.Visible := False;
  WizardForm.WizardBitmapImage.Visible := False;
  WizardForm.WizardBitmapImage2.Visible := False;
  WizardForm.WizardSmallBitmapImage.Visible := False;

  WizardForm.OuterNotebook.Left := ScaleX(1);
  WizardForm.OuterNotebook.Top := ScaleY(39);
  WizardForm.OuterNotebook.Width := WizardForm.ClientWidth - ScaleX(2);
  WizardForm.OuterNotebook.Height := WizardForm.ClientHeight - ScaleY(92);
  WizardForm.OuterNotebook.StyleElements := [];
  WizardForm.InnerPage.Color := InstallerBackground;
  WizardForm.InnerPage.ParentBackground := False;
  WizardForm.InnerPage.StyleElements := [];
  WizardForm.InnerNotebook.StyleElements := [];
end;

procedure PolicyTextClick(Sender: TObject);
var
  ErrorCode: Integer;
begin
  ShellExecAsOriginalUser('open', 'https://policies.fthrclips.com', '', '',
    SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure ConsentChanged(Sender: TObject);
begin
  if ConsentCheck.Checked then
    ConsentError.Visible := False;
end;

procedure BrowseInstallClick(Sender: TObject);
var
  Directory: String;
begin
  Directory := Trim(InstallDirEdit.Text);
  if BrowseForFolder('Choose where FTHR Clips will be installed',
      Directory, True) then
    InstallDirEdit.Text := Directory;
end;

procedure BrowseClipsClick(Sender: TObject);
var
  Directory: String;
begin
  Directory := Trim(ClipDirEdit.Text);
  if BrowseForFolder('Choose where FTHR Clips will save clips',
      Directory, True) then
    ClipDirEdit.Text := Directory;
end;

procedure CreateConsentPage;
var
  AgreeText, PolicyText: TNewStaticText;
  Title: TNewStaticText;
  Body: TNewStaticText;
begin
  ConsentPage := CreateCustomPage(wpWelcome, '', '');
  ApplyPageSurface(ConsentPage);

  Title := TNewStaticText.Create(ConsentPage);
  Title.Left := ScaleX(24);
  Title.Top := ScaleY(34);
  Title.Width := ConsentPage.SurfaceWidth - ScaleX(48);
  Title.Height := ScaleY(36);
  Title.AutoSize := False;
  Title.Caption := 'INSTALL FTHR CLIPS';
  ApplyDisplayStyle(Title, 22, InstallerText);
  Title.Parent := ConsentPage.Surface;

  Body := TNewStaticText.Create(ConsentPage);
  Body.Left := Title.Left;
  Body.Top := Title.Top + ScaleY(46);
  Body.Width := Title.Width;
  Body.Height := ScaleY(42);
  Body.AutoSize := False;
  Body.WordWrap := True;
  Body.Caption := 'Choose your install locations on the next screen.';
  ApplyTextStyle(Body, 10, InstallerTextDim, False);
  Body.Parent := ConsentPage.Surface;

  ConsentCheck := TNewCheckBox.Create(ConsentPage);
  ConsentCheck.Left := Title.Left;
  ConsentCheck.Top := Body.Top + ScaleY(58);
  ConsentCheck.Width := ScaleX(20);
  ConsentCheck.Height := ScaleY(20);
  ConsentCheck.Caption := '';
  ConsentCheck.Checked := False;
  ConsentCheck.OnClick := @ConsentChanged;
  ApplyCheckStyle(ConsentCheck, 10, InstallerText);
  ConsentCheck.Parent := ConsentPage.Surface;

  AgreeText := TNewStaticText.Create(ConsentPage);
  AgreeText.Left := ConsentCheck.Left + ScaleX(28);
  AgreeText.Top := ConsentCheck.Top + ScaleY(1);
  AgreeText.Caption := 'I agree to the';
  ApplyTextStyle(AgreeText, 10, InstallerText, False);
  AgreeText.Parent := ConsentPage.Surface;

  PolicyText := TNewStaticText.Create(ConsentPage);
  PolicyText.Left := AgreeText.Left + AgreeText.Width + ScaleX(4);
  PolicyText.Top := AgreeText.Top;
  PolicyText.Caption := 'PRIVACY POLICY.';
  PolicyText.Cursor := crHand;
  PolicyText.OnClick := @PolicyTextClick;
  ApplyTextStyle(PolicyText, 10, InstallerAccent, True);
  PolicyText.Font.Style := [fsBold, fsUnderline];
  PolicyText.Parent := ConsentPage.Surface;

  ConsentError := TNewStaticText.Create(ConsentPage);
  ConsentError.Left := ConsentCheck.Left;
  ConsentError.Top := ConsentCheck.Top + ConsentCheck.Height + ScaleY(10);
  ConsentError.Width := ConsentPage.SurfaceWidth - ScaleX(48);
  ConsentError.AutoSize := False;
  ConsentError.Caption := 'Tick the Privacy Policy box to continue.';
  ApplyTextStyle(ConsentError, 10, StrToColor('#CC0000'), False);
  ConsentError.Visible := False;
  ConsentError.Parent := ConsentPage.Surface;
end;

procedure CreateLocationPage;
var
  Title, InstallLabel, ClipLabel, Note: TNewStaticText;
  InstallBrowseBorder, InstallBrowseFill: TPanel;
  InstallBrowseLabel: TNewStaticText;
  ClipBrowseBorder, ClipBrowseFill: TPanel;
  ClipBrowseLabel: TNewStaticText;
  FieldWidth: Integer;
begin
  LocationPage := CreateCustomPage(ConsentPage.ID, '', '');
  ApplyPageSurface(LocationPage);

  Title := TNewStaticText.Create(LocationPage);
  Title.Left := ScaleX(24);
  Title.Top := ScaleY(22);
  Title.Width := LocationPage.SurfaceWidth - ScaleX(48);
  Title.Height := ScaleY(34);
  Title.AutoSize := False;
  Title.Caption := 'CHOOSE LOCATIONS';
  ApplyDisplayStyle(Title, 20, InstallerText);
  Title.Parent := LocationPage.Surface;

  InstallLabel := TNewStaticText.Create(LocationPage);
  InstallLabel.Left := Title.Left;
  InstallLabel.Top := ScaleY(75);
  InstallLabel.Width := Title.Width;
  InstallLabel.Height := ScaleY(20);
  InstallLabel.AutoSize := False;
  InstallLabel.Caption := 'INSTALL FOLDER';
  ApplyDisplayStyle(InstallLabel, 9, InstallerText);
  InstallLabel.Parent := LocationPage.Surface;

  FieldWidth := LocationPage.SurfaceWidth - ScaleX(138);
  InstallDirEdit := WizardForm.DirEdit;
  InstallDirEdit.Parent := LocationPage.Surface;
  InstallDirEdit.Left := Title.Left;
  InstallDirEdit.Top := ScaleY(98);
  InstallDirEdit.Width := FieldWidth;
  InstallDirEdit.Height := ScaleY(32);
  StyleEdit(InstallDirEdit);

  CreateBoxButton(LocationPage, LocationPage.Surface,
    InstallBrowseBorder, InstallBrowseFill, InstallBrowseLabel,
    InstallDirEdit.Left + InstallDirEdit.Width + ScaleX(8),
    InstallDirEdit.Top, ScaleX(82), 'BROWSE', False);
  InstallBrowseBorder.OnClick := @BrowseInstallClick;
  InstallBrowseFill.OnClick := @BrowseInstallClick;
  InstallBrowseLabel.OnClick := @BrowseInstallClick;

  ClipLabel := TNewStaticText.Create(LocationPage);
  ClipLabel.Left := Title.Left;
  ClipLabel.Top := ScaleY(151);
  ClipLabel.Width := Title.Width;
  ClipLabel.Height := ScaleY(20);
  ClipLabel.AutoSize := False;
  ClipLabel.Caption := 'CLIP FOLDER';
  ApplyDisplayStyle(ClipLabel, 9, InstallerText);
  ClipLabel.Parent := LocationPage.Surface;

  ClipDirEdit := TNewEdit.Create(LocationPage);
  ClipDirEdit.Left := Title.Left;
  ClipDirEdit.Top := ScaleY(174);
  ClipDirEdit.Width := FieldWidth;
  ClipDirEdit.Height := ScaleY(32);
  StyleEdit(ClipDirEdit);
  ClipDirEdit.Parent := LocationPage.Surface;

  CreateBoxButton(LocationPage, LocationPage.Surface,
    ClipBrowseBorder, ClipBrowseFill, ClipBrowseLabel,
    ClipDirEdit.Left + ClipDirEdit.Width + ScaleX(8),
    ClipDirEdit.Top, ScaleX(82), 'BROWSE', False);
  ClipBrowseBorder.OnClick := @BrowseClipsClick;
  ClipBrowseFill.OnClick := @BrowseClipsClick;
  ClipBrowseLabel.OnClick := @BrowseClipsClick;

  Note := TNewStaticText.Create(LocationPage);
  Note.Left := Title.Left;
  Note.Top := ScaleY(218);
  Note.Width := Title.Width;
  Note.Height := ScaleY(22);
  Note.AutoSize := False;
  Note.Caption := 'Clips, recordings, screenshots, and exports use the clip folder.';
  ApplyTextStyle(Note, 9, InstallerTextDim, False);
  Note.Parent := LocationPage.Surface;

  LocationError := TNewStaticText.Create(LocationPage);
  LocationError.Left := Title.Left;
  LocationError.Top := ScaleY(246);
  LocationError.Width := Title.Width;
  LocationError.Height := ScaleY(20);
  LocationError.AutoSize := False;
  LocationError.Caption := 'Choose both folders to continue.';
  ApplyTextStyle(LocationError, 9, StrToColor('#CC0000'), False);
  LocationError.Visible := False;
  LocationError.Parent := LocationPage.Surface;
end;

function JsonEscape(const Value: String): String;
var
  Escaped: String;
begin
  Escaped := Value;
  StringChangeEx(Escaped, '\\', '\\\\', True);
  StringChangeEx(Escaped, '"', '\\"', True);
  Result := Escaped;
end;

function JsonStringValue(const Json, Key: String): String;
var
  Token: String;
  Remainder: String;
  StartPos, ColonPos, ValueStart, ValueEnd: Integer;
  Escaped: Boolean;
begin
  Result := '';
  Token := '"' + Key + '"';
  StartPos := Pos(Token, Json);
  if StartPos = 0 then
    exit;
  ColonPos := StartPos + Length(Token);
  while (ColonPos <= Length(Json)) and (Json[ColonPos] <> ':') do
    Inc(ColonPos);
  if ColonPos > Length(Json) then
    exit;
  ValueStart := ColonPos + 1;
  while (ValueStart <= Length(Json)) and (Json[ValueStart] <= ' ') do
    Inc(ValueStart);
  if (ValueStart > Length(Json)) or (Json[ValueStart] <> '"') then
    exit;
  Inc(ValueStart);
  ValueEnd := ValueStart;
  Escaped := False;
  while ValueEnd <= Length(Json) do begin
    if (Json[ValueEnd] = '"') and not Escaped then
      break;
    if (Json[ValueEnd] = '\\') then
      Escaped := not Escaped
    else
      Escaped := False;
    Inc(ValueEnd);
  end;
  if ValueEnd > Length(Json) then
    exit;
  Remainder := Copy(Json, ValueStart, ValueEnd - ValueStart);
  StringChangeEx(Remainder, '\\\\', '\\', True);
  StringChangeEx(Remainder, '\\"', '"', True);
  Result := Remainder;
end;

function UpsertJsonString(const Json, Key, Value: String): String;
var
  Token: String;
  Encoded, Remainder: String;
  StartPos, ColonPos, ValueStart, ValueEnd, I: Integer;
  Escaped: Boolean;
begin
  Token := '"' + Key + '"';
  Encoded := '"' + JsonEscape(Value) + '"';
  StartPos := Pos(Token, Json);
  if StartPos <> 0 then begin
    ColonPos := StartPos + Length(Token);
    while (ColonPos <= Length(Json)) and (Json[ColonPos] <> ':') do
      Inc(ColonPos);
    ValueStart := ColonPos + 1;
    while (ValueStart <= Length(Json)) and (Json[ValueStart] <= ' ') do
      Inc(ValueStart);
    if (ValueStart <= Length(Json)) and (Json[ValueStart] = '"') then begin
      ValueEnd := ValueStart + 1;
      Escaped := False;
      while ValueEnd <= Length(Json) do begin
        if (Json[ValueEnd] = '"') and not Escaped then
          break;
        if (Json[ValueEnd] = '\\') then
          Escaped := not Escaped
        else
          Escaped := False;
        Inc(ValueEnd);
      end;
      if ValueEnd <= Length(Json) then begin
        Result := Copy(Json, 1, ValueStart - 1) + Encoded +
          Copy(Json, ValueEnd + 1, Length(Json));
        exit;
      end;
    end;
  end;

  Result := Json;
  I := Length(Result);
  while (I > 0) and (Result[I] <> '}') do
    Dec(I);
  if I = 0 then
    exit;
  Remainder := Copy(Json, 1, I - 1);
  while (Length(Remainder) > 0) and
        (Remainder[Length(Remainder)] <= ' ') do
    Delete(Remainder, Length(Remainder), 1);
  Result := Remainder;
  if (Length(Result) > 0) and (Result[Length(Result)] <> '{') then
    Result := Result + ',';
  Result := Result + #13#10 + '  ' + Token + ': ' + Encoded + #13#10 +
    Copy(Json, I, Length(Json));
end;

function UserProfileDirectory: String;
begin
  { Derive the profile root from the standard roaming AppData location. }
  Result := ExtractFileDir(ExtractFileDir(ExpandConstant('{userappdata}')));
end;

function SettingsFilePath: String;
begin
  Result := AddBackslash(UserProfileDirectory()) + '.fthr\settings.json';
end;

function ReadSettingsJson(const FileName: String; var Json: String): Boolean;
var
  Lines: TStringList;
begin
  Result := False;
  Json := '';
  if not FileExists(FileName) then
    exit;
  Lines := TStringList.Create;
  try
    try
      Lines.LoadFromFile(FileName);
      Json := Lines.Text;
      Result := True;
    except
      Log('Could not read existing FTHR settings from ' + FileName);
    end;
  finally
    Lines.Free;
  end;
end;

function SaveSettingsJson(const FileName, Json: String): Boolean;
var
  Lines: TStringList;
begin
  Result := False;
  Lines := TStringList.Create;
  try
    try
      Lines.Text := Json;
      Lines.SaveToFile(FileName);
      Result := True;
    except
      Log('Could not write FTHR settings to ' + FileName);
    end;
  finally
    Lines.Free;
  end;
end;

function ExistingClipDirectory: String;
var
  Json: String;
begin
  Result := '';
  if ReadSettingsJson(SettingsFilePath(), Json) then
    Result := JsonStringValue(Json, 'clips_directory');
end;

procedure WriteClipSettings;
var
  SettingsPath, SettingsDir, ExistingClip, ExistingRecording: String;
  Json: String;
  NewClip, NewRecording: String;
begin
  NewClip := Trim(ClipDirEdit.Text);
  if NewClip = '' then
    exit;
  NewRecording := AddBackslash(NewClip) + 'Recordings';
  SettingsPath := SettingsFilePath;
  SettingsDir := ExtractFileDir(SettingsPath);
  ForceDirectories(SettingsDir);

  if (not ReadSettingsJson(SettingsPath, Json)) or (Trim(Json) = '') then begin
    Json := '{' + #13#10 +
      '  "clips_directory": "' + JsonEscape(NewClip) + '",' + #13#10 +
      '  "recording_directory": "' + JsonEscape(NewRecording) + '"' + #13#10 +
      '}';
  end else begin
    ExistingClip := JsonStringValue(Json, 'clips_directory');
    ExistingRecording := JsonStringValue(Json, 'recording_directory');
    Json := UpsertJsonString(Json, 'clips_directory', NewClip);
    if (ExistingRecording = '') or
       SameText(ExistingRecording, AddBackslash(ExistingClip) + 'Recordings') then
      Json := UpsertJsonString(Json, 'recording_directory', NewRecording);
  end;
  if not SaveSettingsJson(SettingsPath, Json) then
    Log('Could not write the selected clips directory to ' + SettingsPath);
end;

procedure CreateFinishPage;
var
  Title: TNewStaticText;
  Body: TNewStaticText;
begin
  FinishPage := CreateCustomPage(wpInstalling, '', '');
  ApplyPageSurface(FinishPage);

  Title := TNewStaticText.Create(FinishPage);
  Title.Left := ScaleX(24);
  Title.Top := ScaleY(58);
  Title.Width := FinishPage.SurfaceWidth - ScaleX(48);
  Title.Height := ScaleY(36);
  Title.AutoSize := False;
  Title.Caption := 'FTHR CLIPS IS READY';
  ApplyDisplayStyle(Title, 22, InstallerText);
  Title.Parent := FinishPage.Surface;

  Body := TNewStaticText.Create(FinishPage);
  Body.Left := Title.Left;
  Body.Top := Title.Top + ScaleY(48);
  Body.Width := Title.Width;
  Body.AutoSize := False;
  Body.WordWrap := True;
  Body.Caption := 'The app is installed.' + #13#10 + #13#10 +
    'Clips will be saved to ' + Trim(ClipDirEdit.Text);
  ApplyTextStyle(Body, 10, InstallerTextDim, False);
  Body.AdjustHeight;
  Body.Parent := FinishPage.Surface;
  FinishPathLabel := Body;
end;

procedure CreateInstallingSurface;
begin
  WizardForm.InstallingPage.Color := InstallerBackground;
  WizardForm.InstallingPage.StyleElements := [];

  InstallingTitle := TNewStaticText.Create(WizardForm);
  InstallingTitle.Left := ScaleX(24);
  InstallingTitle.Top := ScaleY(58);
  InstallingTitle.Width := WizardForm.InstallingPage.Width - ScaleX(48);
  InstallingTitle.Height := ScaleY(36);
  InstallingTitle.AutoSize := False;
  InstallingTitle.Caption := 'INSTALLING FTHR CLIPS';
  ApplyDisplayStyle(InstallingTitle, 22, InstallerText);
  InstallingTitle.Parent := WizardForm.InstallingPage;

  InstallingCopy := TNewStaticText.Create(WizardForm);
  InstallingCopy.Left := InstallingTitle.Left;
  InstallingCopy.Top := InstallingTitle.Top + ScaleY(46);
  InstallingCopy.Width := InstallingTitle.Width;
  InstallingCopy.Height := ScaleY(24);
  InstallingCopy.AutoSize := False;
  InstallingCopy.Caption := 'This only takes a moment.';
  ApplyTextStyle(InstallingCopy, 10, InstallerTextDim, False);
  InstallingCopy.Parent := WizardForm.InstallingPage;

  WizardForm.ProgressGauge.Left := InstallingTitle.Left;
  WizardForm.ProgressGauge.Top := ScaleY(142);
  WizardForm.ProgressGauge.Width := InstallingTitle.Width;
  WizardForm.ProgressGauge.Height := ScaleY(12);
  WizardForm.ProgressGauge.StyleElements := [];
  SendMessage(WizardForm.ProgressGauge.Handle, $0409, 0, InstallerAccent);

  WizardForm.StatusLabel.Left := InstallingTitle.Left;
  WizardForm.StatusLabel.Top := ScaleY(170);
  WizardForm.StatusLabel.Width := InstallingTitle.Width;
  WizardForm.StatusLabel.Height := ScaleY(22);
  WizardForm.StatusLabel.AutoSize := False;
  ApplyTextStyle(WizardForm.StatusLabel, 9, InstallerTextDim, False);
  WizardForm.FilenameLabel.Visible := False;
end;

function NextVersionNumber(const Value: String; var Position: Integer): Integer;
var
  Digits: String;
begin
  Digits := '';
  while (Position <= Length(Value)) and
        ((Value[Position] < '0') or (Value[Position] > '9')) do
    Position := Position + 1;
  while (Position <= Length(Value)) and
        (Value[Position] >= '0') and (Value[Position] <= '9') do begin
    Digits := Digits + Value[Position];
    Position := Position + 1;
  end;
  Result := StrToIntDef(Digits, 0);
end;

function CompareDottedVersion(const Left, Right: String): Integer;
var
  Part, LeftPos, RightPos, LeftValue, RightValue: Integer;
begin
  LeftPos := 1;
  RightPos := 1;
  for Part := 1 to 4 do begin
    LeftValue := NextVersionNumber(Left, LeftPos);
    RightValue := NextVersionNumber(Right, RightPos);
    if LeftValue < RightValue then begin
      Result := -1;
      exit;
    end;
    if LeftValue > RightValue then begin
      Result := 1;
      exit;
    end;
  end;
  Result := 0;
end;

function ReadInstalledVersion: String;
begin
  Result := '';
  if not RegQueryStringValue(HKLM64, ProductUninstallKey, 'DisplayVersion', Result) then
    RegQueryStringValue(HKCU, ProductUninstallKey, 'DisplayVersion', Result);
end;

function IsDowngradeAllowed: Boolean;
begin
  Result := Pos('/ALLOWDOWNGRADE', Uppercase(GetCmdTail)) > 0;
end;

function HasOwnedAutostart: Boolean;
var
  Command: String;
begin
  Result := False;
  if RegQueryStringValue(HKCU, RunKey, RunValueName, Command) then begin
    Command := Lowercase(Command);
    Result := (Pos('fthrclips.exe', Command) > 0) and
              (Pos('--background', Command) > 0);
  end;
end;

function LegacyInstallPresent: Boolean;
var
  LegacyLocation: String;
begin
  Result := RegQueryStringValue(HKCU, LegacyUninstallKey, 'InstallLocation', LegacyLocation) and
            SameText(LegacyLocation, ExpandConstant(LegacyInstallDir));
end;

function LegacyCleanupRequested: Boolean;
begin
  Result := Pos('/REMOVELEGACY', Uppercase(GetCmdTail)) > 0;
  if LegacyCleanupPage <> nil then
    Result := Result or LegacyCleanupPage.Values[0];
end;

function NeedsVCRedist: Boolean;
var
  Installed: Cardinal;
  InstalledVersion: String;
begin
  Result := True;
  if not RegQueryDWordValue(HKLM64,
      'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Installed', Installed) then
    exit;
  if Installed <> 1 then
    exit;
  if not RegQueryStringValue(HKLM64,
      'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Version', InstalledVersion) then
    exit;
  Result := CompareDottedVersion(InstalledVersion, '{#VCRedistVersion}') < 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if (InstalledVersion <> '') and
     (CompareDottedVersion(InstalledVersion, '{#MyAppVersion}') > 0) and
     not IsDowngradeAllowed then begin
    Result := 'A newer FTHR Clips version (' + InstalledVersion +
      ') is already installed. This installer will not downgrade it. ' +
      'Use a matching or newer installer, or explicitly pass /ALLOWDOWNGRADE.';
  end;
end;

procedure InitializeWizard;
begin
  InstallerBackground := StrToColor('#000000');
  InstallerSurface := StrToColor('#0A0A0A');
  InstallerHeader := StrToColor('#111111');
  InstallerText := StrToColor('#FFFFFF');
  InstallerTextDim := StrToColor('#888888');
  InstallerAccent := StrToColor('#00FFAA');
  InstallerBorder := StrToColor('#222222');
  LoadInstallerAssets;
  ApplyWizardShell;
  CreateConsentPage;
  CreateLocationPage;
  ClipDirEdit.Text := ExistingClipDirectory;
  if ClipDirEdit.Text = '' then
    ClipDirEdit.Text := GetPreviousData(
      'ClipsDirectory', AddBackslash(UserProfileDirectory()) + 'FTHR_Clips');

  if LegacyInstallPresent then begin
    LegacyCleanupPage := CreateInputOptionPage(LocationPage.ID,
      'Older FTHR installation found',
      'An older per-user FTHR test installation is still registered.',
      'Select this only if you want to remove that obsolete installation. ' +
      'It removes its exact install folder, registry entry, and its two known ' +
      'per-user Start Menu shortcuts. It never removes clips or current settings.',
      False, False);
    LegacyCleanupPage.Add('Remove the obsolete per-user FTHR test installation');
    LegacyCleanupPage.Values[0] := False;
    ApplyPageSurface(LegacyCleanupPage);
    LegacyCleanupPage.CheckListBox.Font.Name := 'Oswald';
    LegacyCleanupPage.CheckListBox.Font.Color := InstallerText;
    LegacyCleanupPage.CheckListBox.Color := InstallerBackground;
    LegacyCleanupPage.CheckListBox.ParentColor := False;
  end;

  CreateFinishPage;
  CreateInstallingSurface;
  CreateTitleBar;
  CreateNavigation;
end;

procedure RemoveLegacyInstall;
var
  LegacyDir, ProgramsDir: String;
begin
  if not LegacyCleanupRequested then
    exit;
  if CheckForMutexes(FTHRMutex) then begin
    Log('Legacy cleanup skipped: FTHR Clips is still running.');
    if not WizardSilent then
      MsgBox('The old FTHR installation is still running, so it was left in place. ' +
        'Close FTHR Clips and run this installer again to remove it.',
        mbInformation, MB_OK);
    exit;
  end;
  if not LegacyInstallPresent then begin
    Log('Legacy cleanup skipped: expected legacy registry record/path no longer matched.');
    exit;
  end;
  LegacyDir := ExpandConstant(LegacyInstallDir);
  ProgramsDir := ExpandConstant(LegacyProgramsDir);
  if DelTree(LegacyDir, True, True, True) then begin
    RegDeleteKeyIncludingSubkeys(HKCU, LegacyUninstallKey);
    DeleteFile(AddBackslash(ProgramsDir) + 'FTHR Clips.lnk');
    DeleteFile(AddBackslash(ProgramsDir) + 'Uninstall FTHR Clips.lnk');
    RemoveDir(ProgramsDir);
    Log('Removed the selected legacy per-user FTHR installation.');
  end else begin
    Log('Legacy cleanup failed without removing its registry record.');
    if not WizardSilent then
      MsgBox('The old FTHR installation could not be removed and was left registered. ' +
        'No clips or settings were touched.', mbError, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    WriteClipSettings;
    if PreserveAutostart then begin
      RegWriteStringValue(HKCU, RunKey, RunValueName,
        '"' + ExpandConstant('{app}\{#MyAppExeName}') + '" --background');
      Log('Preserved FTHR Clips autostart with the current install path.');
    end;
    RemoveLegacyInstall;
  end;
end;

function InitializeSetup: Boolean;
begin
  InstalledVersion := ReadInstalledVersion;
  PreserveAutostart := HasOwnedAutostart;
  RemoveSettingsAndCache := False;
  Result := True;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  if ClipDirEdit <> nil then
    SetPreviousData(PreviousDataKey, 'ClipsDirectory', Trim(ClipDirEdit.Text));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConsentPage.ID then begin
    if not ConsentCheck.Checked then begin
      ConsentError.Visible := True;
      WizardForm.ActiveControl := ConsentCheck;
      Result := False;
    end;
  end else if CurPageID = LocationPage.ID then begin
    if (Trim(InstallDirEdit.Text) = '') or (Trim(ClipDirEdit.Text) = '') then begin
      LocationError.Visible := True;
      if Trim(InstallDirEdit.Text) = '' then
        WizardForm.ActiveControl := InstallDirEdit
      else
        WizardForm.ActiveControl := ClipDirEdit;
      Result := False;
    end else begin
      LocationError.Visible := False;
      WizardForm.DirEdit.Text := Trim(InstallDirEdit.Text);
      ClipDirEdit.Text := Trim(ClipDirEdit.Text);
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  NavBackBorder.Visible := False;
  NavNextBorder.Visible := False;

  if CurPageID = ConsentPage.ID then begin
    WizardForm.BackButton.Enabled := False;
    NavNextLabel.Caption := 'NEXT';
    NavNextBorder.Visible := True;
    WizardForm.ActiveControl := ConsentCheck;
  end else if CurPageID = LocationPage.ID then begin
    WizardForm.BackButton.Enabled := True;
    NavBackBorder.Visible := True;
    NavNextBorder.Visible := True;
    NavNextLabel.Caption := 'INSTALL';
    WizardForm.ActiveControl := InstallDirEdit;
  end else if CurPageID = FinishPage.ID then begin
    FinishPathLabel.Caption := 'The app is installed.' + #13#10 + #13#10 +
      'Clips will be saved to ' + Trim(ClipDirEdit.Text);
    FinishPathLabel.AdjustHeight;
    WizardForm.BackButton.Enabled := False;
    WizardForm.CancelButton.Enabled := False;
    NavNextLabel.Caption := 'CLOSE';
    NavNextBorder.Visible := True;
  end else if CurPageID = wpInstalling then begin
    WizardForm.BackButton.Enabled := False;
  end else begin
    WizardForm.BackButton.Enabled := True;
    NavBackBorder.Visible := True;
    NavNextBorder.Visible := True;
    NavNextLabel.Caption := 'NEXT';
  end;

  StyleBoxButton(NavBackBorder, NavBackFill, NavBackLabel, False,
    WizardForm.BackButton.Enabled);
  StyleBoxButton(NavNextBorder, NavNextFill, NavNextLabel, True,
    WizardForm.NextButton.Enabled);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := PageID = wpSelectTasks;
end;

function ShouldRemoveSettingsAndCache: Boolean;
begin
  Result := RemoveSettingsAndCache;
end;

function UserProfilePath(Param: String): String;
begin
  Result := GetEnv('USERPROFILE');
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  RemoveSettingsAndCache := False;
  if not UninstallSilent then
    RemoveSettingsAndCache :=
      SuppressibleMsgBox(
        'Keep your clips, screenshots, exports, and sidecar files?' + #13#10 + #13#10 +
        'Choose Yes only to also remove FTHR Clips settings, logs, hotkeys, ' +
        'themes, and cache from %USERPROFILE%\\.fthr. Your clip folder is ' +
        'never removed by this installer.',
        mbConfirmation, MB_YESNO, IDNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    RegDeleteValue(HKCU, RunKey, RunValueName);
    Log('Removed the FTHR Clips per-user autostart value.');
  end;
end;
