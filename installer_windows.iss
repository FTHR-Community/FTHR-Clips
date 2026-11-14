; FTHR Clips — Windows Installer (Inno Setup 6)
; Bundles the native Windows capture engine.
; Build: iscc installer_windows.iss
; Prerequisite: redist\vc_redist.x64.exe — https://aka.ms/vs/17/release/vc_redist.x64.exe

#define MyAppName      "FTHR Clips"
#define MyAppVersion   "2.1.0"
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
VersionInfoProductVersion=2.1.0.0
VersionInfoTextVersion={#MyAppVersion}
VersionInfoVersion=2.1.0.0

; Compression — maximum, solid
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=2

; Guided setup: neutral charcoal surfaces, left steps, mint primary actions.
WizardStyle=modern dark hidebevels
WizardSizePercent=140
DisableWelcomePage=yes
DisableFinishedPage=no
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
WizardBackColor=#242629

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
english.FinishedLabel=FTHR Clips is ready. Choose Finish to complete setup.
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
Source: "installer_assets\polished\close-current.png"; Flags: dontcopy noencryption
Source: "installer_assets\polished\step-*.png"; Flags: dontcopy noencryption
; Licence paperwork (AUDIT-005/AUDIT-013). The installed app carries the
; project licence and all bundled third-party notices locally.
Source: "LICENSE";                DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\uninstall_cleanup.ps1"; DestDir: "{app}"; Flags: ignoreversion
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

; Inno owns the checked-by-default completion option and launches it only
; after successful interactive setup when Finish is chosen. A pending restart
; suppresses postinstall actions automatically; preserve that native behavior.
Filename: "{app}\{#MyAppExeName}"; Description: "Open FTHR Clips"; \
    WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Messages]
ConfirmUninstall=Remove %1 and its settings, logs, caches, and installed FTHR plugins?%n%nFTHR Clips will close first. Clips, recordings, screenshots, and exports will be kept.

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
  LegacyCleanupPage: TInputOptionWizardPage;
  ConsentPage: TWizardPage;
  LocationPage: TWizardPage;
#ifdef PreviewBuild
  PreviewInstallingPage: TWizardPage;
  PreviewFinishedPage: TWizardPage;
#endif
  SidebarPanel: TPanel;
  StepRows: array[1..3] of TPanel;
  StepLabels: array[1..3] of TNewStaticText;
  ConsentCheck: TNewCheckBox;
  DesktopShortcutCheck: TNewCheckBox;
  StepImages: array[1..3] of TBitmapImage;
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
  ProgressTrack: TPanel;
  ProgressFill: TPanel;
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
    ExtractTemporaryFile('close-current.png');
    ExtractTemporaryFiles('{tmp}\step-*.png');
  except
    Log('One or more installer presentation assets could not be loaded.');
  end;
end;

procedure ApplyTextStyle(Control: TNewStaticText; Size: Integer; Color: TColor;
  Bold: Boolean);
begin
  Control.StyleElements := Control.StyleElements - [seFont];
  Control.Font.Name := 'Segoe UI';
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
  Control.Font.Name := 'Segoe UI';
  Control.Font.Size := Size;
  Control.Font.Color := Color;
end;

procedure StyleEdit(Control: TNewEdit);
begin
  Control.StyleElements := Control.StyleElements - [seFont, seClient];
  Control.Font.Name := 'Segoe UI';
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
  // With applications in use the left button is CANCEL, not BACK.
  if (WizardForm.CurPageID = wpPreparing) and WizardForm.NextButton.Enabled then
    WizardForm.CancelButton.OnClick(WizardForm.CancelButton)
  else if WizardForm.BackButton.Enabled then
    WizardForm.BackButton.OnClick(WizardForm.BackButton);
end;

procedure NextPanelClick(Sender: TObject);
begin
  if (WizardForm.CurPageID = wpPreparing) and
     not WizardForm.NextButton.Enabled then
    WizardForm.CancelButton.OnClick(WizardForm.CancelButton)
  else if WizardForm.NextButton.Enabled then
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
end;

