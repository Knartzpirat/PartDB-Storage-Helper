"""Windows-Druck-Helfer: verbindet den Labeltool-Server (z. B. Docker auf Unraid) mit b-PAC,
P-touch Editor und dem PT-P710BT an diesem PC.

Start: python -m labeltool.helfer [helfer.yaml]   (oder helfer.bat)

Der Helfer baut selbst die WebSocket-Verbindung zum Server auf (kein offener Port am PC),
meldet sich mit `hallo` + Token an, schickt alle ~10 s seinen Status und führt Aufträge aus
(Protokoll 2):
  vorschau          {vorlage, zeile}                  -> {"png": base64}
  drucken           {vorlage, zeilen, drucker, flags} -> {"gedruckt": n}
  oeffnen           {vorlage, csv: {name, inhalt}}    -> {"vorlage": verknüpfte Kopie, "csv"}
  bearbeiten        {name, csv: {name, inhalt}, pfad} -> {"vorlage", "csv", "backup"}
  ordner            {dateien: [{name, inhalt}], pfad} -> {"ordner"}
  datei             {name}                            -> {"name", "sha256", "daten"}   (Abgleich)
  vorlage_speichern {name, sha256, daten, ueberschreiben} -> {"name", "sha256", "pfad"}
  aktualisieren     {pfad, version, sha256}           -> {"version", "datei"}   (nur installierter Helfer)
`aktualisieren`: Setup über die eigene Server-Adresse + pfad mit dem Helfer-Token laden, sha256
prüfen, in %TEMP%\\Labeltool-Helfer-Update\\ ablegen, mit Administratorrechten starten
(/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE), antworten und sich danach beenden – der Installer
startet den Helfer neu. hallo/status melden dafür "installiert" (gepackt) und "version".
`vorlage` = {"name", "sha256", "daten"?: base64}; ohne `daten` aus dem Cache, fehlt sie dort ->
Antwort mit code "vorlage_fehlt", der Server schickt sie dann mit. `inhalt` = base64.

Vorlagen-Ordner (`vorlagen_ordner` in helfer.yaml): Quelle der .lbx-Layouts. Der Helfer schickt
beim Verbinden, alle ~10 s und sofort nach eigenen Änderungen ein Manifest
{"typ": "manifest", "ordner", "dateien": {name: sha256}, "uebersprungen": {name: grund}};
der Server holt fehlende/geänderte Dateien mit `datei` ab. Dateien, die gerade gespeichert
werden (jünger als 2 s oder kein vollständiges ZIP), stehen unter `uebersprungen` und werden
später erneut geprüft. CSVs schickt der Server als Inhalt; der Helfer legt sie in `csv_ordner`
(Standard %LOCALAPPDATA%\\Labeltool-Helfer\\csv) ab.

Alt (Protokoll 1): Server-Pfade (csv/pfad als Text) werden per `pfade` aus helfer.yaml in
Windows-Pfade übersetzt (längster Präfix gewinnt) – nur noch für „Bearbeiten“ ohne
Vorlagen-Ordner nötig. Bei Abbruch verbindet sich der Helfer mit wachsender Wartezeit
(bis 60 s) neu und liest helfer.yaml dabei jedes Mal neu ein.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import yaml

from . import __version__, bpac
from .desktop import (MAX_FILE, LocalDesktop, backup_once, default_printer, lbx_ok, print_queue,
                      printer_state, require_online, sha256, valid_name, write_atomic)
from .ptouch import TemplateError, open_file

log = logging.getLogger("labeltool.helfer")

PROTOKOLL = 2
WS_PATH = "api/helfer/ws"
STATUS_CHECK = 5.0           # s – Status prüfen, senden bei Änderung oder spätestens nach status_intervall
BACKOFF_MAX = 60.0
REPLACED_WAIT = 300.0       # s – nach Verdrängung durch einen anderen Helfer
MAX_MESSAGE = 32 * 1024 * 1024
CACHE_KEEP = 200             # so viele Vorlagenfassungen bleiben im Cache
STABLE_AGE = 2.0             # s – jüngere Dateien schreibt der P-touch Editor evtl. noch
UNREADY = "wird gerade gespeichert – nächster Versuch gleich"
FILE_JOBS = ("datei", "vorlage_speichern")    # Dateiarbeit: eigener Thread, blockiert Vorschau/Druck nicht
QUEUE_JOB = "warteschlange"                     # eigener Thread: läuft während b-PAC noch beschäftigt ist
UPDATE_JOB = "aktualisieren"                    # eigener Thread: Download + Administratorabfrage dauern
UPDATE_ORDNER = "Labeltool-Helfer-Update"       # in %TEMP%
UPDATE_PARAMETER = "/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE"
UPDATE_TIMEOUT = 120.0                          # s – je Lese-/Verbindungsschritt des Downloads
UPDATE_MAX = 512 * 1024 * 1024                  # Obergrenze der Setup-Datei
UPDATE_PAUSE = 1.5                              # s – nach der Antwort, dann beendet sich der Helfer
ERROR_CANCELLED = 1223                          # Windows: Administratorabfrage abgelehnt
CLOSE_TEXT = {
    4401: "Token abgelehnt – in helfer.yaml den aktuellen Helfer-Token eintragen (Einstellungen > Helfer).",
    4000: "Ein anderer Helfer hat sich am Server angemeldet und diesen ersetzt.",
    4400: "Server hat die Anmeldung nicht verstanden (Protokollfehler).",
    4408: "Server hat zu lange nichts vom Helfer gehört.",
}


class HelferConfigError(Exception):
    pass


class PathError(Exception):
    """Server-Pfad lässt sich nicht in einen Windows-Pfad übersetzen."""


class CacheMiss(Exception):
    """Vorlage ist nicht (mehr) im Cache – der Server soll sie mitschicken."""


class NotReady(Exception):
    """Datei wird gerade geschrieben/ist gesperrt – später erneut versuchen."""


class UpdateFehler(Exception):
    """Update per Knopf nicht möglich (Download, Prüfsumme, Administratorabfrage …)."""


# ------------------------------------------------------------------ Konfiguration
@dataclass
class HelferConfig:
    server: str
    token: str
    drucker: str = ""                       # leer = passend zu drucker_modell automatisch
    pfade: dict[str, str] = field(default_factory=dict)   # alt, nur noch ohne vorlagen_ordner nötig
    cache_dir: Path = Path(".helfer-cache")
    status_interval: float = 10.0
    vorlagen_ordner: Path | None = None     # Quelle der .lbx-Layouts auf diesem PC
    drucker_modell: str = ""                # z. B. "PT-P710BT"
    csv_dir: Path | None = None             # lokale CSVs + verknüpfte Kopien (None: neben dem Cache)
    sync_interval: float = 10.0             # s – Manifest des Vorlagen-Ordners


def default_csv_dir(base: Path) -> Path:
    if os.environ.get(ENV_ORDNER, "").strip() or os.environ.get("LOCALAPPDATA", "").strip():
        return benutzer_ordner() / "csv"
    return base / ".helfer-csv"


# ------------------------------------------------------------------ Gepackter Betrieb (Installer)
# Installiert (PyInstaller, sys.frozen) liegen helfer.yaml, Cache, CSVs und helfer.log in
# %LOCALAPPDATA%\Labeltool-Helfer\ – der Programmordner (C:\Program Files\…) bleibt schreibgeschützt.
# Aus dem Projekt/ZIP (helfer.bat) gilt weiter helfer.yaml im aktuellen Ordner.
APP_ORDNER = "Labeltool-Helfer"
ENV_ORDNER = "LABELTOOL_HELFER_DIR"        # anderer Benutzerdaten-Ordner (Tests, Sonderfälle)
ENV_STILL = "LABELTOOL_HELFER_STILL"       # "1": keine Meldungsfenster (Tests, unbeaufsichtigt)
EXE_HELFER = "LabeltoolHelfer.exe"
EXE_SETUP = "LabeltoolHelferSetup.exe"
LOG_DATEI = "helfer.log"
LOG_GROESSE = 1024 * 1024                  # 1 MB je Datei, dazu 3 ältere
LOG_ANZAHL = 3
INSTANZ_PREFIX = "Labeltool-Druck-Helfer-"


def gepackt() -> bool:
    """Läuft als installiertes Programm (PyInstaller)?"""
    return bool(getattr(sys, "frozen", False))


def benutzer_ordner(env: dict | None = None) -> Path:
    """%LOCALAPPDATA%\\Labeltool-Helfer (bzw. LABELTOOL_HELFER_DIR)."""
    env = os.environ if env is None else env
    eigen = str(env.get(ENV_ORDNER) or "").strip()
    if eigen:
        return Path(eigen)
    local = str(env.get("LOCALAPPDATA") or "").strip()
    return (Path(local) if local else Path.home() / "AppData" / "Local") / APP_ORDNER


def standard_konfig(frozen: bool | None = None) -> Path:
    """Standard-helfer.yaml: installiert im Benutzerordner, sonst im aktuellen Ordner."""
    frozen = gepackt() if frozen is None else frozen
    return benutzer_ordner() / "helfer.yaml" if frozen else Path("helfer.yaml")


def programm_ordner() -> Path:
    """Ordner mit LabeltoolHelfer.exe/LabeltoolHelferSetup.exe (installiert) bzw. das Projekt."""
    return Path(sys.executable).resolve().parent if gepackt() else Path(__file__).resolve().parent.parent


def einrichten_hinweis() -> str:
    return "Startmenü → „Druck-Helfer einrichten“" if gepackt() else "„helfer.bat einrichten“"


def instanz_name(config_path: str | Path) -> str:
    """Name von Mutex/Ende-Ereignis – je Konfigurationsdatei (Autostart + Handstart = dieselbe Datei)."""
    p = os.path.normcase(os.path.abspath(str(config_path)))
    return INSTANZ_PREFIX + hashlib.sha1(p.encode("utf-8")).hexdigest()[:16]


class _Kernel:
    """Die paar kernel32-Funktionen für Mutex und Ereignis (nur Windows)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, B, D, S = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, wintypes.LPCWSTR
        for name, res, args in (("CreateMutexW", H, (ctypes.c_void_p, B, S)), ("OpenMutexW", H, (D, B, S)),
                                ("CreateEventW", H, (ctypes.c_void_p, B, B, S)), ("OpenEventW", H, (D, B, S)),
                                ("SetEvent", B, (H,)), ("CloseHandle", B, (H,)),
                                ("WaitForSingleObject", D, (H, D))):
            fn = getattr(k, name)
            fn.restype, fn.argtypes = res, args
            setattr(self, name, fn)
        self.last_error = ctypes.get_last_error


ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF


