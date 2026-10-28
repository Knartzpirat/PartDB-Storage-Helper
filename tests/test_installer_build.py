"""Windows-Installer des Druck-Helfers: gepackter Betrieb (ohne zu bauen), Inno-Setup-Skript, CI und Docker."""
import json
import logging
import logging.handlers
import re
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from labeltool import helfer, helfer_setup as hs
from labeltool import routes_helfer_download as rd

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Mutex/Ereignis nur unter Windows")


@pytest.fixture
def gepackt(monkeypatch, tmp_path):
    """Wie LabeltoolHelfer.exe: sys.frozen, Programm in tmp/prog, Benutzerdaten in tmp/daten."""
    prog = tmp_path / "prog"
    prog.mkdir()
    for exe in (helfer.EXE_HELFER, helfer.EXE_SETUP):
        (prog / exe).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(prog / helfer.EXE_HELFER))
    monkeypatch.setenv(helfer.ENV_ORDNER, str(tmp_path / "daten"))
    monkeypatch.setenv(helfer.ENV_STILL, "1")
    return prog


# ------------------------------------------------------------------ Pfade
def test_benutzer_ordner_und_standard_konfig(monkeypatch, tmp_path):
    assert helfer.benutzer_ordner({"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}) == \
        Path(r"C:\Users\x\AppData\Local") / "Labeltool-Helfer"
    assert helfer.benutzer_ordner({helfer.ENV_ORDNER: str(tmp_path), "LOCALAPPDATA": "egal"}) == tmp_path
    assert helfer.benutzer_ordner({}).name == "Labeltool-Helfer"
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(helfer.ENV_ORDNER, str(tmp_path / "d"))
    assert not helfer.gepackt()
    assert helfer.standard_konfig() == Path("helfer.yaml")                 # Projekt/ZIP: wie bisher
    assert helfer.standard_konfig(frozen=True) == tmp_path / "d" / "helfer.yaml"
    assert helfer.programm_ordner() == ROOT
    assert "helfer.bat einrichten" in helfer.einrichten_hinweis()
    assert helfer.default_csv_dir(tmp_path / "x") == tmp_path / "d" / "csv"
    monkeypatch.delenv(helfer.ENV_ORDNER)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert helfer.default_csv_dir(tmp_path / "x") == tmp_path / "x" / ".helfer-csv"


def test_pfade_gepackt(gepackt, tmp_path):
    assert helfer.gepackt()
    assert helfer.standard_konfig() == tmp_path / "daten" / "helfer.yaml"
    assert helfer.programm_ordner() == gepackt.resolve()
    assert "Startmenü" in helfer.einrichten_hinweis()
    cfg_path = tmp_path / "daten" / "helfer.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text('server: "http://s:1"\ntoken: "t"\n', encoding="utf-8")
    cfg = helfer.load_helper_config(cfg_path)
    assert cfg.cache_dir == tmp_path / "daten" / ".helfer-cache"           # nicht im Programmordner
    assert cfg.csv_dir == tmp_path / "daten" / "csv"
    with pytest.raises(helfer.HelferConfigError, match="Druck-Helfer einrichten"):
        helfer.load_helper_config(tmp_path / "fehlt.yaml")


# ------------------------------------------------------------------ Kommandozeile Helfer
def test_optionen():
    opts, rest = helfer._optionen(["--diagnose", "a.json", "--vorlage", "x.lbx", "c.yaml", "--beenden"])
    assert opts == {"--diagnose": "a.json", "--vorlage": "x.lbx", "--beenden": True} and rest == ["c.yaml"]
    with pytest.raises(helfer.HelferConfigError):
        helfer._optionen(["--log"])


def test_start_ohne_konfiguration_gepackt(gepackt, monkeypatch, tmp_path):
    gestartet, fragen = [], []
    monkeypatch.setattr(helfer, "setup_starten", lambda p=None: gestartet.append(p))
    monkeypatch.setattr(helfer, "meldung", lambda text, frage=False, **k: fragen.append(text) or True)
    try:
        assert helfer.main([]) == 2
    finally:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    assert "noch nicht eingerichtet" in fragen[0] and gestartet == [tmp_path / "daten" / "helfer.yaml"]
    log = (tmp_path / "daten" / helfer.LOG_DATEI).read_text(encoding="utf-8")
    assert "Konfiguration nicht gefunden" in log


def test_start_ohne_konfiguration_projekt(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    try:
        with pytest.raises(SystemExit, match="helfer.bat einrichten"):
            helfer.main([str(tmp_path / "helfer.yaml")])
    finally:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)


def test_setup_starten_uebergibt_nur_abweichende_konfig(gepackt, tmp_path):
    calls = []
    helfer.setup_starten(helfer.standard_konfig(), popen=lambda a, **k: calls.append(a))
    helfer.setup_starten(tmp_path / "anders.yaml", popen=lambda a, **k: calls.append(a))
    assert calls[0] == [str(gepackt.resolve() / helfer.EXE_SETUP)]
    assert calls[1] == [str(gepackt.resolve() / helfer.EXE_SETUP), str(tmp_path / "anders.yaml")]


def test_logdatei_mit_rotation(tmp_path):
    try:
        helfer.logging_einrichten(tmp_path / "log" / "helfer.log")
        fh = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert fh and fh[0].maxBytes == 1024 * 1024 and fh[0].backupCount == 3
        logging.getLogger("labeltool.helfer").info("Hallo Protokoll")
        helfer.logging_einrichten(tmp_path / "log" / "nebenbei.log", rotation=False)
        assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers)
    finally:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    assert "Hallo Protokoll" in (tmp_path / "log" / "helfer.log").read_text(encoding="utf-8")


def test_beenden_ohne_benutzerordner_legt_nichts_an(gepackt, tmp_path):
    try:
        assert helfer.main(["--beenden"]) == 0
    finally:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    assert not (tmp_path / "daten").exists()


def test_diagnose_mit_attrappe(tmp_path, monkeypatch):
    from labeltool import ptouch_modelle as pm

    class Drucker:
        BpacError = Exception
        def preview_available(self): return True
        def status(self, name): return {"drucker": ["Brother PT-P710BT"]}
        def render_png(self, path, row):
            assert path.name == "PartDB_SMD15.lbx" and row and set(row.values()) == {"TEST"}
            return b"\x89PNG\r\n\x1a\n" + b"x" * 10

    monkeypatch.setattr(pm, "modelle_info", lambda neu=False: {"modelle": list(pm.EINGEBAUT), "quelle": "eingebaut"})
    monkeypatch.setattr(pm, "installierte_drucker", lambda: [])
    out = helfer.diagnose(tmp_path / "d.json", ROOT / "tests" / "vorlagen" / "PartDB_SMD15.lbx", printer=Drucker())
    data = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert out["ok"] and data["bpac"] and data["vorschau"]["png_kopf"] and (tmp_path / "d.png").is_file()
    assert data["modelle_anzahl"] == len(pm.EINGEBAUT) and data["websockets"] is True


# ------------------------------------------------------------------ Einzelinstanz
def test_instanz_name_je_konfiguration(tmp_path):
    a = helfer.instanz_name(tmp_path / "helfer.yaml")
    assert a.startswith(helfer.INSTANZ_PREFIX) and re.fullmatch(r"[\w-]+", a)
    assert a == helfer.instanz_name(str(tmp_path / "x" / ".." / "helfer.yaml"))
    assert a != helfer.instanz_name(tmp_path / "anders.yaml")
    if sys.platform == "win32":     # Windows-Pfade: Groß/Klein egal
        assert a == helfer.instanz_name(str(tmp_path / "HELFER.YAML").upper())


def test_beenden_ohne_laufenden_helfer():
    assert helfer.helfer_beenden("x.yaml", laeuft=lambda p: False) is None
    zustand = iter([True, True, False])
    assert helfer.helfer_beenden("x.yaml", timeout=5, laeuft=lambda p: next(zustand)) is True
    assert helfer.helfer_beenden("x.yaml", timeout=0.3, laeuft=lambda p: True) is False


@WINDOWS
def test_einzelinstanz_und_beenden_echt(tmp_path):
    cfg = tmp_path / "helfer.yaml"
    erster, zweiter = helfer.Einzelinstanz(cfg), helfer.Einzelinstanz(cfg)
    assert not helfer.helfer_laeuft(cfg)
    assert erster.erwerben()
    try:
        assert helfer.helfer_laeuft(cfg)
        assert not zweiter.erwerben()                          # Autostart + Handstart: nur einer
        assert helfer.Einzelinstanz(tmp_path / "anders.yaml").erwerben()   # andere Konfiguration darf
        assert not erster.warte_ende(timeout=0.05)

        def helfer_thread():                                   # wie main(): auf Ende warten, dann freigeben
            if erster.warte_ende(timeout=10):
                erster.freigeben()
        t = threading.Thread(target=helfer_thread)
        t.start()
        t0 = time.monotonic()
        assert helfer.helfer_beenden(cfg, timeout=10) is True
        assert time.monotonic() - t0 < 5
        t.join(5)
        assert not helfer.helfer_laeuft(cfg) and zweiter.erwerben()
        zweiter.freigeben()
    finally:
        erster.freigeben()


# ------------------------------------------------------------------ Assistent
def test_setup_argumente():
    a = hs.argumente(["--nach-installation", "--autostart", "nein", "--vorgabe", r"D:\dl\helfer.vorgabe.yaml", "x.yaml"])
    assert a == {"aus_bat": False, "konsole": False, "nach_installation": True,
                 "vorgabe": r"D:\dl\helfer.vorgabe.yaml", "autostart": False, "datei": "x.yaml"}
    assert hs.argumente(["--aus-bat", "helfer.yaml"])["aus_bat"]
    assert hs.argumente(["--autostart", "ja"])["autostart"] is True
    assert hs.argumente([])["datei"] is None
    with pytest.raises(hs.SetupFehler):
        hs.argumente(["--autostart", "vielleicht"])
    with pytest.raises(hs.SetupFehler):
        hs.argumente(["--vorgabe"])


def test_vorgabe_aus_setup_ordner(tmp_path):
    daten, src = tmp_path / "daten", tmp_path / "download"
    daten.mkdir(), src.mkdir()
    (src / hs.VORGABE).write_text('server: "http://unraid:8765"\ntoken: "NEU"\n', encoding="utf-8")
    assert hs.vorgabe_suchen(daten, src / hs.VORGABE) == src / hs.VORGABE
    assert hs.vorgabe_suchen(daten, src / "fehlt.yaml") == daten / hs.VORGABE
    assert hs.vorgabe_suchen(daten) == daten / hs.VORGABE
    w = hs.lade_werte(daten, src / hs.VORGABE)
    assert (w.server, w.token) == ("http://unraid:8765", "NEU")
    # bestehende Einstellungen (Update): Ordner/Modell bleiben, Token aus der neueren Vorgabe
    (daten / hs.DATEI).write_text('server: "http://alt:1"\ntoken: "ALT"\nvorlagen_ordner: "C:\\\\V"\n'
                                  'drucker_modell: "PT-P710BT"\n', encoding="utf-8")
    import os
    os.utime(src / hs.VORGABE, (time.time() + 60, time.time() + 60))
    w = hs.lade_werte(daten, src / hs.VORGABE)
    assert (w.token, w.vorlagen_ordner, w.drucker_modell) == ("NEU", "C:\\V", "PT-P710BT")
    assert hs.lade_werte(daten).token == "ALT"


def test_speichern_gepackt_startet_exe_und_neustart(gepackt, tmp_path):
    daten = tmp_path / "daten"
    w = hs.Werte(server="unraid:8765", token="abc", vorlagen_ordner=str(tmp_path / "V"),
                 drucker_modell="PT-P710BT", autostart=True)
    runs, popen, beendet = [], [], []

    def run(args, **kw):
        runs.append(kw["env"])
        Path(kw["env"]["LT_LNK"]).write_bytes(b"lnk")
        return type("R", (), {"returncode": 0, "stderr": ""})()

    out = hs.speichern(daten, w, autostart_dir=tmp_path / "Startup", runner=run,
                       popen=lambda a, **k: popen.append((a, k)), beenden=lambda p: beendet.append(p) or True)
    assert (daten / "helfer.yaml").is_file() and beendet == [daten / "helfer.yaml"]
    assert runs[0]["LT_ZIEL"] == str(gepackt.resolve() / helfer.EXE_HELFER) and runs[0]["LT_ARGS"] == ""
    assert popen[0][0] == [str(gepackt.resolve() / helfer.EXE_HELFER)]          # Standard-Konfig: ohne Argument
    assert "Laufender Helfer beendet." in out and out[-1] == "Helfer neu gestartet."
    # abweichende helfer.yaml: wird an Autostart und Start übergeben
    runs.clear(), popen.clear()
    anders = tmp_path / "anders"
    hs.speichern(anders, w, autostart_dir=tmp_path / "Startup", runner=run,
                 popen=lambda a, **k: popen.append((a, k)), beenden=lambda p: None)
    assert popen[0][0][1] == str((anders / "helfer.yaml").resolve())
    assert str((anders / "helfer.yaml").resolve()) in runs[0]["LT_ARGS"]


def test_speichern_projekt_wie_bisher_mit_neustart(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    (tmp_path / "helfer.bat").write_text("@echo off\r\n")
    w = hs.Werte(server="h:1", token="t", vorlagen_ordner=str(tmp_path / "V"), drucker_modell="PT-P710BT",
                 autostart=False)
    beendet, popen = [], []
    out = hs.speichern(tmp_path, w, aus_bat=True, autostart_dir=tmp_path / "S",
                       popen=lambda *a, **k: popen.append(a), beenden=lambda p: beendet.append(p) or False)
    assert beendet == [tmp_path / "helfer.yaml"] and not popen               # helfer.bat startet selbst
    assert any("reagiert nicht" in t for t in out)
    assert hs.autostart_ziel(tmp_path) == tmp_path / "helfer.bat"


def test_nach_abbruch_startet_helfer_wieder(tmp_path):
    cfg = tmp_path / "helfer.yaml"
    gestartet = []
    assert not hs.nach_abbruch(cfg, laeuft=lambda p: False, starten=gestartet.append)   # keine Konfig
    cfg.write_text("server: x\n", encoding="utf-8")
    assert not hs.nach_abbruch(cfg, laeuft=lambda p: True, starten=gestartet.append)    # läuft schon
    assert hs.nach_abbruch(cfg, laeuft=lambda p: False, starten=gestartet.append) and gestartet == [cfg]


# ------------------------------------------------------------------ Inno Setup / PyInstaller / build.ps1
def iss() -> str:
    return (INSTALLER / "labeltool-helfer.iss").read_text(encoding="utf-8")


def test_iss_grundeinstellungen():
    t = iss()
    assert re.search(r"^AppId=\{\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}$", t, re.M)
    assert "#ifndef Version" in t and "AppVersion={#Version}" in t and "VersionInfoVersion={#VersionNum}" in t
    for zeile in (r"DefaultDirName={autopf}\Labeltool-Helfer", "PrivilegesRequired=admin",
                  "PrivilegesRequiredOverridesAllowed=commandline", "OutputBaseFilename=Labeltool-Helfer-Setup",
                  "OutputDir=dist", r'MessagesFile: "compiler:Languages\German.isl"',
                  "UninstallDisplayName={#AppName}", "ArchitecturesInstallIn64BitMode=x64compatible"):
        assert zeile in t, zeile
    assert '#define AppName "Labeltool Druck-Helfer"' in t
    assert r'#define Bundle "build\bundle\Labeltool-Helfer"' in t


def test_iss_startmenue_autostart_und_assistent():
    t = iss()
    for text in ("de.EintragHelfer=Labeltool Druck-Helfer", "de.EintragEinrichten=Druck-Helfer einrichten",
                 "de.EintragDeinstallieren=Deinstallieren"):
        assert text in t
    task = re.search(r'^Name: "autostart";.*$', t, re.M).group(0)
    assert "unchecked" not in task                                # Autostart standardmäßig an
    # [Icons] hängt ".lnk" selbst an -> dort der Name ohne Endung (sonst entsteht "….lnk.lnk")
    assert re.search(r'^Name: "\{userstartup\}\\\{#AutostartName\}".*Check: AutostartAnlegen$', t, re.M)
    assert not re.search(r'^Name: "\{userstartup\}\\\{#AutostartLnk\}"', t, re.M)
    assert "WizardIsTaskSelected('autostart')" in t
    assert '#define AutostartName "Labeltool Druck-Helfer"' in t and '#define AutostartLnk AutostartName + ".lnk"' in t
    assert hs.AUTOSTART_NAME == "Labeltool Druck-Helfer.lnk"                 # gleicher Dateiname wie im Setup
    assert re.search(r'^Type: files; Name: "\{userstartup\}\\\{#AutostartLnk\}"', t, re.M)   # Deinstallation
    assert re.search(r'^Type: files; Name: "\{userstartup\}\\\{#AutostartAlt\}"', t, re.M)   # alter Doppelname
    run = re.search(r"^\[Run\]\n(.*)$", t, re.M).group(1)
    assert "{#SetupExe}" in run and "postinstall" in run and "runasoriginaluser" in run and "skipifsilent" in run
    assert run.endswith("Check: not IstUpdate")                   # /UPDATE: kein Assistent
    assert r"ExpandConstant('{src}\helfer.vorgabe.yaml')" in t and "'--nach-installation --autostart '" in t
    assert re.search(r'^Filename: "\{app\}\\\{#HelferExe\}"; Parameters: "--beenden"', t, re.M)
    assert "function PrepareToInstall" in t and "ExecAsOriginalUser" in t and "CloseApplications=force" in t
    assert "{localappdata}\\Labeltool-Helfer" in t                # Hinweis: Benutzerdaten bleiben
    assert '#define HelferExe "LabeltoolHelfer.exe"' in t and helfer.EXE_HELFER == "LabeltoolHelfer.exe"
    assert '#define SetupExe "LabeltoolHelferSetup.exe"' in t and helfer.EXE_SETUP == "LabeltoolHelferSetup.exe"


def test_iss_update_parameter():
    """/UPDATE (Update per Knopf): kein Assistent, Autostart unverändert, Helfer danach neu starten."""
    t = iss()
    assert "function IstUpdate(): Boolean;" in t and "CompareText(ParamStr(I), '/UPDATE') = 0" in t
    neustart = re.search(r'^Filename: "\{app\}\\\{#HelferExe\}"; Parameters: "\{code:HelferParameter\}".*$', t, re.M)
    assert neustart, "Neustart des Helfers fehlt in [Run]"
    zeile = neustart.group(0)
    assert "runasoriginaluser" in zeile and "nowait" in zeile and zeile.endswith("Check: IstUpdate")
    assert "postinstall" not in zeile and "skipifsilent" not in zeile          # auch bei /SILENT
    # Autostart beim Update: nur, wenn die Verknüpfung schon da ist (Assistent kann sie abgeschaltet haben)
    auto = re.search(r"function AutostartAnlegen\(\): Boolean;.*?^end;", t, re.M | re.S).group(0)
    assert "IstUpdate()" in auto and "HatteAutostart" in auto
    # Zustand vor jeder Änderung gemerkt – auch unter dem alten Namen "….lnk.lnk"
    assert "{#AutostartAlt}" in t and "function InitializeSetup" in t and "ssPostInstall" in t
    assert "{param:KONFIG|}" in t                                             # abweichende helfer.yaml
    # Parameter, mit denen der Helfer das Setup startet
    assert helfer.UPDATE_PARAMETER.split() == ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE"]
    assert re.search(r"^AppId=\{" + re.escape(helfer.APP_ID) + "$", t, re.M)    # Registry-Schlüssel im Helfer


def test_version_vorgabe_im_repo():
    from labeltool import _version, __version__
    assert _version.VERSION == "0.0.0-dev" == __version__                       # nie die Build-Version einchecken
    text = (ROOT / "labeltool" / "_version.py").read_text(encoding="utf-8")
    assert re.search(r'^VERSION = "0\.0\.0-dev"$', text, re.M)


def test_pyinstaller_spec_und_build_skript():
    spec = (INSTALLER / "helfer.spec").read_text(encoding="utf-8")
    assert 'name="LabeltoolHelfer"' in spec and 'name="LabeltoolHelferSetup"' in spec
    assert spec.count("console=False") == 2 and 'name="Labeltool-Helfer"' in spec
    for mod in ("win32com.client", "pythoncom", "win32print", "websockets.asyncio.client"):
        assert f'"{mod}"' in spec
    assert all(f'"{p}"' in spec for p in rd.VERBOTEN)                    # Server-Pakete ausgeschlossen
    for name in ("start_helfer.py", "start_setup.py"):
        assert "sys.exit(main())" in (INSTALLER / name).read_text(encoding="utf-8")
    ps = (INSTALLER / "build.ps1").read_text(encoding="utf-8")
    assert '"Labeltool-Helfer-Setup.exe"' in ps and "/DVersion=$Version" in ps and "JRSoftware.InnoSetup" in ps
    assert "helfer.spec" in ps and "labeltool-helfer.iss" in ps
    # Build-Version: _version.py nur während PyInstaller, danach Repo-Stand; Versionsdatei neben dem Setup
    schreiben, bundeln = ps.index("WriteAllText($VersionPy"), ps.index("-m PyInstaller --noconfirm")
    zurueck = ps.index("WriteAllBytes($VersionPy, $VersionPyAlt)")
    assert schreiben < bundeln < zurueck and "finally" in ps[bundeln:zurueck]
    assert '"Labeltool-Helfer-Setup.version"' in ps and 'labeltool\\_version.py' in ps


# ------------------------------------------------------------------ CI und Docker
def test_workflow_struktur():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    inst, image, ver = jobs["installer"], jobs["image"], jobs["version"]
    assert inst["runs-on"] == "windows-latest" and set(image["needs"]) == {"version", "test", "installer"}
    # eine Version für beide Jobs (Job-Output)
    assert ver["outputs"]["version"] == "${{ steps.version.outputs.version }}"
    vrun = next(s for s in ver["steps"] if s.get("id") == "version")["run"]
    assert "0.0.0-" in vrun and "GITHUB_OUTPUT" in vrun
    assert inst["needs"] == ["version"]
    schritte = {s.get("name") or s.get("uses"): s for s in inst["steps"]}
    assert "build.ps1" in schritte["Installer bauen"]["run"]
    assert '-Version "${{ needs.version.outputs.version }}"' in schritte["Installer bauen"]["run"]
    upload = next(s for s in inst["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["path"].split() == ["installer/dist/Labeltool-Helfer-Setup.exe",
                                              "installer/dist/Labeltool-Helfer-Setup.version"]
    release = schritte["Release-Asset (nur Tags)"]
    assert "refs/tags/v" in release["if"] and release["with"]["files"] == "installer/dist/Labeltool-Helfer-Setup.exe"
    assert "needs.version.outputs.version" in schritte["Gepacktes Programm prüfen"]["run"]
    assert inst["permissions"]["contents"] == "write"
    download = next(s for s in image["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert download["with"]["name"] == upload["with"]["name"]
    build = next(s for s in image["steps"] if str(s.get("uses", "")).startswith("docker/build-push-action"))
    assert f"helfer-installer={download['with']['path']}" in build["with"]["build-contexts"]
    assert build["with"]["build-args"].strip() == "VERSION=${{ needs.version.outputs.version }}"


def test_dockerfile_installer_optional():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM scratch AS helfer-installer" in df                      # leerer Standard: lokaler Build geht
    ziel = re.search(r"^COPY --from=helfer-installer / (\S+)$", df, re.M).group(1)
    assert Path(ziel.rstrip("/")).as_posix() == rd.INSTALLER_STANDARD.parent.as_posix()
    assert df.index("FROM scratch AS helfer-installer") < df.index("FROM python")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "installer/" in ignore
    # Build-Version: ARG mit Vorgabe (lokales "docker build ." ohne Arg) -> labeltool/_version.py im Image
    assert re.search(r"^ARG VERSION=0\.0\.0-dev$", df, re.M)
    assert df.index("COPY labeltool/ labeltool/") < df.index("ARG VERSION") < df.index("\"${VERSION}\" > labeltool/_version.py")
    assert df.index("\"${VERSION}\" > labeltool/_version.py") < df.index("USER ${PUID}")