procedure CloseSetupClick(Sender: TObject);
begin
  if WizardForm.CurPageID = wpFinished then begin
    { Closing the window is not an instruction to open the app. }
    if WizardForm.RunList.Items.Count > 0 then
      WizardForm.RunList.Checked[0] := False;
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
  end
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
  BorderPanel.Height := ScaleY(38);
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
  LabelControl.Top := ScaleY(5);
  LabelControl.Width := FillPanel.Width;
  LabelControl.Height := ScaleY(28);
  LabelControl.AutoSize := False;
  LabelControl.Alignment := taCenter;
  LabelControl.Caption := Caption;
  LabelControl.Cursor := crHand;
  ApplyDisplayStyle(LabelControl, 11, InstallerText);
  LabelControl.Parent := FillPanel;
  StyleBoxButton(BorderPanel, FillPanel, LabelControl, Primary, True);
end;

procedure CreateNavigation;
var
  BackWidth, NextWidth, RightEdge: Integer;
begin
  FooterPanel := TPanel.Create(WizardForm);
  FooterPanel.Left := ScaleX(201);
  FooterPanel.Top := WizardForm.ClientHeight - ScaleY(68);
  FooterPanel.Width := WizardForm.ClientWidth - ScaleX(202);
  FooterPanel.Height := ScaleY(67);
  FooterPanel.BevelOuter := bvNone;
  FooterPanel.Color := InstallerBackground;
  FooterPanel.ParentBackground := False;
  FooterPanel.StyleElements := [];
  FooterPanel.Parent := WizardForm;

  BackWidth := ScaleX(100);
  NextWidth := ScaleX(130);
  RightEdge := FooterPanel.Width - ScaleX(36);

  CreateBoxButton(WizardForm, FooterPanel,
    NavNextBorder, NavNextFill, NavNextLabel,
    RightEdge - NextWidth, ScaleY(10), NextWidth, 'NEXT', True);
  NavNextBorder.OnClick := @NextPanelClick;
  NavNextFill.OnClick := @NextPanelClick;
  NavNextLabel.OnClick := @NextPanelClick;
  CreateBoxButton(WizardForm, FooterPanel,
    NavBackBorder, NavBackFill, NavBackLabel,
    ScaleX(36), ScaleY(10), BackWidth, 'BACK', False);
  NavBackBorder.OnClick := @BackPanelClick;
  NavBackFill.OnClick := @BackPanelClick;
  NavBackLabel.OnClick := @BackPanelClick;
  WizardForm.BackButton.Left := -ScaleX(300);
  WizardForm.NextButton.Left := -ScaleX(300);
  WizardForm.CancelButton.Left := -ScaleX(300);
end;

procedure UpdateSteps(CurrentStep: Integer);
var
  I, Size: Integer;
  State: String;
begin
  Size := ScaleX(24);
  if Size <= 24 then Size := 24
  else if Size <= 30 then Size := 30
  else if Size <= 36 then Size := 36
  else if Size <= 48 then Size := 48
  else Size := 72;
  for I := 1 to 3 do begin
    if I = CurrentStep then State := 'active'
    else if I < CurrentStep then State := 'done'
    else State := 'pending';
    StepImages[I].PngImage.LoadFromFile(ExpandConstant('{tmp}\step-') +
      IntToStr(I) + '-' + State + '-' + IntToStr(Size) + '.png');
    if I = CurrentStep then begin
      StepRows[I].Color := InstallerSurface;
      StepLabels[I].Font.Color := InstallerText;
    end else begin
      StepRows[I].Color := InstallerHeader;
      if I < CurrentStep then StepLabels[I].Font.Color := InstallerText
      else StepLabels[I].Font.Color := InstallerTextDim;
    end;
  end;
end;

procedure CreateSteps;
var
  I: Integer;
  Brand, VersionLabel: TNewStaticText;