class Einzelinstanz:
    """Nur ein Helfer je helfer.yaml: benannter Mutex `Local\\<name>`; dazu das Ereignis
    `Local\\<name>-Ende`, über das Assistent/Installer den laufenden Helfer sauber beenden."""

    def __init__(self, config_path: str | Path):
        self.name = instanz_name(config_path)
        self._k = _Kernel() if sys.platform == "win32" else None
        self._mutex = self._event = None

    def erwerben(self) -> bool:
        """True = wir sind der einzige Helfer für diese Konfiguration."""
        if self._k is None:
            return True
        h = self._k.CreateMutexW(None, False, "Local\\" + self.name)
        if not h:
            return True     # Mutex nicht anlegbar: lieber laufen als gar nicht
        if self._k.last_error() == ERROR_ALREADY_EXISTS:
            self._k.CloseHandle(h)
            return False
        self._mutex = h
        self._event = self._k.CreateEventW(None, True, False, "Local\\" + self.name + "-Ende")
        return True

    def warte_ende(self, timeout: float | None = None) -> bool:
        """Blockiert, bis jemand das Ende-Ereignis setzt (True) bzw. bis timeout (False)."""
        if self._k is None or not self._event:
            if timeout is None:
                threading.Event().wait()
            return False
        ms = INFINITE if timeout is None else int(timeout * 1000)
        return self._k.WaitForSingleObject(self._event, ms) == 0

    def freigeben(self) -> None:
        if self._k is None:
            return
        for h in (self._event, self._mutex):
            if h:
                self._k.CloseHandle(h)
        self._mutex = self._event = None


def helfer_laeuft(config_path: str | Path) -> bool:
    """Läuft (in dieser Windows-Sitzung) ein Helfer mit dieser helfer.yaml?"""
    if sys.platform != "win32":
        return False
    k = _Kernel()
    h = k.OpenMutexW(SYNCHRONIZE, False, "Local\\" + instanz_name(config_path))
    if not h:
        return False
    k.CloseHandle(h)
    return True


def helfer_beenden(config_path: str | Path, timeout: float = 10.0, laeuft=helfer_laeuft) -> bool | None:
    """Laufenden Helfer zu dieser helfer.yaml bitten, sich zu beenden.

    -> None: lief keiner; True: beendet; False: reagiert nicht innerhalb von timeout.
    """
    if not laeuft(config_path):
        return None
    if sys.platform == "win32":
        k = _Kernel()
        h = k.OpenEventW(EVENT_MODIFY_STATE, False, "Local\\" + instanz_name(config_path) + "-Ende")
        if h:
            k.SetEvent(h)
            k.CloseHandle(h)
    ende = time.monotonic() + timeout
    while time.monotonic() < ende:
        if not laeuft(config_path):
            return True
        time.sleep(0.1)
    return not laeuft(config_path)


def meldung(text: str, frage: bool = False, titel: str = "Labeltool Druck-Helfer") -> bool:
    """Meldungsfenster im installierten Programm (ohne Konsole). frage=True: Ja/Nein -> True bei Ja.

    Aus dem Projekt/ZIP (Konsole) bzw. mit LABELTOOL_HELFER_STILL=1: nur Text in die Konsole/das Log."""
    if os.environ.get(ENV_STILL, "").strip() not in ("", "0") or not gepackt() or sys.platform != "win32":
        if sys.stderr is not None:
            print(text, file=sys.stderr)
        return False
    import ctypes
    stil = (0x4 | 0x20) if frage else 0x40          # MB_YESNO|MB_ICONQUESTION bzw. MB_ICONINFORMATION
    return ctypes.windll.user32.MessageBoxW(None, text, titel, stil | 0x10000) == 6    # MB_SETFOREGROUND, IDYES


def setup_starten(config_path: Path | None = None, popen=None) -> None:
    """Einrichtungs-Assistenten des installierten Programms starten."""
    import subprocess
    popen = popen or subprocess.Popen
    args = [str(programm_ordner() / EXE_SETUP)]
    if config_path is not None and Path(config_path).resolve() != standard_konfig(True).resolve():
        args.append(str(config_path))
    popen(args, cwd=str(programm_ordner()))


