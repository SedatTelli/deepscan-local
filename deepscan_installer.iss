; DeepScan Local — Inno Setup Installer Script
; Designer: Sedat Telli | sedattelli.com
;
; Build: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" deepscan_installer.iss
; Output: installer\DeepScanLocal_Setup.exe

#define AppName      "DeepScan Local"
#define AppVersion   "1.0.0"
#define AppPublisher "Sedat Telli"
#define AppURL       "https://sedattelli.com"
#define AppExeName   "DeepScanLocal.exe"
#define SrcDir       "dist\DeepScanLocal"

[Setup]
AppId={{B7A2C1D3-4E5F-4A6B-8C9D-0E1F2A3B4C5D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Output
OutputDir=installer
OutputBaseFilename=DeepScanLocal_Setup
; Compression
Compression=lzma2/ultra64
SolidCompression=yes
; Appearance
WizardStyle=modern
WizardImageFile=wizard_finish.bmp
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
; Privileges — user-level install (no UAC prompt needed)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Min Windows 10
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "turkish";    MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english";    MessagesFile: "compiler:Default.isl"
Name: "german";     MessagesFile: "compiler:Languages\German.isl"
Name: "french";     MessagesFile: "compiler:Languages\French.isl"
Name: "spanish";    MessagesFile: "compiler:Languages\Spanish.isl"
Name: "arabic";     MessagesFile: "compiler:Languages\Arabic.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"
Name: "russian";    MessagesFile: "compiler:Languages\Russian.isl"
Name: "japanese";   MessagesFile: "compiler:Languages\Japanese.isl"
Name: "korean";     MessagesFile: "compiler:Languages\Korean.isl"

; Override the built-in language dialog label for all languages
[Messages]
SelectLanguageLabel=Kullanılacak dili seçin:

[Tasks]
Name: "desktopicon";  Description: "Masaüstüne kısayol oluştur";             GroupDescription: "Ek görevler:"; Flags: unchecked
Name: "startuprun";   Description: "Windows başlangıcında otomatik çalıştır"; GroupDescription: "Ek görevler:"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";       Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\Kaldır";           Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DeepScanLocal"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: startuprun

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im {#AppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; Database & config in %LOCALAPPDATA%\DeepScanLocal are left intact on uninstall.

; ---------------------------------------------------------------------------
; Seçilen installer dili → uygulama dil kodu → lang.txt
; ---------------------------------------------------------------------------
[Code]
function GetLangCode: String;
begin
  case ActiveLanguage of
    'turkish':    Result := 'tr';
    'german':     Result := 'de';
    'french':     Result := 'fr';
    'spanish':    Result := 'es';
    'arabic':     Result := 'ar';
    'portuguese': Result := 'pt';
    'russian':    Result := 'ru';
    'japanese':   Result := 'ja';
    'korean':     Result := 'ko';
  else
    Result := 'en';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDataDir: String;
  LangFile:   String;
  F:          TStringList;
begin
  if CurStep = ssPostInstall then
  begin
    AppDataDir := ExpandConstant('{localappdata}\DeepScanLocal');
    if not DirExists(AppDataDir) then
      CreateDir(AppDataDir);

    LangFile := AppDataDir + '\lang.txt';
    F := TStringList.Create;
    try
      F.Add(GetLangCode);
      F.SaveToFile(LangFile);
    finally
      F.Free;
    end;
  end;
end;