begin
  SidebarPanel := TPanel.Create(WizardForm);
  SidebarPanel.SetBounds(ScaleX(1), ScaleY(49), ScaleX(199),
    WizardForm.ClientHeight - ScaleY(50));
  SidebarPanel.BevelOuter := bvNone;
  SidebarPanel.Color := InstallerHeader;
  SidebarPanel.ParentBackground := False;
  SidebarPanel.StyleElements := [];
  SidebarPanel.Parent := WizardForm;

  Brand := TNewStaticText.Create(WizardForm);
  Brand.SetBounds(ScaleX(28), ScaleY(30), ScaleX(150), ScaleY(40));
  Brand.AutoSize := False;
  Brand.Caption := 'FTHR Clips';
  ApplyDisplayStyle(Brand, 20, InstallerText);
  Brand.Parent := SidebarPanel;

  for I := 1 to 3 do begin
    StepRows[I] := TPanel.Create(WizardForm);
    StepRows[I].SetBounds(ScaleX(12), ScaleY(106 + (I - 1) * 58),
      ScaleX(175), ScaleY(46));
    StepRows[I].BevelOuter := bvNone;
    StepRows[I].ParentBackground := False;
    StepRows[I].StyleElements := [];
    StepRows[I].Parent := SidebarPanel;
    StepImages[I] := TBitmapImage.Create(WizardForm);
    StepImages[I].Parent := StepRows[I];
    StepImages[I].SetBounds(ScaleX(14), ScaleY(11), ScaleX(24), ScaleX(24));
    StepImages[I].Stretch := True;
    StepLabels[I] := TNewStaticText.Create(WizardForm);
    StepLabels[I].SetBounds(ScaleX(50), ScaleY(11), ScaleX(116), ScaleY(27));
    StepLabels[I].AutoSize := False;
    case I of
      1: StepLabels[I].Caption := 'Privacy';
      2: StepLabels[I].Caption := 'Folders';
      3: StepLabels[I].Caption := 'Install';
    end;
    ApplyTextStyle(StepLabels[I], 11, InstallerText, True);
    StepLabels[I].Parent := StepRows[I];
  end;
  VersionLabel := TNewStaticText.Create(WizardForm);
  VersionLabel.SetBounds(ScaleX(28), SidebarPanel.Height - ScaleY(48),
    ScaleX(150), ScaleY(24));
  VersionLabel.AutoSize := False;
  VersionLabel.Caption := 'Version {#MyAppVersion}';
  ApplyTextStyle(VersionLabel, 9, InstallerTextDim, False);
  VersionLabel.Parent := SidebarPanel;
  UpdateSteps(1);
end;

procedure CreateTitleBar;
var
  TitleLabel: TNewStaticText;
  CloseButton: TBitmapButton;
begin
  TitleBarPanel := TPanel.Create(WizardForm);
  TitleBarPanel.SetBounds(ScaleX(1), ScaleY(1),
    WizardForm.ClientWidth - ScaleX(2), ScaleY(47));
  TitleBarPanel.BevelOuter := bvNone;
  TitleBarPanel.Color := InstallerHeader;
  TitleBarPanel.ParentBackground := False;
  TitleBarPanel.StyleElements := [];
  TitleBarPanel.Parent := WizardForm;
  TitleLabel := TNewStaticText.Create(WizardForm);
  TitleLabel.SetBounds(ScaleX(20), ScaleY(10), ScaleX(260), ScaleY(28));
  TitleLabel.AutoSize := False;
  TitleLabel.Caption := 'FTHR Clips Setup';
  ApplyTextStyle(TitleLabel, 10, InstallerText, True);
  TitleLabel.Parent := TitleBarPanel;
  CloseButton := TBitmapButton.Create(WizardForm);
  CloseButton.SetBounds(TitleBarPanel.Width - ScaleX(45), ScaleY(7),
    ScaleX(32), ScaleX(32));
  CloseButton.BackColor := InstallerHeader;
  CloseButton.Caption := 'Close setup';
  CloseButton.Hint := 'Close setup';
  CloseButton.ShowHint := True;
  CloseButton.Cursor := crHand;
  CloseButton.Stretch := True;
  CloseButton.PngImage.LoadFromFile(ExpandConstant('{tmp}\close-current.png'));
  CloseButton.OnClick := @CloseSetupClick;
  CloseButton.Parent := TitleBarPanel;
end;

