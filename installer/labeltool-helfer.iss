; Inno-Setup-Skript für den Labeltool Druck-Helfer (Windows).
;
; Bauen (installer\build.ps1 erledigt alles):
;   ISCC.exe /DVersion=1.2.3 installer\labeltool-helfer.iss
; Erwartet das PyInstaller-Ergebnis in installer\build\bundle\Labeltool-Helfer\ (/DBundle=… für einen anderen
; Ort) und schreibt installer\dist\Labeltool-Helfer-Setup.exe.
;
; Installiert nach {autopf}\Labeltool-Helfer (Admin). Für Tests ohne Admin:
;   Labeltool-Helfer-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /DIR=C:\tmp\inst /TASKS=""
; Einstellungen, Cache und Protokoll liegen in %LOCALAPPDATA%\Labeltool-Helfer\ und bleiben bei
; Update und Deinstallation erhalten.
;
; Update per Knopf (der Helfer startet das Setup selbst):
;   Labeltool-Helfer-Setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE [/CURRENTUSER] ["/KONFIG=<helfer.yaml>"]
;   /UPDATE: kein Einrichtungs-Assistent, Autostart bleibt wie er ist (Verknüpfung vorhanden oder nicht),
;   laufender Helfer wird beendet und nach der Installation als angemeldeter Benutzer neu gestartet
;   (auch im Silent-Modus). /KONFIG nur bei einer abweichenden helfer.yaml.

#ifndef Version
  #define Version "0.0.0-dev"
#endif
#ifndef Bundle
  #define Bundle "build\bundle\Labeltool-Helfer"
#endif
; Windows-Dateiversion braucht Zahlen: "1.2.3-rc1" / "0.0.0-abc1234" -> "1.2.3"
#if Pos("-", Version) > 0
  #define VersionNum Copy(Version, 1, Pos("-", Version) - 1)
#else
  #define VersionNum Version
#endif

#define AppName "Labeltool Druck-Helfer"
#define HelferExe "LabeltoolHelfer.exe"
#define SetupExe "LabeltoolHelferSetup.exe"
; [Icons] hängt ".lnk" selbst an – dort AutostartName verwenden, für Dateioperationen AutostartLnk
#define AutostartName "Labeltool Druck-Helfer"
#define AutostartLnk AutostartName + ".lnk"
; ältere Setups legten die Verknüpfung versehentlich als "….lnk.lnk" an
#define AutostartAlt AutostartLnk + ".lnk"

