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
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import __version__, bpac
from .desktop import (MAX_FILE, LocalDesktop, backup_once, default_printer, lbx_ok, printer_state,
                      require_online, sha256, valid_name, write_atomic)
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
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "Labeltool-Helfer" / "csv" if local else base / ".helfer-csv"


def _folder(raw, base: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    return p if p.is_absolute() else base / p


def load_helper_config(path: str | Path) -> HelferConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise HelferConfigError(f"{path} nicht gefunden – „helfer.bat einrichten“ ausführen.")
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
                        vorlagen_ordner=_folder(raw.get("vorlagen_ordner"), base),
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
                        f"(zugeordnet: {known}). Besser: 'vorlagen_ordner' einrichten (helfer.bat einrichten).")


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
                 opener=open_file, hostname: str | None = None):
        self.cfg = cfg
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
            states[name] = {"angeschlossen": s.get("angeschlossen"), "status_text": s.get("status_text", "")}
        return {"protokoll": PROTOKOLL, "rechner": self.hostname, "version": __version__, "bpac": avail,
                "bpac_grund": "" if avail else (st.get("grund") or "b-PAC nicht verfügbar"),
                "drucker_liste": names, "drucker": self.printer_name(names), "drucker_status": states,
                "drucker_modell": self.cfg.drucker_modell or None,
                "vorlagen_ordner": str(self.folder) if self.folder else None}

    # ----------------------------------------------------------- Vorlagen-Ordner
    def _require_folder(self) -> Path:
        if self.folder is None:
            raise TemplateError("Auf diesem PC ist kein Vorlagen-Ordner eingerichtet (helfer.bat einrichten).")
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
        except (bpac.BpacError, TemplateError, PathError, OSError, ValueError, KeyError, TypeError) as e:
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
                            f"Vorlagen-Ordner einrichten (helfer.bat einrichten) oder 'pfade' in helfer.yaml prüfen.")
        return path

    def _csv(self, msg: dict) -> Path:
        spec = msg.get("csv")
        if isinstance(spec, dict):
            return self._write_csv(spec)
        return self._file(str(spec or ""), "CSV")       # Protokoll 1: Server-Pfad

    def _job_vorschau(self, msg: dict) -> dict:
        path = self.cache.get(msg.get("vorlage"))
        log.info("Vorschau: %s", path.name)
        png = self.printer.render_png(path, dict(msg.get("zeile") or {}))
        return {"png": base64.b64encode(png).decode("ascii")}

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
                        "(„helfer.bat einrichten“ zum Einrichten).")
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
        pool = files if msg.get("typ") in FILE_JOBS else jobs
        answer = await loop.run_in_executor(pool, kern.handle, msg)
        if answer is not None:
            await send(answer)

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
        for pool in (jobs, files, probe):
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


async def run_forever(config_path: Path, kern_factory=HelferKern) -> None:
    """Verbinden, bei Abbruch mit Backoff neu verbinden. Liest helfer.yaml bei jedem Versuch neu."""
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
            (log.error if close_code in (4401, 4400) else log.warning)(text)

        if connected and close_code not in (4401, 4000, 4400):
            backoff = 1.0
        elif close_code == 4401:
            backoff = max(backoff, 30.0)   # nicht im Sekundentakt gegen einen falschen Token anrennen
        elif close_code == 4000:
            backoff = REPLACED_WAIT        # zwei Helfer sollen sich nicht ständig gegenseitig verdrängen
        log.info("Neuer Verbindungsversuch in %d s …", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX) if backoff < BACKOFF_MAX else backoff


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help", "/?", "-?"):
        print("Labeltool-Druck-Helfer " + __version__ + "\n\n"
              "Aufruf: python -m labeltool.helfer [helfer.yaml]\n\n"
              "Verbindet diesen Windows-PC (b-PAC, P-touch Editor, Etikettendrucker) mit dem Labeltool-Server\n"
              "und gleicht den Vorlagen-Ordner ab. Ohne Angabe wird helfer.yaml im aktuellen Ordner gelesen.\n"
              "Einrichten: helfer.bat einrichten. Beenden: Strg+C.")
        return
    path = Path(argv[0]) if argv else Path("helfer.yaml")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if not path.exists():
        sys.exit(f"Konfiguration nicht gefunden: {path.resolve()}\n"
                 f"„helfer.bat einrichten“ ausführen (oder helfer.example.yaml nach helfer.yaml kopieren).")
    log.info("Labeltool-Druck-Helfer %s – Konfiguration %s (Beenden mit Strg+C)", __version__, path.resolve())
    try:
        cfg = load_helper_config(path)
        if cfg.vorlagen_ordner:
            log.info("Vorlagen-Ordner: %s", cfg.vorlagen_ordner)
            if cfg.pfade:
                log.info("Hinweis: 'pfade' in helfer.yaml wird mit Vorlagen-Ordner nicht mehr benötigt.")
    except HelferConfigError:
        pass   # meldet run_forever verständlich
    try:
        asyncio.run(run_forever(path))
    except KeyboardInterrupt:
        log.info("Helfer beendet.")


if __name__ == "__main__":
    main()