procedure ApplyPageSurface(Page: TWizardPage);
begin
  Page.Surface.SetBounds(0, 0, WizardForm.OuterNotebook.Width,
    WizardForm.OuterNotebook.Height);
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
  WizardForm.ClientWidth := ScaleX(860);
  WizardForm.ClientHeight := ScaleY(600);
  WizardForm.Left := WizardForm.Left + (OldWidth - WizardForm.Width) div 2;
  WizardForm.Top := WizardForm.Top + (OldHeight - WizardForm.Height) div 2;
  WizardForm.BorderStyle := bsNone;
  WizardForm.Color := InstallerBorder;
  WizardForm.StyleElements := [];
  WizardForm.Font.Name := 'Segoe UI';
  WizardForm.Font.Size := 10;
  WizardForm.Font.Style := [];
  WizardForm.Font.Color := InstallerText;
  WizardForm.Caption := 'FTHR Clips Setup';

  WizardForm.MainPanel.Visible := False;
  WizardForm.Bevel.Visible := False;
  WizardForm.PageNameLabel.Visible := False;
  WizardForm.PageDescriptionLabel.Visible := False;
  WizardForm.WizardBitmapImage.Visible := False;
  WizardForm.WizardBitmapImage2.Visible := False;
  WizardForm.WizardSmallBitmapImage.Visible := False;

  WizardForm.OuterNotebook.Left := ScaleX(201);
  WizardForm.OuterNotebook.Top := ScaleY(49);
  WizardForm.OuterNotebook.Width := WizardForm.ClientWidth - ScaleX(202);
  WizardForm.OuterNotebook.Height := WizardForm.ClientHeight - ScaleY(117);
  WizardForm.OuterNotebook.StyleElements := [];
  WizardForm.InnerPage.Color := InstallerBackground;
  WizardForm.InnerPage.ParentBackground := False;
  WizardForm.InnerPage.StyleElements := [];
  WizardForm.InnerPage.SetBounds(0, 0,
    WizardForm.OuterNotebook.Width, WizardForm.OuterNotebook.Height);
  WizardForm.InnerNotebook.StyleElements := [];
  WizardForm.InnerNotebook.SetBounds(0, 0,
    WizardForm.OuterNotebook.Width, WizardForm.OuterNotebook.Height);
  WizardForm.InstallingPage.SetBounds(0, 0,
    WizardForm.OuterNotebook.Width, WizardForm.OuterNotebook.Height);
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
  PolicyText: TNewStaticText;
  Title: TNewStaticText;
  Body: TNewStaticText;
