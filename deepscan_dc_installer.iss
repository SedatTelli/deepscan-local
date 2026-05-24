; DeepScan Local — Domain / Enterprise Silent Installer
; Designer: Sedat Telli | sedattelli.com
;
; Hedef : Domain Controller üzerinden tüm PC'lere uzaktan sessiz kurulum
;
; Build : "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" deepscan_dc_installer.iss
; Çıktı : installer\DeepScanLocal_DC_Setup.exe
;
; Uzaktan kurulum komutu (GPO / SCCM / PDQ / psexec):
;   DeepScanLocal_DC_Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;
; Not   : Kurulum her kullanıcı oturumunda ayrı çalışmalıdır (%LocalAppData%).
;         GPO ile dağıtımda "Login Script" veya "User Configuration" tercih edin.

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
; Çıktı
OutputDir=installer
OutputBaseFilename=DeepScanLocal_DC_Setup
; Sıkıştırma
Compression=lzma2/ultra64
SolidCompression=yes
; Görünüm
WizardStyle=modern
WizardImageFile=wizard_finish.bmp
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
; Kullanıcı düzeyi kurulum — UAC gerektirmez
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Min Windows 10
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; --- Domain sürümü farkları ---
; Dil ekranını atla, Türkçe varsayılan
ShowLanguageDialog=no
; Sessiz modda gereksiz ekranları kapat
DisableWelcomePage=yes
DisableReadyPage=yes

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

[Tasks]
; Domain sürümünde her iki görev varsayılan olarak SEÇİLİ gelir
Name: "desktopicon";  Description: "Masaüstüne kısayol oluştur";             GroupDescription: "Ek görevler:"
Name: "startuprun";   Description: "Windows başlangıcında otomatik çalıştır"; GroupDescription: "Ek görevler:"

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
; Kullanıcı verileri (%LOCALAPPDATA%\DeepScanLocal) kaldırma sırasında korunur.

; ---------------------------------------------------------------------------
; Dil tespiti: önce sistem locale, yoksa Türkçe varsayılan
; ---------------------------------------------------------------------------
[Code]
function GetLangCode: String;
var
  LocaleID: Integer;
begin
  // ShowLanguageDialog=no olduğu için ActiveLanguage her zaman 'turkish' gelir.
  // Sistem diline göre uygulama dilini belirle.
  LocaleID := GetUILanguage;
  case LocaleID of
    $041F: Result := 'tr'; // Türkçe
    $0407, $0807, $0C07: Result := 'de'; // Almanca
    $040C, $080C, $0C0C: Result := 'fr'; // Fransızca
    $0C0A, $080A, $040A: Result := 'es'; // İspanyolca
    $0401, $0801, $0C01: Result := 'ar'; // Arapça
    $0416, $0816:        Result := 'pt'; // Portekizce
    $0419:               Result := 'ru'; // Rusça
    $0411:               Result := 'ja'; // Japonca
    $0412:               Result := 'ko'; // Korece
  else
    Result := 'en'; // Varsayılan İngilizce
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