def logging_einrichten(datei: Path | None, rotation: bool = True) -> None:
    """Konsole (falls vorhanden) und/oder Datei mit Rotation (1 MB × 3)."""
    from logging.handlers import RotatingFileHandler
    handlers: list[logging.Handler] = []
    if datei is not None:
        try:
            Path(datei).parent.mkdir(parents=True, exist_ok=True)
            fh = (RotatingFileHandler(datei, maxBytes=LOG_GROESSE, backupCount=LOG_ANZAHL, encoding="utf-8")
                  if rotation else logging.FileHandler(datei, encoding="utf-8"))
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
            handlers.append(fh)
        except OSError:
            pass
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        handlers.append(sh)
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def diagnose(ausgabe: Path, vorlage: Path | None = None, printer=bpac) -> dict:
    """Selbsttest ohne Druck: b-PAC, Drucker, Modellliste, optional Vorschau einer Vorlage (PNG
    neben `ausgabe`). Ergebnis als JSON nach `ausgabe` (das gepackte Programm hat keine Konsole)."""
    from . import ptouch_modelle
    out: dict = {"version": __version__, "gepackt": gepackt(), "programm": sys.executable,
                 "benutzer_ordner": str(benutzer_ordner()), "konfig": str(standard_konfig()), "ok": True}
    try:
        out["bpac"] = bool(printer.preview_available())
        out["drucker"] = [str(n) for n in printer.status("").get("drucker") or []]
    except Exception as e:
        out["bpac"], out["bpac_fehler"], out["ok"] = False, str(e), False
    try:
        info = ptouch_modelle.modelle_info()
        out["modelle_anzahl"], out["modelle_quelle"] = len(info["modelle"]), info["quelle"]
        out["installierte_drucker"] = ptouch_modelle.installierte_drucker()
    except Exception as e:
        out["modelle_fehler"], out["ok"] = str(e), False
    from . import ptouch_fonts
    out["ptouch_schriften"] = ptouch_fonts.load()   # leer = Trennstriche werden als Linie ersetzt
    try:
        import tkinter   # noqa: F401 – nur prüfen, ob gepackt
        out["tkinter"] = True
    except Exception as e:
        out["tkinter"] = f"fehlt: {e}"
    try:
        from websockets.asyncio.client import connect   # noqa: F401
        out["websockets"] = True
    except Exception as e:
        out["websockets"], out["ok"] = f"fehlt: {e}", False
    if vorlage is not None:
        try:
            row = {col: "TEST" for _, col in bpac.merge_fields(Path(vorlage)) if col}
            png = printer.render_png(Path(vorlage), row)
            ziel = Path(ausgabe).with_suffix(".png")
            ziel.write_bytes(png)
            out["vorschau"] = {"png": str(ziel), "bytes": len(png), "png_kopf": png[:8] == b"\x89PNG\r\n\x1a\n"}
        except Exception as e:
            out["vorschau"], out["ok"] = {"fehler": str(e)}, False
    Path(ausgabe).parent.mkdir(parents=True, exist_ok=True)
    Path(ausgabe).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _folder(raw, base: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    return p if p.is_absolute() else base / p


def _template_folder(raw, base: Path) -> Path | None:
    """Vorlagen-Ordner; Netzlaufwerk-Buchstaben werden zum UNC-Pfad (sonst evtl. "Pfad nicht gefunden")."""
    from .desktop import unc_path
    # erst übersetzen, dann auflösen: "Z:\…" ist nur unter Windows ein absoluter Pfad
    text = unc_path(os.path.expandvars(os.path.expanduser(str(raw or "").strip())))
    if not text:
        return None
    folder = Path(text)
    return folder if folder.is_absolute() or PureWindowsPath(text).is_absolute() else base / folder


def load_helper_config(path: str | Path) -> HelferConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise HelferConfigError(f"{path} nicht gefunden – {einrichten_hinweis()} ausführen.")
    except (OSError, yaml.YAMLError) as e:
        raise HelferConfigError(f"{path} kann nicht gelesen werden: {e}")
    if not isinstance(raw, dict):
        raise HelferConfigError(f"{path}: erwartet werden Einträge wie 'server:' und 'token:'.")
    server = str(raw.get("server") or "").strip()
    token = str(raw.get("token") or "").strip()
    if not server:
        raise HelferConfigError(f"{path}: 'server' fehlt (z. B. http://unraid:8765).")
    if not token:
        raise HelferConfigError(f"{path}: 'token' fehlt – im Labeltool unter Einstellungen > Helfer erzeugen.")
    pfade = raw.get("pfade") or {}
    if not isinstance(pfade, dict):
        raise HelferConfigError(f"{path}: 'pfade' muss eine Zuordnung Server-Pfad: Windows-Pfad sein.")
    base = path.parent
    return HelferConfig(server=server, token=token, drucker=str(raw.get("drucker") or "").strip(),
                        pfade={str(k): str(v) for k, v in pfade.items()},
                        cache_dir=_folder(raw.get("cache_ordner") or ".helfer-cache", base),
                        status_interval=float(raw.get("status_intervall") or 10.0),
                        vorlagen_ordner=_template_folder(raw.get("vorlagen_ordner"), base),
                        drucker_modell=str(raw.get("drucker_modell") or "").strip(),
                        csv_dir=_folder(raw.get("csv_ordner"), base) or default_csv_dir(base),
                        sync_interval=float(raw.get("abgleich_intervall") or 10.0))


def ws_url(server: str) -> str:
    """http(s)://host:port[/pfad] -> ws(s)://host:port[/pfad]/api/helfer/ws"""
    s = server.strip().rstrip("/")
    if "://" not in s:
        s = "http://" + s
    scheme, rest = s.split("://", 1)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(scheme.lower())
    if not scheme or not rest:
        raise HelferConfigError(f"Server-Adresse '{server}' verstehe ich nicht – Beispiel: http://unraid:8765")
    host, _, base = rest.partition("/")
    base = base.strip("/")
    if not base.endswith(WS_PATH):
        base = f"{base}/{WS_PATH}" if base else WS_PATH
    return f"{scheme}://{host}/{base}"


def server_basis(server: str) -> str:
    """http(s)://host:port[/pfad] ohne abschließenden / (ws(s):// wird zu http(s)://)."""
    s = server.strip().rstrip("/")
    if "://" not in s:
        s = "http://" + s
    scheme, rest = s.split("://", 1)
    scheme = {"http": "http", "https": "https", "ws": "http", "wss": "https"}.get(scheme.lower())
    host, _, base = rest.partition("/")
    if not scheme or not host:
        raise HelferConfigError(f"Server-Adresse '{server}' verstehe ich nicht – Beispiel: http://unraid:8765")
    base = base.strip("/")
    if base.endswith(WS_PATH):
        base = base[: -len(WS_PATH)].strip("/")
    return f"{scheme}://{host}" + (f"/{base}" if base else "")


# ------------------------------------------------------------------ Update per Knopf
APP_ID = "{8C1E5B4F-3A7D-4E2B-9F61-2D7C5A0B9E43}"   # = AppId in installer\labeltool-helfer.iss


def update_ordner() -> Path:
    import tempfile
    return Path(tempfile.gettempdir()) / UPDATE_ORDNER


def installation_pro_benutzer(ordner: Path | None = None) -> bool:
    """Wurde dieser Programmordner nur für den aktuellen Benutzer installiert (/CURRENTUSER)?

    Dann braucht auch das Update keine Administratorrechte (und muss /CURRENTUSER bekommen, sonst
    landet eine zweite Installation unter Program Files)."""
    if sys.platform != "win32":
        return False
    import winreg
    ordner = Path(ordner or programm_ordner())
    schluessel = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, schluessel) as k:
            ort = str(winreg.QueryValueEx(k, "InstallLocation")[0] or "")
    except OSError:
        return False
    if not ort.strip():
        return False
    norm = lambda p: os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")
    return norm(ort) == norm(ordner)


def setup_ausfuehren(datei: Path, parameter: str, erhoeht: bool = True) -> None:
    """Setup per ShellExecuteEx starten – erhoeht=True mit „runas“ (Windows-Administratorabfrage).

    Fehler -> OSError mit .winerror (1223 = Abfrage abgelehnt)."""
    if sys.platform != "win32":
        raise UpdateFehler("Update per Knopf geht nur unter Windows.")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    fn = shell32.ShellExecuteExW
    fn.restype, fn.argtypes = wintypes.BOOL, (ctypes.POINTER(SHELLEXECUTEINFOW),)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100 | 0x400          # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI
    info.lpVerb = "runas" if erhoeht else "open"
    info.lpFile = str(datei)
    info.lpParameters = parameter
    info.lpDirectory = str(Path(datei).parent)
    info.nShow = 1                              # SW_SHOWNORMAL (Fortschrittsfenster bei /SILENT)
    if not fn(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.hProcess:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)