begin
  ConsentPage := CreateCustomPage(wpWelcome, '', '');
  ApplyPageSurface(ConsentPage);

  Title := TNewStaticText.Create(ConsentPage);
  Title.Left := ScaleX(36);
  Title.Top := ScaleY(34);
  Title.Width := ConsentPage.SurfaceWidth - ScaleX(72);
  Title.Height := ScaleY(48);
  Title.AutoSize := False;
  Title.Caption := 'Welcome to FTHR Clips';
  ApplyDisplayStyle(Title, 22, InstallerText);
  Title.Parent := ConsentPage.Surface;

  Body := TNewStaticText.Create(ConsentPage);
  Body.Left := Title.Left;
  Body.Top := Title.Top + ScaleY(46);
  Body.Width := Title.Width;
  Body.Height := ScaleY(42);
  Body.AutoSize := False;
  Body.WordWrap := True;
  Body.Caption := 'Review the Privacy Policy, then choose where to install the app and store your clips.';
  ApplyTextStyle(Body, 10, InstallerTextDim, False);
  Body.Parent := ConsentPage.Surface;

  ConsentCheck := TNewCheckBox.Create(ConsentPage);
  ConsentCheck.Left := Title.Left;
  ConsentCheck.Top := Body.Top + ScaleY(120);
  ConsentCheck.Width := Title.Width;
  ConsentCheck.Height := ScaleY(28);
  ConsentCheck.Caption := 'I agree to the Privacy Policy';
  ConsentCheck.Checked := False;
  ConsentCheck.OnClick := @ConsentChanged;
  ApplyCheckStyle(ConsentCheck, 10, InstallerText);
  ConsentCheck.Parent := ConsentPage.Surface;

  PolicyText := TNewStaticText.Create(ConsentPage);
  PolicyText.Left := ConsentCheck.Left;
  PolicyText.Top := Body.Top + ScaleY(70);
  PolicyText.Caption := 'Read the Privacy Policy';
  PolicyText.Cursor := crHand;
  PolicyText.OnClick := @PolicyTextClick;
  ApplyTextStyle(PolicyText, 10, InstallerAccent, False);
  PolicyText.Font.Style := [fsUnderline];
  PolicyText.Parent := ConsentPage.Surface;

  ConsentError := TNewStaticText.Create(ConsentPage);
  ConsentError.Left := ConsentCheck.Left;
  ConsentError.Top := ConsentCheck.Top + ConsentCheck.Height + ScaleY(10);
  ConsentError.Width := ConsentPage.SurfaceWidth - ScaleX(72);
  ConsentError.AutoSize := False;
  ConsentError.Caption := 'Tick the Privacy Policy box to continue.';
  ApplyTextStyle(ConsentError, 10, StrToColor('#FFA49B'), False);
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
  Title.Left := ScaleX(36);
  Title.Top := ScaleY(30);
  Title.Width := LocationPage.SurfaceWidth - ScaleX(72);
  Title.Height := ScaleY(44);
  Title.AutoSize := False;
  Title.Caption := 'Set up FTHR Clips';
  ApplyDisplayStyle(Title, 20, InstallerText);
  Title.Parent := LocationPage.Surface;

  InstallLabel := TNewStaticText.Create(LocationPage);
  InstallLabel.Left := Title.Left;
  InstallLabel.Top := ScaleY(100);
  InstallLabel.Width := Title.Width;
  InstallLabel.Height := ScaleY(20);
  InstallLabel.AutoSize := False;
  InstallLabel.Caption := 'INSTALL FOLDER';
  ApplyDisplayStyle(InstallLabel, 9, InstallerText);
  InstallLabel.Parent := LocationPage.Surface;

  FieldWidth := LocationPage.SurfaceWidth - ScaleX(180);
  InstallDirEdit := WizardForm.DirEdit;
  InstallDirEdit.Parent := LocationPage.Surface;
  InstallDirEdit.Left := Title.Left;
  InstallDirEdit.Top := ScaleY(126);
  InstallDirEdit.Width := FieldWidth;
  InstallDirEdit.Height := ScaleY(32);
  StyleEdit(InstallDirEdit);

  CreateBoxButton(LocationPage, LocationPage.Surface,
    InstallBrowseBorder, InstallBrowseFill, InstallBrowseLabel,
    InstallDirEdit.Left + InstallDirEdit.Width + ScaleX(8),
    InstallDirEdit.Top, ScaleX(100), 'BROWSE', False);
  InstallBrowseBorder.OnClick := @BrowseInstallClick;
  InstallBrowseFill.OnClick := @BrowseInstallClick;
  InstallBrowseLabel.OnClick := @BrowseInstallClick;

  ClipLabel := TNewStaticText.Create(LocationPage);
  ClipLabel.Left := Title.Left;
  ClipLabel.Top := ScaleY(190);
  ClipLabel.Width := Title.Width;
  ClipLabel.Height := ScaleY(20);
  ClipLabel.AutoSize := False;
  ClipLabel.Caption := 'CLIP FOLDER';
  ApplyDisplayStyle(ClipLabel, 9, InstallerText);
  ClipLabel.Parent := LocationPage.Surface;

  ClipDirEdit := TNewEdit.Create(LocationPage);
  ClipDirEdit.Left := Title.Left;
  ClipDirEdit.Top := ScaleY(216);
  ClipDirEdit.Width := FieldWidth;
  ClipDirEdit.Height := ScaleY(32);
  StyleEdit(ClipDirEdit);
  ClipDirEdit.Parent := LocationPage.Surface;

  CreateBoxButton(LocationPage, LocationPage.Surface,
    ClipBrowseBorder, ClipBrowseFill, ClipBrowseLabel,
    ClipDirEdit.Left + ClipDirEdit.Width + ScaleX(8),
    ClipDirEdit.Top, ScaleX(100), 'BROWSE', False);
  ClipBrowseBorder.OnClick := @BrowseClipsClick;
  ClipBrowseFill.OnClick := @BrowseClipsClick;
  ClipBrowseLabel.OnClick := @BrowseClipsClick;

  Note := TNewStaticText.Create(LocationPage);
  Note.Left := Title.Left;
  Note.Top := ScaleY(258);
  Note.Width := Title.Width;
  Note.Height := ScaleY(40);
  Note.WordWrap := True;
  Note.AutoSize := False;
  Note.Caption := 'Clips, recordings, screenshots, and exports use this folder. You can change it later in app settings.';
  ApplyTextStyle(Note, 9, InstallerTextDim, False);
  Note.Parent := LocationPage.Surface;

  DesktopShortcutCheck := TNewCheckBox.Create(LocationPage);
  DesktopShortcutCheck.Parent := LocationPage.Surface;
  DesktopShortcutCheck.SetBounds(Title.Left, ScaleY(300), Title.Width, ScaleY(28));
  DesktopShortcutCheck.Caption := 'Add a desktop shortcut';
  DesktopShortcutCheck.Checked := WizardIsTaskSelected('desktopicon');
  ApplyCheckStyle(DesktopShortcutCheck, 10, InstallerText);

  LocationError := TNewStaticText.Create(LocationPage);
  LocationError.Left := Title.Left;
  LocationError.Top := ScaleY(342);
  LocationError.Width := Title.Width;
  LocationError.Height := ScaleY(30);
  LocationError.WordWrap := True;
  LocationError.AutoSize := False;
  LocationError.Caption := 'Choose both folders to continue.';
  ApplyTextStyle(LocationError, 9, StrToColor('#FFA49B'), False);
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
begin
  { Retain Inno's real finish lifecycle: native postinstall selection runs once
    on Finish, never on cancellation/failure/silent setup, and is withheld when
    a restart is needed. Do not launch the application from a page event. }
  WizardForm.FinishedPage.SetBounds(0, 0,
    WizardForm.OuterNotebook.Width, WizardForm.OuterNotebook.Height);
  WizardForm.FinishedPage.Color := InstallerBackground;
  WizardForm.FinishedPage.StyleElements := [];
  WizardForm.FinishedHeadingLabel.SetBounds(ScaleX(36), ScaleY(32),
    WizardForm.OuterNotebook.Width - ScaleX(72), ScaleY(52));
  ApplyDisplayStyle(WizardForm.FinishedHeadingLabel, 22, InstallerText);
  WizardForm.FinishedLabel.SetBounds(ScaleX(36), ScaleY(98),
    WizardForm.OuterNotebook.Width - ScaleX(72), ScaleY(112));
  ApplyTextStyle(WizardForm.FinishedLabel, 10, InstallerTextDim, False);
  FinishPathLabel := WizardForm.FinishedLabel;
  WizardForm.RunList.SetBounds(ScaleX(36), ScaleY(234),
    WizardForm.OuterNotebook.Width - ScaleX(72), ScaleY(52));
  WizardForm.RunList.Color := InstallerBackground;
  WizardForm.RunList.Font.Name := 'Segoe UI';
  WizardForm.RunList.Font.Size := 11;
  WizardForm.RunList.Font.Color := InstallerText;
  WizardForm.RunList.StyleElements := [];
  { Keep the platform restart choice and its keyboard behavior intact. }
  WizardForm.YesRadio.Left := ScaleX(36);
  WizardForm.YesRadio.Top := ScaleY(234);
  WizardForm.NoRadio.Left := ScaleX(36);
  WizardForm.NoRadio.Top := ScaleY(270);
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
begin
  if (ProgressTrack <> nil) and (MaxProgress > 0) then
    ProgressFill.Width := Round(ProgressTrack.Width * (CurProgress / MaxProgress));