[Setup]
; AppId nie ändern – daran erkennt Windows Updates derselben Anwendung
AppId={{8C1E5B4F-3A7D-4E2B-9F61-2D7C5A0B9E43}
AppName={#AppName}
AppVersion={#Version}
AppVerName={#AppName} {#Version}
AppPublisher=Labeltool
AppComments=Verbindet diesen PC (b-PAC, P-touch Editor, Etikettendrucker) mit dem Labeltool-Server.
VersionInfoVersion={#VersionNum}
VersionInfoProductVersion={#VersionNum}
VersionInfoProductTextVersion={#Version}
VersionInfoDescription={#AppName} – Setup
DefaultDirName={autopf}\Labeltool-Helfer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyPage=no
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=Labeltool-Helfer-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#HelferExe}
; laufenden Helfer vor dem Kopieren beenden (zusätzlich zu "--beenden", s. [Code])
CloseApplications=force
RestartApplications=no
; Autostart liegt bewusst beim angemeldeten Benutzer ({userstartup}), auch bei Admin-Installation
UsedUserAreasWarning=no
ShowLanguageDialog=no

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[CustomMessages]
de.AufgabeAutostart=Beim Anmelden automatisch starten (im Hintergrund)
de.AufgabenGruppe=Druck-Helfer:
de.AssistentStarten=Einrichtungs-Assistent öffnen (Server, Token, Vorlagen-Ordner, Drucker)
de.EintragHelfer=Labeltool Druck-Helfer
de.EintragEinrichten=Druck-Helfer einrichten
de.EintragDeinstallieren=Deinstallieren
de.DatenBleiben=Der Labeltool Druck-Helfer wurde entfernt.%n%nDeine Einstellungen (helfer.yaml), der Vorlagen-Zwischenspeicher und das Protokoll liegen weiterhin in%n%1%n%nWenn du sie nicht mehr brauchst, kannst du diesen Ordner von Hand löschen. Der Vorlagen-Ordner mit deinen .lbx-Dateien wird nicht angetastet.

[Tasks]
Name: "autostart"; Description: "{cm:AufgabeAutostart}"; GroupDescription: "{cm:AufgabenGruppe}"

[InstallDelete]
; alte Bibliotheken eines früheren Builds nicht liegen lassen
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Bundle}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{cm:EintragHelfer}"; Filename: "{app}\{#HelferExe}"; WorkingDir: "{app}"; Comment: "Druck-Helfer starten (läuft im Hintergrund)"
Name: "{group}\{cm:EintragEinrichten}"; Filename: "{app}\{#SetupExe}"; WorkingDir: "{app}"; Comment: "Server, Token, Vorlagen-Ordner und Drucker einstellen"
Name: "{group}\{cm:EintragDeinstallieren}"; Filename: "{uninstallexe}"; Comment: "Labeltool Druck-Helfer entfernen"
; Autostart: bei normaler Installation laut Aufgabe, bei /UPDATE nur, wenn die Verknüpfung schon da ist
Name: "{userstartup}\{#AutostartName}"; Filename: "{app}\{#HelferExe}"; WorkingDir: "{app}"; Comment: "Labeltool Druck-Helfer"; Check: AutostartAnlegen

[Run]
Filename: "{app}\{#SetupExe}"; Parameters: "{code:AssistentParameter}"; WorkingDir: "{app}"; Description: "{cm:AssistentStarten}"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: not IstUpdate
; /UPDATE: Helfer als angemeldeter Benutzer wieder starten (kein postinstall -> auch bei /SILENT und /VERYSILENT)
Filename: "{app}\{#HelferExe}"; Parameters: "{code:HelferParameter}"; WorkingDir: "{app}"; Flags: nowait runasoriginaluser; Check: IstUpdate

[UninstallRun]
Filename: "{app}\{#HelferExe}"; Parameters: "--beenden"; Flags: runhidden waituntilterminated; RunOnceId: "HelferBeenden"

[UninstallDelete]
; Autostart-Verknüpfung – auch wenn der Assistent sie angelegt hat
Type: files; Name: "{userstartup}\{#AutostartLnk}"
Type: files; Name: "{userstartup}\{#AutostartAlt}"
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
var
  { Autostart vor der Installation (auch unter dem alten Namen "….lnk.lnk") – vor jeder Änderung gemerkt }
  HatteAutostart: Boolean;

function InitializeSetup(): Boolean;
begin
  HatteAutostart := FileExists(ExpandConstant('{userstartup}\{#AutostartLnk}')) or
                    FileExists(ExpandConstant('{userstartup}\{#AutostartAlt}'));
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { alte Verknüpfung mit doppelter Endung entfernen – die neue heißt richtig }
  if CurStep = ssPostInstall then
    DeleteFile(ExpandConstant('{userstartup}\{#AutostartAlt}'));
end;

{ /UPDATE auf der Kommandozeile (Update per Knopf aus dem Helfer) }
function IstUpdate(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

{ abweichende helfer.yaml des laufenden Helfers ("/KONFIG=<Pfad>"), sonst leer = Standard }
function KonfigPfad(): String;
begin
  Result := ExpandConstant('{param:KONFIG|}');
end;

function HelferParameter(Param: String): String;
begin
  Result := '';
  if KonfigPfad() <> '' then
    Result := '"' + KonfigPfad() + '"';
end;

function AutostartAnlegen(): Boolean;
begin
  if IstUpdate() then
    Result := HatteAutostart
  else
    Result := WizardIsTaskSelected('autostart');
end;

function AssistentParameter(Param: String): String;
var
  Vorgabe: String;
begin
  Result := '--nach-installation --autostart ';
  if WizardIsTaskSelected('autostart') then
    Result := Result + 'ja'
  else
    Result := Result + 'nein';
  { helfer.vorgabe.yaml (Server + Token) liegt im Installer-ZIP neben der Setup-Datei }
  Vorgabe := ExpandConstant('{src}\helfer.vorgabe.yaml');
  if FileExists(Vorgabe) then
    Result := Result + ' --vorgabe "' + Vorgabe + '"';
end;

procedure HelferBeenden();
var
  Exe, Args: String;
  Code: Integer;
begin
  Exe := ExpandConstant('{app}\{#HelferExe}');
  Args := '--beenden';
  if KonfigPfad() <> '' then
    Args := Args + ' "' + KonfigPfad() + '"';
  if FileExists(Exe) then
  begin
    { als angemeldeter Benutzer: dessen helfer.yaml/Sitzung; was übrig bleibt, beendet CloseApplications }
    if not ExecAsOriginalUser(Exe, Args, '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Exec(Exe, Args, '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  HelferBeenden();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
    MsgBox(FmtMessage(CustomMessage('DatenBleiben'), [ExpandConstant('{localappdata}\Labeltool-Helfer')]),
           mbInformation, MB_OK);
end;