class _KeineUmleitung(urllib.request.HTTPRedirectHandler):
    """Den Token nie an eine andere Adresse weiterreichen: Umleitungen = Fehler."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def setup_laden(url: str, token: str, sha: str, ziel_ordner: Path, version: str,
                timeout: float = UPDATE_TIMEOUT, max_bytes: int = UPDATE_MAX) -> Path:
    """Setup-Datei mit Token laden, sha256 prüfen, unter ziel_ordner ablegen -> Pfad. Fehler: UpdateFehler."""
    ziel_ordner = Path(ziel_ordner)
    try:
        ziel_ordner.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise UpdateFehler(f"Update-Ordner {ziel_ordner} nicht anlegbar: {e.strerror or e}")
    sicher = re.sub(r"[^0-9A-Za-z._+-]", "_", version or "")[:40] or "neu"
    ziel = ziel_ordner / f"Labeltool-Helfer-Setup-{sicher}.exe"
    teil = ziel_ordner / f".{ziel.name}.{os.getpid()}.part"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "User-Agent": f"Labeltool-Helfer/{__version__}"})
    opener = urllib.request.build_opener(_KeineUmleitung)
    h = hashlib.sha256()
    n = 0
    try:
        with opener.open(req, timeout=timeout) as resp, open(teil, "wb") as f:
            while True:
                block = resp.read(256 * 1024)
                if not block:
                    break
                n += len(block)
                if n > max_bytes:
                    raise UpdateFehler(f"Setup-Datei ist größer als {max_bytes // 2**20} MB – abgebrochen.")
                h.update(block)
                f.write(block)
    except urllib.error.HTTPError as e:
        teil.unlink(missing_ok=True)
        grund = {401: "Server hat den Helfer-Token abgelehnt", 403: "Zugriff verweigert",
                 404: "Setup-Datei auf dem Server nicht gefunden"}.get(e.code, "Server meldet einen Fehler")
        if 300 <= e.code < 400:
            grund = "Server leitet um (nicht erlaubt)"
        raise UpdateFehler(f"Download des Setups fehlgeschlagen: {grund} (HTTP {e.code}).")
    except urllib.error.URLError as e:
        teil.unlink(missing_ok=True)
        raise UpdateFehler(f"Download des Setups fehlgeschlagen: Server nicht erreichbar ({e.reason}).")
    except UpdateFehler:
        teil.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as e:      # Zeitüberschreitung, abgebrochene Verbindung, Platte voll …
        teil.unlink(missing_ok=True)
        raise UpdateFehler(f"Download des Setups fehlgeschlagen: {e or type(e).__name__}.")
    if h.hexdigest() != sha:
        teil.unlink(missing_ok=True)
        raise UpdateFehler("Prüfsumme der heruntergeladenen Setup-Datei passt nicht (sha256) – "
                           "Download beschädigt, Update abgebrochen.")
    try:
        os.replace(teil, ziel)
    except OSError:                          # alte Datei gesperrt (Setup läuft noch?) -> eigener Name
        ziel = ziel.with_name(f"{ziel.stem}-{int(time.time())}.exe")
        os.replace(teil, ziel)
    for alt in ziel_ordner.iterdir():        # ältere Downloads aufräumen (gesperrte bleiben liegen)
        if alt != ziel and alt.suffix.lower() in (".exe", ".part"):
            try:
                alt.unlink()
            except OSError:
                pass
    log.info("Setup geladen: %s (%d Bytes, sha256 ok)", ziel, n)
    return ziel


def printer_for_model(model: str, names: list[str]) -> str | None:
    """Installierten Drucker zum Modell finden ("PT-P710BT" -> "Brother PT-P710BT").

    Das Modell muss als eigenes Wort vorkommen (PT-P710 passt nicht auf PT-P710BT).
    """
    model = (model or "").strip()
    if not model:
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    hits = [str(n) for n in names or [] if n and pattern.search(str(n))]
    return min(hits, key=len) if hits else None   # "Brother PT-P710BT" vor "... (Kopie 1)"


# ------------------------------------------------------------------ Pfade (alt)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathMapper:
    """Server-Pfad -> Windows-Pfad über die Präfix-Zuordnung `pfade` (längster Präfix gewinnt).

    Ohne Zuordnung werden Pfade unverändert genommen (Server und Helfer auf demselben PC).
    """

    def __init__(self, mapping: dict[str, str]):
        self.items = sorted(((self._norm(k), v) for k, v in (mapping or {}).items()),
                            key=lambda kv: len(kv[0]), reverse=True)

    @staticmethod
    def _norm(p: str) -> str:
        p = str(p).replace("\\", "/")
        return p.rstrip("/") or "/"

    @staticmethod
    def _rest(path: str, prefix: str) -> str | None:
        a, b = (path.casefold(), prefix.casefold()) if _DRIVE_RE.match(prefix) else (path, prefix)
        if prefix == "/":
            return path[1:] if a.startswith("/") else None
        if a == b:
            return ""
        if a.startswith(b + "/"):
            return path[len(prefix) + 1:]
        return None

    def translate(self, server_path: str) -> Path:
        if not self.items:
            return Path(server_path)
        s = self._norm(server_path)
        for prefix, target in self.items:
            rest = self._rest(s, prefix)
            if rest is None:
                continue
            parts = [p for p in rest.split("/") if p not in ("", ".")]
            if ".." in parts:
                raise PathError(f"Server-Pfad '{server_path}' enthält '..' – abgelehnt.")
            return Path(target).joinpath(*parts)
        known = ", ".join(p for p, _ in self.items)
        raise PathError(f"Server-Pfad '{server_path}' ist in helfer.yaml unter 'pfade' nicht zugeordnet "
                        f"(zugeordnet: {known}). Besser: 'vorlagen_ordner' einrichten ({einrichten_hinweis()}).")


# ------------------------------------------------------------------ Vorlagen-Cache
class TemplateCache:
    """Vorlagen nach sha256 – jede Fassung liegt als <cache>/<sha[:16]>/<Name>.lbx."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def get(self, tpl: dict) -> Path:
        if not isinstance(tpl, dict):
            raise TemplateError("Auftrag ohne Vorlage.")
        name = str(tpl.get("name") or "")
        sha = str(tpl.get("sha256") or "").lower()
        if not valid_name(name):
            raise TemplateError(f"Ungültiger Vorlagenname '{name}'.")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise TemplateError("Vorlage ohne gültige Prüfsumme (sha256).")
        target = self.folder / sha[:16] / name
        if tpl.get("daten") is not None:
            data = _b64(tpl["daten"], name)
            if sha256(data) != sha:
                raise TemplateError(f"{name}: Prüfsumme passt nicht – Übertragung beschädigt.")
            if not (target.is_file() and target.read_bytes() == data):
                write_atomic(target, data)
                self.prune()
            return target
        if target.is_file() and sha256(target.read_bytes()) == sha:
            return target
        raise CacheMiss(name)

    def prune(self, keep: int = CACHE_KEEP) -> None:
        try:
            dirs = sorted((d for d in self.folder.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
            for d in dirs[keep:]:
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
                d.rmdir()
        except OSError:
            pass


def _b64(text, what: str) -> bytes:
    if not isinstance(text, str) or len(text) > MAX_FILE * 4 // 3 + 8:
        raise TemplateError(f"{what}: fehlt oder ist größer als {MAX_FILE // 2**20} MB.")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise TemplateError(f"{what} wurde beschädigt übertragen.")


# ------------------------------------------------------------------ Aufträge
class HelferKern:
    """Führt Aufträge aus (blockierend, ohne Netzwerk) – im Test direkt aufrufbar.

    on_change: wird nach eigenen Änderungen im Vorlagen-Ordner aufgerufen (-> Manifest sofort).
    """

    def __init__(self, cfg: HelferConfig, printer=bpac, desktop=None, state_fn=printer_state,
                 opener=open_file, hostname: str | None = None, installiert: bool | None = None,
                 starter=None, update_dir: Path | None = None, pro_benutzer=None, queue_fn=print_queue):
        self.cfg = cfg
        self.queue_fn = queue_fn
        self.letzter_drucker = ""                # Drucker des letzten Auftrags (für die Warteschlange)
        self.installiert = gepackt() if installiert is None else bool(installiert)
        self.starter = starter or setup_ausfuehren   # (datei, parameter, erhoeht) – im Test eine Attrappe
        self.update_dir = Path(update_dir) if update_dir else None
        self.pro_benutzer = pro_benutzer or installation_pro_benutzer
        self.konfig_pfad: Path | None = None     # setzt run_forever (für den Neustart nach dem Update)
        self.update_gestartet = False            # True: Setup läuft -> Helfer beendet sich nach der Antwort
        self._update_lock = threading.Lock()
        self.printer = printer
        self.desktop = desktop or LocalDesktop(opener=opener)   # verknüpfte Kopien neben der CSV
        self.state_fn = state_fn
        self.paths = PathMapper(cfg.pfade)
        self.cache = TemplateCache(cfg.cache_dir)
        self.hostname = hostname or socket.gethostname()
        self.folder = Path(cfg.vorlagen_ordner) if cfg.vorlagen_ordner else None
        self.csv_dir = Path(cfg.csv_dir) if cfg.csv_dir else Path(cfg.cache_dir).parent / ".helfer-csv"
        self.on_change = None
        self._hashes: dict[str, tuple[int, int, str]] = {}    # name -> (Größe, mtime_ns, sha256)
        self._own: dict[str, tuple[int, int]] = {}            # selbst geschrieben -> sofort stabil
        self._lock = threading.Lock()

    # ----------------------------------------------------------- Status
    def printer_name(self, names: list[str]) -> str:
        return (self.cfg.drucker or printer_for_model(self.cfg.drucker_modell, names)
                or default_printer(names) or "")

    def info(self) -> dict:
        """Inhalt von hallo/status: Rechner, Version, b-PAC, Drucker, Modell und Vorlagen-Ordner."""
        try:
            st = self.printer.status(self.cfg.drucker)
        except Exception as e:
            st = {"ok": False, "grund": f"b-PAC-Status nicht lesbar: {e}", "drucker": []}
        names = [str(n) for n in st.get("drucker") or []]
        try:
            avail = bool(self.printer.preview_available())
        except Exception:
            avail = False
        states = {}
        for name in names:
            try:
                s = self.state_fn(name)
            except Exception as e:
                s = {"angeschlossen": None, "status_text": f"Status nicht lesbar: {e}"}
            states[name] = {"angeschlossen": s.get("angeschlossen"), "status_text": s.get("status_text", ""),
                            "band": s.get("band")}
        return {"protokoll": PROTOKOLL, "sammeldruck": True, "warteschlange": True, "rechner": self.hostname, "version": __version__,
                "installiert": self.installiert, "bpac": avail,
                "bpac_grund": "" if avail else (st.get("grund") or "b-PAC nicht verfügbar"),
                "drucker_liste": names, "drucker": self.printer_name(names), "drucker_status": states,
                "drucker_modell": self.cfg.drucker_modell or None,
                "vorlagen_ordner": str(self.folder) if self.folder else None}

    # ----------------------------------------------------------- Vorlagen-Ordner
    def _require_folder(self) -> Path:
        if self.folder is None:
            raise TemplateError(f"Auf diesem PC ist kein Vorlagen-Ordner eingerichtet ({einrichten_hinweis()}).")
        if not self.folder.is_dir():
            raise TemplateError(f"Vorlagen-Ordner {self.folder} gibt es nicht (mehr).")
        return self.folder

    def _template(self, name) -> Path:
        if not valid_name(name):
            raise TemplateError(f"Ungültiger Vorlagenname '{name}' – nur ein Dateiname mit Endung .lbx.")
        return self._require_folder() / name

    def _read_stable(self, path: Path) -> tuple[bytes, str]:
        """Inhalt + sha256 einer fertig gespeicherten Vorlage, sonst NotReady/TemplateError."""
        try:
            st = path.stat()
        except FileNotFoundError:
            raise TemplateError(f"{path.name} gibt es im Vorlagen-Ordner nicht (mehr).")
        if st.st_size > MAX_FILE:
            raise TemplateError(f"zu groß ({st.st_size / 2**20:.1f} MB, höchstens {MAX_FILE // 2**20} MB)")
        key = (st.st_size, st.st_mtime_ns)
        with self._lock:
            own = self._own.get(path.name) == key
        if not own and time.time() - st.st_mtime < STABLE_AGE:
            raise NotReady(UNREADY)
        try:
            data = path.read_bytes()
            after = path.stat()
        except PermissionError:
            raise NotReady("gesperrt – nächster Versuch gleich")
        if (after.st_size, after.st_mtime_ns) != key or len(data) != st.st_size:
            raise NotReady(UNREADY)
        if not lbx_ok(data):
            if time.time() - st.st_mtime < 60:
                raise NotReady(UNREADY)
            raise TemplateError("keine gültige P-touch-Vorlage (ZIP beschädigt)")
        sha = sha256(data)
        with self._lock:
            self._hashes[path.name] = (*key, sha)
        return data, sha

    def manifest(self) -> dict:
        """{"typ": "manifest", "ordner", "dateien": {name: sha256} | None, "uebersprungen", "fehler"}"""
        out = {"typ": "manifest", "ordner": str(self.folder) if self.folder else None,
               "dateien": None, "uebersprungen": {}, "fehler": None}
        if self.folder is None:
            return out
        try:
            entries = [p for p in self.folder.iterdir() if p.suffix.lower() == ".lbx" and p.is_file()]
        except OSError as e:
            out["fehler"] = f"Vorlagen-Ordner {self.folder} nicht lesbar: {e.strerror or e}"
            return out
        files, skipped = {}, {}
        for p in sorted(entries, key=lambda p: p.name.lower()):
            if p.name.startswith("."):      # versteckt/Zwischendatei
                continue
            if not valid_name(p.name):
                skipped[p.name] = "ungültiger Dateiname"
                continue
            try:
                st = p.stat()
                with self._lock:
                    hit = self._hashes.get(p.name)
                if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
                    files[p.name] = hit[2]
                else:
                    files[p.name] = self._read_stable(p)[1]
            except NotReady as e:
                skipped[p.name] = str(e)
            except (TemplateError, OSError) as e:
                skipped[p.name] = str(e)
        with self._lock:   # Einträge gelöschter Dateien vergessen
            for name in set(self._hashes) - set(files):
                self._hashes.pop(name, None)
        out["dateien"], out["uebersprungen"] = files, skipped
        return out

    def _note_own(self, path: Path) -> None:
        st = path.stat()
        with self._lock:
            self._own[path.name] = (st.st_size, st.st_mtime_ns)
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # pragma: no cover
                pass

    def _write_csv(self, spec) -> Path:
        if not isinstance(spec, dict) or not valid_name(spec.get("name"), "csv"):
            raise TemplateError("Ungültiger CSV-Name – nur ein Dateiname mit Endung .csv.")
        data = _b64(spec.get("inhalt"), spec["name"])
        target = self.csv_dir / spec["name"]
        if not (target.is_file() and target.read_bytes() == data):
            write_atomic(target, data)
        return target

    # ----------------------------------------------------------- Nachrichten
    def handle(self, msg: dict) -> dict | None:
        typ = msg.get("typ")
        if typ == "ping":
            return {"typ": "pong"}
        if typ == "willkommen":
            log.info("Am Server angemeldet (Server-Version %s).", msg.get("server_version", "?"))
            return None
        rid = msg.get("id")
        if rid is None:
            return None
        job = getattr(self, f"_job_{typ}", None) if isinstance(typ, str) else None
        if job is None:
            return {"typ": "antwort", "id": rid, "ok": False, "fehler": f"Unbekannter Auftrag '{typ}' – Helfer aktualisieren?"}
        try:
            return {"typ": "antwort", "id": rid, "ok": True, "daten": job(msg) or {}}
        except CacheMiss as e:
            log.info("Vorlage %s nicht im Cache – Server schickt sie erneut.", e)
            return {"typ": "antwort", "id": rid, "ok": False, "code": "vorlage_fehlt", "fehler": f"Vorlage {e} fehlt im Cache."}
        except NotReady as e:
            return {"typ": "antwort", "id": rid, "ok": False, "code": "unfertig", "fehler": str(e)}
        except (bpac.BpacError, TemplateError, PathError, UpdateFehler, OSError, ValueError, KeyError, TypeError) as e:
            text = str(e) or type(e).__name__
            log.warning("Auftrag %s fehlgeschlagen: %s", typ, text)
            return {"typ": "antwort", "id": rid, "ok": False, "fehler": text}
        except Exception as e:  # pragma: no cover – nie den Helfer abstürzen lassen
            log.exception("Unerwarteter Fehler bei Auftrag %s", typ)
            return {"typ": "antwort", "id": rid, "ok": False, "fehler": f"Unerwarteter Fehler im Helfer: {e}"}

    def _file(self, server_path: str, what: str) -> Path:
        path = self.paths.translate(server_path)
        if not path.exists():
            raise PathError(f"{what} '{path}' (Server: {server_path}) gibt es auf diesem PC nicht – "
                            f"Vorlagen-Ordner einrichten ({einrichten_hinweis()}) oder 'pfade' in helfer.yaml prüfen.")
        return path

    def _csv(self, msg: dict) -> Path:
        spec = msg.get("csv")
        if isinstance(spec, dict):
            return self._write_csv(spec)
        return self._file(str(spec or ""), "CSV")       # Protokoll 1: Server-Pfad

    def _job_vorschau(self, msg: dict) -> dict:
        path = self.cache.get(msg.get("vorlage"))
        log.info("Vorschau: %s", path.name)
        dpi = msg.get("dpi")
        extra = {"dpi": min(max(int(dpi), 90), 720)} if isinstance(dpi, int) and dpi > 0 else {}
        png = self.printer.render_png(path, dict(msg.get("zeile") or {}), **extra)
        return {"png": base64.b64encode(png).decode("ascii")}

    def _job_drucken_sammel(self, msg: dict) -> dict:
        """Mehrere Vorlagen in einem Druckauftrag (ein Vorlauf, Autocut dazwischen)."""
        teile = msg.get("teile") or []
        if not isinstance(teile, list) or not teile:
            raise ValueError("Sammeldruck ohne Teile.")
        jobs = [(self.cache.get(t.get("vorlage")), list(t.get("zeilen") or [])) for t in teile]
        name = self._druckername(str(msg.get("drucker") or ""))
        anzahl = sum(len(r) for _, r in jobs)
        log.info("Drucke %d Label(s) aus %d Vorlage(n) in einem Auftrag …", anzahl, len(jobs))
        batch = getattr(self.printer, "print_batch", None)
        if batch is None:
            n = sum(self.printer.print_labels(p, r, name, int(msg.get("flags") or 0)) for p, r in jobs)
        else:
            n = batch(jobs, name, int(msg.get("flags") or 0))
        log.info("Gedruckt: %d Label(s).", n)
        return {"gedruckt": n}

    def _druckername(self, name: str) -> str:
        """Drucker für den Auftrag wählen und prüfen, dass er verbunden ist (sonst nichts senden)."""
        name = name or self.cfg.drucker
        names = None
        if not name and self.cfg.drucker_modell:   # Drucker passend zum Modell
            names = self.printer.status("").get("drucker") or []
            name = printer_for_model(self.cfg.drucker_modell, names) or ""
        check = name
        if not check:   # Drucker aus der Vorlage – b-PAC nimmt dann den (einzigen) P-touch
            check = default_printer(names if names is not None else self.printer.status("").get("drucker") or [])
        require_online(check, self.state_fn)
        self.letzter_drucker = check or ""
        return name

    def _job_warteschlange(self, msg: dict) -> dict:
        """Windows-Warteschlange des Druckers – nach dem Senden, um Fehler wie „Band leer“ zu sehen."""
        name = str(msg.get("drucker") or "") or self.cfg.drucker or self.letzter_drucker
        if not name:
            name = self.printer_name(self.printer.status("").get("drucker") or [])
        return self.queue_fn(name) or {}

    def _job_drucken(self, msg: dict) -> dict:
        path = self.cache.get(msg.get("vorlage"))
        rows = list(msg.get("zeilen") or [])
        name = str(msg.get("drucker") or "") or self.cfg.drucker
        names = None
        if not name and self.cfg.drucker_modell:   # Drucker passend zum Modell
            names = self.printer.status("").get("drucker") or []
            name = printer_for_model(self.cfg.drucker_modell, names) or ""
        check = name
        if not check:   # Drucker aus der Vorlage – b-PAC nimmt dann den (einzigen) P-touch
            check = default_printer(names if names is not None else self.printer.status("").get("drucker") or [])
        require_online(check, self.state_fn)
        self.letzter_drucker = check or ""
        log.info("Drucke %d Label(s) mit %s auf %s …", len(rows), path.name, name or check or "Drucker aus der Vorlage")
        n = self.printer.print_labels(path, rows, name, int(msg.get("flags") or 0))
        log.info("Gedruckt: %d Label(s).", n)
        return {"gedruckt": n}

    def _job_oeffnen(self, msg: dict) -> dict:
        path = self.cache.get(msg.get("vorlage"))
        csv_path = self._csv(msg)
        linked = self.desktop.open_linked(path, csv_path)
        log.info("P-touch Editor: %s (verknüpft mit %s)", linked, csv_path)
        return {"vorlage": str(linked), "csv": str(csv_path)}

    def _job_bearbeiten(self, msg: dict) -> dict:
        csv_path = self._csv(msg)
        backup = None
        if self.folder is not None:      # Original im Vorlagen-Ordner dieses PCs
            name = msg.get("name") or Path(str(msg.get("pfad") or "")).name
            tpl = self._template(name)
            if not tpl.is_file():
                raise TemplateError(f"{name} gibt es im Vorlagen-Ordner {self.folder} nicht – Abgleich abwarten.")
            backup = backup_once(self.folder, tpl)
            try:
                self.desktop.edit_original(tpl, csv_path)
            finally:
                self._note_own(tpl)
        else:                            # alt: Server-Vorlage über 'pfade'
            tpl = self._file(str(msg.get("pfad") or ""), "Vorlage")
            self.desktop.edit_original(tpl, csv_path)
        log.info("Bearbeiten im P-touch Editor: %s (Beispieldaten %s)", tpl, csv_path)
        return {"vorlage": str(tpl), "csv": str(csv_path), "backup": str(backup) if backup else None}

    def _job_ordner(self, msg: dict) -> dict:
        if "dateien" in msg:             # CSV-Inhalte -> lokaler CSV-Ordner
            for spec in msg.get("dateien") or []:
                self._write_csv(spec)
            folder = self.csv_dir
            folder.mkdir(parents=True, exist_ok=True)
        else:
            folder = self._file(str(msg.get("pfad") or ""), "Ordner")
        self.desktop.open_folder(folder)
        log.info("Ordner geöffnet: %s", folder)
        return {"ordner": str(folder)}

    def _job_datei(self, msg: dict) -> dict:
        path = self._template(msg.get("name"))
        data, sha = self._read_stable(path)
        return {"name": path.name, "sha256": sha, "daten": base64.b64encode(data).decode("ascii")}

    def _job_aktualisieren(self, msg: dict) -> dict:
        """Setup vom Server laden, prüfen und mit Administratorrechten starten (Update per Knopf)."""
        if not self.installiert:
            raise UpdateFehler("Dieser Druck-Helfer läuft aus dem Projekt bzw. ZIP-Paket (Python) und kann nicht "
                               "per Knopf aktualisiert werden – bitte einmalig den Windows-Installer verwenden "
                               "(Einstellungen > Druck-Helfer > Installer herunterladen).")
        pfad = str(msg.get("pfad") or "")
        if not pfad.startswith("/") or pfad.startswith("//") or "://" in pfad or "\\" in pfad:
            raise UpdateFehler(f"Ungültiger Download-Pfad '{pfad}'.")     # Token nur an den eigenen Server
        sha = str(msg.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise UpdateFehler("Auftrag ohne gültige Prüfsumme (sha256) – Update abgebrochen.")
        version = str(msg.get("version") or "").strip()
        if not self._update_lock.acquire(blocking=False):
            raise UpdateFehler("Ein Update läuft bereits.")
        try:
            if self.update_gestartet:
                raise UpdateFehler("Das Update wurde bereits gestartet.")
            url = server_basis(self.cfg.server) + pfad
            log.info("Update auf Version %s: lade %s …", version or "?", url)
            datei = setup_laden(url, self.cfg.token, sha, self.update_dir or update_ordner(), version)
            parameter = UPDATE_PARAMETER
            pro_benutzer = bool(self.pro_benutzer())
            if pro_benutzer:
                parameter += " /CURRENTUSER"        # Installation nur für diesen Benutzer: ohne Admin
            if self.konfig_pfad is not None and \
                    Path(self.konfig_pfad).resolve() != standard_konfig(True).resolve():
                parameter += f' "/KONFIG={Path(self.konfig_pfad).resolve()}"'
            log.info("Starte Setup %s %s%s", datei, parameter,
                     "" if pro_benutzer else " (Windows fragt nach Administratorrechten)")
            try:
                self.starter(datei, parameter, not pro_benutzer)
            except OSError as e:
                if getattr(e, "winerror", None) == ERROR_CANCELLED:
                    raise UpdateFehler("Die Windows-Administratorabfrage wurde abgelehnt – Update abgebrochen, "
                                       "der Helfer läuft mit der bisherigen Version weiter.")
                raise UpdateFehler(f"Setup konnte nicht gestartet werden: {e.strerror or e}")
            self.update_gestartet = True
            log.info("Setup gestartet – der Helfer beendet sich und wird nach der Installation neu gestartet.")
            return {"version": version or None, "datei": str(datei)}
        finally:
            self._update_lock.release()

    def _job_vorlage_speichern(self, msg: dict) -> dict:
        path = self._template(msg.get("name"))
        data = _b64(msg.get("daten"), path.name)
        if sha256(data) != str(msg.get("sha256") or "").lower():
            raise TemplateError(f"{path.name}: Prüfsumme passt nicht – Übertragung beschädigt.")
        if len(data) > MAX_FILE or not lbx_ok(data):
            raise TemplateError(f"{path.name} ist keine gültige P-touch-Vorlage.")
        if path.exists():
            if path.read_bytes() == data:
                return {"name": path.name, "sha256": sha256(data), "pfad": str(path)}
            if not msg.get("ueberschreiben"):
                raise TemplateError(f"Vorlage '{path.name}' gibt es im Vorlagen-Ordner schon – anderen Namen wählen.")
        write_atomic(path, data)
        self._note_own(path)
        log.info("Neue Vorlage vom Server im Vorlagen-Ordner: %s", path)
        return {"name": path.name, "sha256": sha256(data), "pfad": str(path)}


# ------------------------------------------------------------------ Verbindung
async def _session(ws, kern: HelferKern, cfg: HelferConfig) -> None:
    loop = asyncio.get_running_loop()
    jobs = ThreadPoolExecutor(1, thread_name_prefix="auftrag")     # b-PAC: Aufträge nacheinander
    files = ThreadPoolExecutor(1, thread_name_prefix="abgleich")   # Dateiarbeit, unabhängig von b-PAC
    probe = ThreadPoolExecutor(1, thread_name_prefix="status")
    upd = ThreadPoolExecutor(1, thread_name_prefix="update")       # Download + Administratorabfrage
    spool = ThreadPoolExecutor(1, thread_name_prefix="warteschlange")
    send_lock = asyncio.Lock()
    sync_now = asyncio.Event()

    async def send(msg: dict) -> None:
        async with send_lock:
            await ws.send(json.dumps(msg, ensure_ascii=False))

    info = await loop.run_in_executor(probe, kern.info)
    await send(dict(info, typ="hallo", token=cfg.token))
    _log_info(info)
    kern.on_change = lambda: loop.call_soon_threadsafe(sync_now.set)

    async def status_loop() -> None:
        last, last_sent = info, time.monotonic()
        while True:
            await asyncio.sleep(STATUS_CHECK)
            try:
                now_info = await loop.run_in_executor(probe, kern.info)
            except Exception as e:  # pragma: no cover
                log.warning("Status konnte nicht ermittelt werden: %s", e)
                continue
            if now_info != last or time.monotonic() - last_sent >= cfg.status_interval:
                if now_info != last:
                    _log_info(now_info)
                await send(dict(now_info, typ="status"))
                last, last_sent = now_info, time.monotonic()

    async def manifest_loop() -> None:
        if kern.folder is None:
            log.warning("Kein Vorlagen-Ordner eingerichtet – der Server nutzt seine eigenen Vorlagen "
                        f"({einrichten_hinweis()} zum Einrichten).")
            return
        last = None
        while True:
            sync_now.clear()
            try:
                m = await loop.run_in_executor(files, kern.manifest)
            except Exception as e:  # pragma: no cover
                log.warning("Vorlagen-Ordner konnte nicht gelesen werden: %s", e)
                m = None
            if m is not None:
                _log_manifest(m, last)
                last = m
                await send(m)
            try:
                await asyncio.wait_for(sync_now.wait(), cfg.sync_interval)
            except asyncio.TimeoutError:
                pass

    async def run_job(msg: dict) -> None:
        typ = msg.get("typ")
        pool = upd if typ == UPDATE_JOB else files if typ in FILE_JOBS else spool if typ == QUEUE_JOB else jobs
        answer = await loop.run_in_executor(pool, kern.handle, msg)
        if answer is not None:
            await send(answer)
        if typ == UPDATE_JOB and getattr(kern, "update_gestartet", False):
            await asyncio.sleep(UPDATE_PAUSE)        # Antwort sicher beim Server, dann sauber trennen
            await ws.close(1000, "Helfer wird aktualisiert")

    tasks: set[asyncio.Task] = set()
    background = [asyncio.create_task(status_loop()), asyncio.create_task(manifest_loop())]
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("typ") in ("ping", "willkommen"):
                answer = kern.handle(msg)
                if answer is not None:
                    await send(answer)
                continue
            task = asyncio.create_task(run_job(msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        kern.on_change = None
        for t in background + list(tasks):
            t.cancel()
        for pool in (jobs, files, probe, upd, spool):
            pool.shutdown(wait=False, cancel_futures=True)


def _log_info(info: dict) -> None:
    if not info.get("bpac"):
        log.warning("b-PAC: %s", info.get("bpac_grund") or "nicht verfügbar")
    for name, st in (info.get("drucker_status") or {}).items():
        mark = {True: "verbunden", False: "NICHT verbunden", None: "unbekannt"}[st.get("angeschlossen")]
        log.info("Drucker %s: %s (%s)", name, mark, st.get("status_text") or "-")
    if info.get("drucker_modell") and not info.get("drucker"):
        log.warning("Kein installierter Drucker passt zum Modell %s.", info["drucker_modell"])


def _log_manifest(m: dict, last: dict | None) -> None:
    if m.get("fehler"):
        if not last or last.get("fehler") != m["fehler"]:
            log.error("%s", m["fehler"])
        return
    files = m.get("dateien") or {}
    names = set(files) | set(m.get("uebersprungen") or {})
    if not last or names != set(last.get("dateien") or {}) | set(last.get("uebersprungen") or {}):
        log.info("Vorlagen-Ordner %s: %d Vorlage(n).", m.get("ordner"), len(names))
    old = (last or {}).get("uebersprungen") or {}
    for name, why in (m.get("uebersprungen") or {}).items():
        if old.get(name) != why and why != UNREADY:
            log.warning("Vorlage %s wird nicht abgeglichen: %s", name, why)


async def run_forever(config_path: Path, kern_factory=HelferKern) -> bool:
    """Verbinden, bei Abbruch mit Backoff neu verbinden. Liest helfer.yaml bei jedem Versuch neu.

    Endet nur nach einem gestarteten Update (-> True); der Installer startet den Helfer danach neu."""
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI
    except ImportError:
        sys.exit("Paket 'websockets' fehlt – in der venv 'pip install -r requirements.txt' ausführen.")

    backoff = 1.0
    while True:
        try:
            cfg = load_helper_config(config_path)
            url = ws_url(cfg.server)
        except HelferConfigError as e:
            log.error("%s  Neuer Versuch in 30 s.", e)
            await asyncio.sleep(30)
            continue
        kern = kern_factory(cfg)
        try:
            kern.konfig_pfad = Path(config_path)
        except AttributeError:   # pragma: no cover – Attrappen im Test
            pass
        log.info("Verbinde mit %s …", url)
        connected = False
        close_code = None
        try:
            async with connect(url, additional_headers={"Authorization": f"Bearer {cfg.token}"},
                               max_size=MAX_MESSAGE, open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                connected = True
                log.info("Verbunden – warte auf Aufträge.")
                try:
                    await _session(ws, kern, cfg)
                except ConnectionClosed:
                    pass
                close_code = ws.close_code
                reason = ws.close_reason or ""
        except InvalidStatus as e:
            code = getattr(getattr(e, "response", None), "status_code", "?")
            log.error("Server hat die Verbindung abgelehnt (HTTP %s) – ist die Adresse richtig und der Server aktuell?", code)
        except (InvalidURI, InvalidHandshake) as e:
            log.error("Verbindungsaufbau fehlgeschlagen: %s", e)
        except (OSError, asyncio.TimeoutError) as e:
            log.error("Server %s nicht erreichbar: %s", cfg.server, e or type(e).__name__)
        else:
            text = CLOSE_TEXT.get(close_code) or (f"Verbindung getrennt (Code {close_code}{': ' + reason if reason else ''}).")
            if not getattr(kern, "update_gestartet", False):
                (log.error if close_code in (4401, 4400) else log.warning)(text)

        if getattr(kern, "update_gestartet", False):
            log.info("Update gestartet – Helfer beendet sich.")
            return True
        if connected and close_code not in (4401, 4000, 4400):
            backoff = 1.0
        elif close_code == 4401:
            backoff = max(backoff, 30.0)   # nicht im Sekundentakt gegen einen falschen Token anrennen
        elif close_code == 4000:
            backoff = REPLACED_WAIT        # zwei Helfer sollen sich nicht ständig gegenseitig verdrängen
        log.info("Neuer Verbindungsversuch in %d s …", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX) if backoff < BACKOFF_MAX else backoff


HILFE = """\
Labeltool-Druck-Helfer {version}

Aufruf: python -m labeltool.helfer [helfer.yaml]      (installiert: LabeltoolHelfer.exe [helfer.yaml])
        … --beenden                 laufenden Helfer zu dieser helfer.yaml beenden
        … --diagnose AUSGABE.json [--vorlage X.lbx]   Selbsttest ohne Druck (b-PAC, Drucker,
                                    Modellliste, Vorschau als PNG neben AUSGABE.json)
        … --log DATEI               Protokoll zusätzlich in DATEI (1 MB × 3)

Verbindet diesen Windows-PC (b-PAC, P-touch Editor, Etikettendrucker) mit dem Labeltool-Server
und gleicht den Vorlagen-Ordner ab. Ohne Angabe wird helfer.yaml im aktuellen Ordner gelesen,
installiert in %LOCALAPPDATA%\\Labeltool-Helfer\\ (dort auch helfer.log).
Einrichten: {einrichten}. Beenden: Strg+C bzw. --beenden."""


def _optionen(argv: list[str]) -> tuple[dict, list[str]]:
    """Einfache Kommandozeile: --schalter bzw. --option WERT, Rest = Positionsargumente."""
    mit_wert = ("--diagnose", "--vorlage", "--log")
    opts, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in mit_wert:
            if i + 1 >= len(argv):
                raise HelferConfigError(f"{a} braucht einen Wert.")
            opts[a], i = argv[i + 1], i + 2
            continue
        if a.startswith("--"):
            opts[a] = True
        else:
            rest.append(a)
        i += 1
    return opts, rest


async def _laufen(path: Path, instanz: Einzelinstanz) -> bool:
    """run_forever, bis das Ende-Ereignis kommt (-> True) bzw. Strg+C."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run_forever(path))
    angefordert = threading.Event()

    def waechter():
        if instanz.warte_ende():
            angefordert.set()
            loop.call_soon_threadsafe(task.cancel)
    threading.Thread(target=waechter, name="ende", daemon=True).start()
    try:
        if await task:
            return True          # Update gestartet
    except asyncio.CancelledError:
        if not angefordert.is_set():
            raise
    return angefordert.is_set()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help", "/?", "-?"):
        text = HILFE.format(version=__version__, einrichten=einrichten_hinweis())
        if sys.stdout is not None:
            print(text)
        else:
            meldung(text)
        return 0
    try:
        opts, rest = _optionen(argv)
    except HelferConfigError as e:
        meldung(str(e))
        return 2
    path = Path(rest[0]) if rest else standard_konfig()
    log_datei = Path(opts["--log"]) if "--log" in opts else (path.parent / LOG_DATEI if gepackt() else None)
    nebenbei = "--beenden" in opts or "--diagnose" in opts
    if nebenbei and "--log" not in opts and log_datei is not None and not log_datei.parent.is_dir():
        log_datei = None    # Setup/Deinstallation: keinen Benutzerordner nur fürs Protokoll anlegen
    logging_einrichten(log_datei, rotation=not nebenbei)   # rotieren darf nur der laufende Helfer

    if "--beenden" in opts:
        r = helfer_beenden(path)
        log.info({None: "Kein laufender Helfer für %s.", True: "Laufender Helfer für %s beendet.",
                  False: "Laufender Helfer für %s reagiert nicht."}[r], path.resolve())
        return 1 if r is False else 0
    if "--diagnose" in opts:
        vorlage = Path(opts["--vorlage"]) if "--vorlage" in opts else None
        return 0 if diagnose(Path(opts["--diagnose"]), vorlage).get("ok") else 1

    if not path.exists():
        if gepackt():
            log.error("Konfiguration nicht gefunden: %s – der Helfer ist noch nicht eingerichtet.", path.resolve())
            if meldung("Der Labeltool Druck-Helfer ist auf diesem PC noch nicht eingerichtet.\n\n"
                       "Jetzt den Einrichtungs-Assistenten öffnen?", frage=True):
                setup_starten(path)
            return 2
        sys.exit(f"Konfiguration nicht gefunden: {path.resolve()}\n"
                 f"„helfer.bat einrichten“ ausführen (oder helfer.example.yaml nach helfer.yaml kopieren).")

    instanz = Einzelinstanz(path)
    if not instanz.erwerben():
        text = f"Der Druck-Helfer läuft bereits (Konfiguration {path.resolve()})."
        log.warning(text)
        if gepackt():
            meldung(text + "\n\nNeu starten: Startmenü → „Druck-Helfer einrichten“ → „Speichern & starten“.")
            return 0
        sys.exit(text)

    log.info("Labeltool-Druck-Helfer %s – Konfiguration %s%s", __version__, path.resolve(),
             "" if gepackt() else " (Beenden mit Strg+C)")
    try:
        cfg = load_helper_config(path)
        if cfg.vorlagen_ordner:
            log.info("Vorlagen-Ordner: %s", cfg.vorlagen_ordner)
            if cfg.pfade:
                log.info("Hinweis: 'pfade' in helfer.yaml wird mit Vorlagen-Ordner nicht mehr benötigt.")
    except HelferConfigError:
        pass   # meldet run_forever verständlich
    angefordert = False
    try:
        angefordert = asyncio.run(_laufen(path, instanz))
    except KeyboardInterrupt:
        log.info("Helfer beendet.")
    finally:
        instanz.freigeben()
    if angefordert:
        log.info("Helfer beendet (angefordert von Einrichtung/Update).")
        logging.shutdown()
        os._exit(0)    # ein hängender b-PAC-Aufruf soll das Beenden nicht aufhalten
    return 0


if __name__ == "__main__":
    sys.exit(main())