end;

procedure CreateInstallingSurface;
begin
  WizardForm.InstallingPage.Color := InstallerBackground;
  WizardForm.InstallingPage.StyleElements := [];

  InstallingTitle := TNewStaticText.Create(WizardForm);
  InstallingTitle.Left := ScaleX(36);
  InstallingTitle.Top := ScaleY(28);
  InstallingTitle.Width := WizardForm.InstallingPage.Width - ScaleX(72);
  InstallingTitle.Height := ScaleY(48);
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
  { Keep the native progress control for its value/accessibility. A simple
    foreground track gives consistent mint rendering under Windows themes. }
  ProgressTrack := TPanel.Create(WizardForm);
  ProgressTrack.SetBounds(WizardForm.ProgressGauge.Left,
    WizardForm.ProgressGauge.Top, WizardForm.ProgressGauge.Width,
    WizardForm.ProgressGauge.Height);
  ProgressTrack.BevelOuter := bvNone;
  ProgressTrack.Color := InstallerSurface;
  ProgressTrack.ParentBackground := False;
  ProgressTrack.StyleElements := [];
  ProgressTrack.Parent := WizardForm.InstallingPage;
  ProgressFill := TPanel.Create(WizardForm);
  ProgressFill.SetBounds(0, 0, 0, ProgressTrack.Height);
  ProgressFill.BevelOuter := bvNone;
  ProgressFill.Color := InstallerAccent;
  ProgressFill.ParentBackground := False;
  ProgressFill.StyleElements := [];
  ProgressFill.Parent := ProgressTrack;

  WizardForm.StatusLabel.Left := InstallingTitle.Left;
  WizardForm.StatusLabel.Top := ScaleY(170);
  WizardForm.StatusLabel.Width := InstallingTitle.Width;
  WizardForm.StatusLabel.Height := ScaleY(22);
  WizardForm.StatusLabel.AutoSize := False;
  ApplyTextStyle(WizardForm.StatusLabel, 9, InstallerTextDim, False);
  WizardForm.FilenameLabel.Visible := False;
end;

#ifdef PreviewBuild
procedure CreatePreviewScreens;
var
  PreviewCopy: TNewStaticText;
begin
  { These pages use the shipping controls but never enter installation. The
    immutable PrepareToInstall barrier still prevents all filesystem changes. }
  PreviewInstallingPage := CreateCustomPage(LocationPage.ID, '', '');
  ApplyPageSurface(PreviewInstallingPage);
  InstallingTitle.Parent := PreviewInstallingPage.Surface;
  InstallingCopy.Parent := PreviewInstallingPage.Surface;
  InstallingCopy.Caption := 'Visual preview only. No files are being installed.';
  WizardForm.ProgressGauge.Parent := PreviewInstallingPage.Surface;
  WizardForm.ProgressGauge.Position := 65;
  ProgressTrack.Parent := PreviewInstallingPage.Surface;
  ProgressFill.Width := Round(ProgressTrack.Width * 0.65);
  WizardForm.StatusLabel.Parent := PreviewInstallingPage.Surface;
  WizardForm.StatusLabel.Caption := 'Preview of installation progress';
  PreviewFinishedPage := CreateCustomPage(PreviewInstallingPage.ID, '', '');
  ApplyPageSurface(PreviewFinishedPage);
  WizardForm.FinishedHeadingLabel.Parent := PreviewFinishedPage.Surface;
  WizardForm.FinishedHeadingLabel.Caption := 'FTHR Clips is ready';
  WizardForm.FinishedLabel.Parent := PreviewFinishedPage.Surface;
  WizardForm.FinishedLabel.Caption := 'Visual preview only. Nothing was installed.';
  WizardForm.RunList.Parent := PreviewFinishedPage.Surface;
  WizardForm.RunList.AddCheckBox('Open FTHR Clips', '', 0, True, True, False, False, nil);
  WizardForm.RunList.Visible := True;
  PreviewCopy := TNewStaticText.Create(PreviewFinishedPage);
  PreviewCopy.SetBounds(ScaleX(36), ScaleY(302),
    PreviewFinishedPage.SurfaceWidth - ScaleX(72), ScaleY(48));
  PreviewCopy.AutoSize := False;
  PreviewCopy.WordWrap := True;
  PreviewCopy.Caption := 'Completion option preview. This build never launches the app.';
  ApplyTextStyle(PreviewCopy, 9, InstallerTextDim, False);
  PreviewCopy.Parent := PreviewFinishedPage.Surface;
end;
#endif

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
#ifdef PreviewBuild
  Result := 'This is a visual preview build. Installation is disabled. ' +
    'Close this window and use the normal installer to install FTHR Clips.';
  exit;
#endif
  { File-lock detection misses source builds and other install locations.
    Never replace files or launch a successor while an old UI owns capture. }
  if CheckForMutexes(FTHRMutex) then begin
    Result := 'FTHR Clips is still running. Use its power button to shut down ' +
      'the app and background capture completely, then retry installation.';
    exit;
  end;
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
  InstallerBackground := StrToColor('#242629');
  InstallerSurface := StrToColor('#2C2F33');
  InstallerHeader := StrToColor('#202326');
  InstallerText := StrToColor('#FFFFFF');
  InstallerTextDim := StrToColor('#BFC3C7');
  InstallerAccent := StrToColor('#00FFAA');
  InstallerBorder := StrToColor('#4C5055');
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
    LegacyCleanupPage.CheckListBox.Font.Name := 'Segoe UI';
    LegacyCleanupPage.CheckListBox.Font.Size := 10;
    LegacyCleanupPage.CheckListBox.Font.Style := [];
    LegacyCleanupPage.CheckListBox.Font.Color := InstallerText;
    LegacyCleanupPage.CheckListBox.Color := InstallerBackground;
    LegacyCleanupPage.CheckListBox.ParentColor := False;
  end;

  CreateFinishPage;
  CreateInstallingSurface;
#ifdef PreviewBuild
  CreatePreviewScreens;
#endif
  CreateTitleBar;
  CreateSteps;
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
      if DesktopShortcutCheck.Checked then
        WizardSelectTasks('desktopicon')
      else
        WizardSelectTasks('');
      WizardForm.DirEdit.Text := Trim(InstallDirEdit.Text);
      ClipDirEdit.Text := Trim(ClipDirEdit.Text);
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = ConsentPage.ID then UpdateSteps(1)
  else if (CurPageID = LocationPage.ID) or
    ((LegacyCleanupPage <> nil) and (CurPageID = LegacyCleanupPage.ID)) then UpdateSteps(2)
  else UpdateSteps(3);
  NavBackBorder.Visible := False;
  NavNextBorder.Visible := False;
  NavBackLabel.Caption := 'BACK';

#ifdef PreviewBuild
  if (CurPageID = PreviewInstallingPage.ID) or (CurPageID = PreviewFinishedPage.ID) then begin
    WizardForm.BackButton.Enabled := True;
    NavBackBorder.Visible := True;
    NavNextBorder.Visible := True;
    NavNextLabel.Caption := 'NEXT PREVIEW';
    exit;
  end;
#endif
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
  end else if CurPageID = wpFinished then begin
    CreateFinishPage;
    if not WizardForm.YesRadio.Visible then
      FinishPathLabel.Caption := 'The app is installed.' + #13#10 + #13#10 +
      'Clips will be saved to ' + Trim(ClipDirEdit.Text);
    WizardForm.BackButton.Enabled := False;
    WizardForm.CancelButton.Enabled := False;
    NavNextLabel.Caption := 'FINISH';
    NavNextBorder.Visible := True;
  end else if CurPageID = wpPreparing then begin
    WizardForm.PreparingErrorBitmapImage.Left := ScaleX(36);
    WizardForm.PreparingErrorBitmapImage.Top := 0;
    WizardForm.PreparingMemo.Left := ScaleX(60);
    WizardForm.PreparingMemo.Width := WizardForm.InnerNotebook.Width - ScaleX(96);
    WizardForm.PreparingMemo.Font.Name := 'Segoe UI';
    WizardForm.PreparingMemo.Font.Size := 10;
    WizardForm.PreparingMemo.Font.Style := [];
    WizardForm.PreparingLabel.Left := ScaleX(60);
    WizardForm.PreparingLabel.Width := WizardForm.InnerNotebook.Width - ScaleX(96);
    ApplyTextStyle(WizardForm.PreparingLabel, 10, InstallerText, False);
    WizardForm.PreparingLabel.AdjustHeight;
    NavNextBorder.Visible := True;
    if WizardForm.NextButton.Enabled then begin
      // Applications are in use and Setup may close them: offer a real way out.
      NavBackLabel.Caption := 'CANCEL';
      NavBackBorder.Visible := True;
      NavNextLabel.Caption := 'CONTINUE';
    end else begin
      NavBackBorder.Visible := WizardForm.BackButton.Enabled;
      NavNextLabel.Caption := 'CANCEL';
    end;
  end else if CurPageID = wpInstalling then begin
    WizardForm.BackButton.Enabled := False;
  end else begin
    WizardForm.BackButton.Enabled := True;
    NavBackBorder.Visible := True;
    NavNextBorder.Visible := True;
    NavNextLabel.Caption := 'NEXT';
  end;

  StyleBoxButton(NavBackBorder, NavBackFill, NavBackLabel, False,
    WizardForm.BackButton.Enabled or
    ((CurPageID = wpPreparing) and WizardForm.NextButton.Enabled));
  StyleBoxButton(NavNextBorder, NavNextFill, NavNextLabel, True,
    WizardForm.NextButton.Enabled or
    ((CurPageID = wpPreparing) and WizardForm.CancelButton.Enabled));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := PageID = wpSelectTasks;
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
end;

function RunUninstallCleanup(const Phase: String): Boolean;
var
  ExitCode: Integer;
  ScriptPath, Arguments: String;
begin
  ScriptPath := ExpandConstant('{app}\uninstall_cleanup.ps1');
  Arguments := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ScriptPath + '" -InstallRoot "' + ExpandConstant('{app}') + '" -Phase ' + Phase;
  Result := FileExists(ScriptPath) and
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Arguments, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if Result then Result := ExitCode = 0;
  if not Result then
    Log('FTHR uninstall ' + Phase + ' failed; uninstall stopped before removing application files.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    if not RunUninstallCleanup('Shutdown') or CheckForMutexes(FTHRMutex) then begin
      SuppressibleMsgBox('FTHR Clips could not finish shutting down. ' +
        'Wait for recordings or exports to finish, close FTHR Clips using its power button, ' +
        'then run uninstall again. No application files were removed.', mbError, MB_OK, IDOK);
      Abort;
    end;
    if not RunUninstallCleanup('Cleanup') then begin
      SuppressibleMsgBox('Some FTHR data could not be cleaned up. ' +
        'Close any programs using its files and run uninstall again. ' +
        'The application remains installed so you can retry.', mbError, MB_OK, IDOK);
      Abort;
    end;
    RegDeleteValue(HKCU, RunKey, RunValueName);
    Log('Removed the FTHR Clips per-user autostart value.');
  end;
end;
