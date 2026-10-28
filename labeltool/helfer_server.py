"""Server-Seite des Windows-Druck-Helfers (Modus `druck.modus: helfer`).

Der Helfer (labeltool/helfer.py) baut vom Windows-PC aus eine WebSocket-Verbindung zu
`/api/helfer/ws` auf und meldet sich mit `hallo` (+ Token) an. Danach schickt der Server
Aufträge (vorschau, drucken, oeffnen, bearbeiten, ordner) und wartet auf die Antwort mit
derselben `id`. Es gibt immer höchstens EINEN aktiven Helfer – ein neuer verdrängt den alten.

Die FastAPI-Routen sind synchron (laufen im Threadpool). Sie rufen `HelperHub.request()`
auf, das den Auftrag per `asyncio.run_coroutine_threadsafe` in der Event-Loop der
WebSocket-Verbindung ausführt und blockierend auf die Antwort wartet.

Außerdem hier: `LocalPrinter` (b-PAC im Prozess + Prüfung, ob der Drucker verbunden ist),
`ModePrinter`/`ModeDesktop` (wählen je nach cfg.print_mode lokal oder Helfer – Umschalten
wirkt sofort) und `helper_status()` für /api/status.

Vorlagen-Abgleich (`TemplateSync`): Hat der Helfer einen Vorlagen-Ordner, ist DER die Quelle.
Der Helfer schickt regelmäßig ein Manifest {name: sha256}; der Server holt fehlende/geänderte
Dateien (Auftrag `datei`), schreibt sie atomar nach cfg.template_dir und verschiebt Dateien,
die es auf dem PC nicht mehr gibt, nach <template_dir>/.geloescht/. PC gewinnt immer. Der
Abgleich läuft als eigener Task in der Event-Loop (Dateiarbeit in Threads) und blockiert
Vorschau/Druck nicht. Der Server ändert Vorlagen nie selbst zurück zum PC, außer bei
„Kopie als neue Vorlage“ (`vorlage_speichern`) und beim Erstbefüllen eines leeren PC-Ordners.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import hashlib
import itertools
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__, bpac
from . import routes_helfer_download as downloads
from .desktop import (MAX_FILE, LocalDesktop, default_printer, lbx_ok, printer_state, require_online,
                      sha256, valid_name, write_atomic)
from .ptouch import TemplateError

log = logging.getLogger("labeltool.helfer_server")

PROTOKOLL = 2
CLOSE_AUTH = 4401            # Token fehlt/falsch/geändert
CLOSE_REPLACED = 4000        # neuer Helfer hat sich angemeldet
CLOSE_PROTOCOL = 4400        # erste Nachricht war kein "hallo"
CLOSE_TIMEOUT = 4408         # keine Lebenszeichen mehr
AUTH_TIMEOUT = 10.0          # s bis zur hallo-Nachricht
PING_INTERVAL = 20.0         # s – Server -> Helfer {"typ": "ping"}
RECEIVE_TIMEOUT = 60.0       # s ohne Nachricht (Status kommt alle ~10 s) -> Verbindung tot
TIMEOUT_PREVIEW = 15.0
TIMEOUT_PRINT = 60.0
TIMEOUT_DESKTOP = 20.0
TIMEOUT_FILE = 30.0          # s – eine Vorlage im Abgleich holen/schicken
TIMEOUT_UPDATE = 300.0       # s – Helfer lädt das Setup (~13 MB) und startet es (inkl. Windows-Admin-Abfrage)
SETUP_PFAD = "/api/helfer/installer/setup"
STATE_TTL = 5.0              # s – Cache für den lokalen Drucker-/b-PAC-Status
PROTECT_TIME = 120.0         # s – frisch zum PC geschickte Vorlagen nicht als „gelöscht“ werten
DELETED_DIR = ".geloescht"   # im Vorlagenordner des Servers

NOT_CONNECTED = ("Der Druck-Helfer ist nicht verbunden – auf dem Windows-PC mit dem P-touch "
                 "„helfer.bat“ starten (Token unter Einstellungen > Helfer).")
NO_FOLDER = ("Auf dem Helfer-PC ist kein Vorlagen-Ordner eingerichtet – es werden die Vorlagen auf dem "
             "Server genutzt („helfer.bat einrichten“ auf dem PC).")
UPD_NO_SETUP = "Auf dem Server liegt kein Helfer-Setup vor – Update per Knopf nicht möglich."
UPD_NOT_INSTALLED = ("Der Helfer läuft ohne Installation (Python-/ZIP-Paket) – Update per Knopf geht nur mit dem "
                     "installierten Helfer. Bitte den Installer verwenden (Einstellungen > Druck-Helfer).")
OLD_HELPER = "Der Druck-Helfer ist veraltet (kein Vorlagen-Abgleich) – neue Version unter Einstellungen > Helfer laden."
INFO_KEYS = ("rechner", "version", "bpac", "bpac_grund", "drucker_liste", "drucker", "drucker_status",
             "protokoll", "drucker_modell", "vorlagen_ordner", "installiert", "sammeldruck")
_VERSION_NUM = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class HelperError(bpac.BpacError):
    """Helfer fehlt, antwortet nicht oder meldet einen Fehler (-> HTTP 400, Vorschau fällt auf Skizze zurück)."""


class HelperDesktopError(TemplateError):
    """Fehler beim Öffnen über den Helfer (-> HTTP 400 bzw. 'vorlage_fehler' beim Export)."""


def print_mode(cfg) -> str:
    return "helfer" if str(getattr(cfg, "print_mode", "lokal") or "lokal").lower() == "helfer" else "lokal"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _same(a, b: str) -> bool:
    return isinstance(a, str) and bool(b) and secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _clean_info(msg: dict) -> dict:
    info = {k: msg[k] for k in INFO_KEYS if k in msg}
    if "drucker_liste" in info:
        info["drucker_liste"] = [str(n) for n in (info["drucker_liste"] or [])]
    if "drucker_status" in info and not isinstance(info["drucker_status"], dict):
        info["drucker_status"] = {}
    return info


# ------------------------------------------------------------------ Versionen / Update
def compare_versions(a: str | None, b: str | None) -> int | None:
    """-1/0/1 für a<b, a==b, a>b. Nur `x.y.z` wird numerisch verglichen; sonst 0 bei gleich, None bei anders
    (oder wenn eine Seite unbekannt ist)."""
    if not a or not b:
        return None
    ma, mb = _VERSION_NUM.fullmatch(a.strip()), _VERSION_NUM.fullmatch(b.strip())
    if ma and mb:
        x, y = tuple(map(int, ma.groups())), tuple(map(int, mb.groups()))
        return (x > y) - (x < y)
    return 0 if a.strip() == b.strip() else None


class UpdateError(Exception):
    """Update per Knopf nicht möglich/fehlgeschlagen; `status` = HTTP-Status (400/409)."""

    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


def update_status(info: dict | None, modus: str = "helfer") -> dict:
    """"update"-Teil von helper_status()["helfer"]. info = hallo/status des Helfers (None = nicht verbunden)."""
    setup = downloads.installer_pfad()
    angeboten = downloads.setup_version(setup) if setup else None
    installiert = str(info.get("version")) if info and info.get("version") else None
    out = {"verfuegbar": False, "installiert": installiert, "angeboten": angeboten, "grund": "", "moeglich": False}
    if modus != "helfer":
        return dict(out, grund="Lokaler Modus – es ist kein Druck-Helfer im Einsatz.")
    if info is None:
        return dict(out, grund="Der Druck-Helfer ist nicht verbunden.")
    if setup is None:
        return dict(out, grund=UPD_NO_SETUP)
    cmp = compare_versions(installiert, angeboten)
    if angeboten is None:
        out["grund"] = ("Version des Setups auf dem Server unbekannt (Labeltool-Helfer-Setup.version fehlt) – "
                        "Aktualisieren ist trotzdem möglich.")
    elif installiert is None:
        out.update(verfuegbar=True, grund=f"Helfer-Version unbekannt – auf dem Server: {angeboten}.")
    elif cmp == 0:
        out["grund"] = f"Der Helfer ist aktuell ({installiert})."
    elif cmp is not None and cmp > 0:
        out["grund"] = f"Der Helfer ({installiert}) ist neuer als das Setup auf dem Server ({angeboten})."
    else:
        out.update(verfuegbar=True, grund=f"Neue Version verfügbar: installiert {installiert} → verfügbar {angeboten}.")
    if info.get("installiert") is not True:
        out["grund"] = (out["grund"] + " " if out["verfuegbar"] else "") + UPD_NOT_INSTALLED
        return out
    out["moeglich"] = True
    return out


# ------------------------------------------------------------------ Verbindung
class _Connection:
    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop, info: dict):
        self.ws, self.loop, self.info = ws, loop, info
        self.seit = _now_iso()
        self.last_seen = time.monotonic()
        self.pending: dict[str, asyncio.Future] = {}
        self.known: set[str] = set()      # sha256 der Vorlagen, die der Helfer schon im Cache hat
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.manifest: dict | None = None         # neuestes, noch nicht abgeglichenes Manifest
        self.sync_task: asyncio.Task | None = None
        self.seeded = False                       # leerer PC-Ordner wurde schon befüllt
        self.last_manifest: dict | None = None

    async def send(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))

    def fail_all(self, text: str) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(HelperError(text))
        self.pending.clear()

    async def close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.fail_all(f"Verbindung zum Druck-Helfer wurde getrennt ({reason}).")
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass


# ------------------------------------------------------------------ Vorlagen-Abgleich
class TemplateSync:
    """Gleicht cfg.template_dir mit dem Vorlagen-Ordner des Helfer-PCs ab (PC gewinnt)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._hashes: dict[str, tuple[int, int, str]] = {}   # name -> (Größe, mtime_ns, sha256)
        self.protected: dict[str, float] = {}                # name -> monotonic (zum PC geschickt)
        self.confirmed: set[str] = set()                     # Löschen bestätigt (Massen-Löschschutz)
        self.state = {"ordner": None, "rechner": None, "anzahl": 0, "abgeglichen": None, "fehler": None,
                      "uebersprungen": {}, "geholt": 0, "verschoben": 0, "runden": 0, "ausstehend": []}

    @property
    def folder(self) -> Path:
        return Path(self.cfg.template_dir)

    @property
    def known_file(self) -> Path:
        """Bekannte Helfer-Installationen (Rechner|Ordner) – neben status.json im Konfig-Ordner."""
        state = getattr(self.cfg, "state_file", None)
        base = Path(state).parent if state else self.folder.parent
        return base / "vorlagen-abgleich.json"

    def _known(self) -> set[str]:
        try:
            data = json.loads(self.known_file.read_text(encoding="utf-8"))
            return {str(k) for k in data.get("bekannt", [])}
        except (OSError, ValueError, AttributeError):
            return set()

    def _remember(self, key: str) -> None:
        known = self._known() | {key}
        try:
            write_atomic(self.known_file, json.dumps({"bekannt": sorted(known)}, ensure_ascii=False,
                                                     indent=1).encode("utf-8"))
        except (OSError, TemplateError) as e:   # nur Komfort – Abgleich läuft trotzdem
            log.warning("%s nicht schreibbar: %s", self.known_file, e)

    def confirm_delete(self) -> list[str]:
        """Ausstehende Massen-Löschung bestätigen – greift beim nächsten Abgleich."""
        names = list(self.state.get("ausstehend") or [])
        with self._lock:
            self.confirmed = set(names)
        return names

    def protect(self, name: str) -> None:
        with self._lock:
            self.protected[name] = time.monotonic()

    def _protected(self, name: str) -> bool:
        with self._lock:
            t = self.protected.get(name)
        return t is not None and time.monotonic() - t < PROTECT_TIME

    # ----------------------------------------------------------- Dateien (in Worker-Threads)
    def scan(self) -> dict[str, str]:
        """{name: sha256} der Vorlagen direkt im Vorlagenordner des Servers."""
        out = {}
        if not self.folder.is_dir():
            return out
        for p in self.folder.iterdir():
            if not valid_name(p.name) or not p.is_file():
                continue
            try:
                st = p.stat()
                key = (st.st_size, st.st_mtime_ns)
                hit = self._hashes.get(p.name)
                if hit and hit[:2] == key:
                    out[p.name] = hit[2]
                    continue
                sha = sha256(p.read_bytes())
            except OSError:
                continue
            self._hashes[p.name] = (*key, sha)
            out[p.name] = sha
        return out

    def count(self) -> int:
        try:
            return sum(1 for p in self.folder.iterdir() if valid_name(p.name) and p.is_file())
        except OSError:
            return 0

    def write(self, name: str, data: bytes) -> None:
        if not valid_name(name):   # doppelt hält besser: nie Pfade vom Helfer übernehmen
            raise TemplateError(f"Ungültiger Vorlagenname '{name}'.")
        self.folder.mkdir(parents=True, exist_ok=True)
        write_atomic(self.folder / name, data)

    def retire(self, name: str) -> Path:
        """Vorlage nach .geloescht/ verschieben (nie hart löschen)."""
        src = self.folder / name
        dest = self.folder / DELETED_DIR / name
        if dest.exists():
            dest = dest.with_name(f"{src.stem}.{datetime.now():%Y%m%d-%H%M%S}.lbx")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        self._hashes.pop(name, None)
        return dest

    @staticmethod
    def _lookup(local: dict[str, str], name: str) -> str | None:
        if name in local:
            return local[name]
        if os.name == "nt":   # Windows: Groß/klein egal
            cf = name.casefold()
            return next((s for n, s in local.items() if n.casefold() == cf), None)
        return None

    # ----------------------------------------------------------- Abgleich (in der Event-Loop)
    async def apply(self, hub: "HelperHub", conn: _Connection, msg: dict) -> None:
        ordner = msg.get("ordner")
        rechner = conn.info.get("rechner")
        if not ordner:
            self.state.update(ordner=None, rechner=rechner, fehler=None, uebersprungen={}, ausstehend=[])
            return
        if self.state.get("ordner") != ordner or self.state.get("rechner") != rechner:
            self.state.update(abgeglichen=None, anzahl=0)
        self.state.update(ordner=str(ordner), rechner=rechner)
        dateien = msg.get("dateien")
        if msg.get("fehler") or not isinstance(dateien, dict):
            self.state["fehler"] = str(msg.get("fehler") or "Manifest ohne Dateiliste")
            return
        files = {n: s.lower() for n, s in dateien.items()
                 if valid_name(n) and isinstance(s, str) and _SHA_RE.fullmatch(s.lower())}
        errors = [f"{n}: ungültiger Name oder Prüfsumme – ignoriert" for n in dateien if n not in files]
        raw_skipped = msg.get("uebersprungen") if isinstance(msg.get("uebersprungen"), dict) else {}
        skipped = {str(n): str(w) for n, w in raw_skipped.items() if isinstance(n, str)}
        local = await asyncio.to_thread(self.scan)

        key = f"{rechner or '?'}|{ordner}"
        known = await asyncio.to_thread(self._known)
        if not files and not skipped and local and not conn.seeded and key not in known:
            # erster Abgleich dieser Installation mit leerem PC-Ordner: Server-Vorlagen übernehmen
            conn.seeded = True
            await self._seed(hub, conn, local, errors)
            self.state.update(anzahl=0, fehler="; ".join(errors) or None, uebersprungen={}, ausstehend=[])
            if not errors:
                await asyncio.to_thread(self._remember, key)
            return

        fetched = 0
        for name, sha in files.items():
            if self._lookup(local, name) == sha:
                continue
            try:
                if await self._fetch(hub, conn, name):
                    fetched += 1
            except (HelperError, TemplateError, OSError) as e:
                errors.append(f"{name}: {e}")
        pc = {n.casefold() for n in list(files) + list(skipped)}
        gone = [n for n in sorted(local) if n.casefold() not in pc and not self._protected(n)]
        # Schutz: verschwinden auf einmal mehr als die Hälfte (oder alle) Vorlagen, ist eher der
        # falsche Ordner gewählt oder ein Laufwerk weg – dann erst nach Bestätigung verschieben.
        pending: list[str] = []
        if gone and (len(gone) == len(local) or 2 * len(gone) > len(local)):
            with self._lock:
                ok = set(gone) <= self.confirmed
            if not ok:
                pending = gone
                errors.append(f"{len(gone)} von {len(local)} Vorlagen fehlen im Vorlagen-Ordner auf dem PC – "
                              f"Ordner richtig gewählt? Nichts verschoben; Löschen unter Einstellungen > Helfer "
                              f"bestätigen.")
                gone = []
        with self._lock:
            self.confirmed.clear()
        moved = 0
        for name in gone:
            try:
                dest = await asyncio.to_thread(self.retire, name)
                moved += 1
                log.info("Vorlage %s gibt es auf dem PC nicht mehr – verschoben nach %s", name, dest)
            except OSError as e:
                errors.append(f"{name}: konnte nicht nach {DELETED_DIR} verschoben werden ({e})")
        with self._lock:
            for name in files:
                self.protected.pop(name, None)
        if not pending and key not in known:
            await asyncio.to_thread(self._remember, key)
        self.state["ausstehend"] = pending
        self.state.update(anzahl=len(files), abgeglichen=_now_iso(), fehler="; ".join(errors) or None,
                          uebersprungen=skipped, geholt=self.state["geholt"] + fetched,
                          verschoben=self.state["verschoben"] + moved)

    async def _fetch(self, hub: "HelperHub", conn: _Connection, name: str) -> bool:
        """Eine Vorlage vom PC holen. False = gerade in Arbeit (später erneut)."""
        ans = await hub._arequest(conn, {"typ": "datei", "name": name}, TIMEOUT_FILE)
        if not ans.get("ok"):
            if ans.get("code") == "unfertig":
                return False
            raise HelperError(str(ans.get("fehler") or "Helfer hat die Datei nicht geliefert"))
        d = ans.get("daten") or {}
        if d.get("name") != name:
            raise TemplateError("Helfer hat eine andere Datei geliefert – ignoriert.")
        text = d.get("daten")
        if not isinstance(text, str) or len(text) > MAX_FILE * 4 // 3 + 8:
            raise TemplateError(f"fehlt oder größer als {MAX_FILE // 2**20} MB")
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise TemplateError("beschädigt übertragen")
        if sha256(data) != str(d.get("sha256") or "").lower():
            raise TemplateError("Prüfsumme passt nicht – beschädigt übertragen")
        if not lbx_ok(data):
            raise TemplateError("keine gültige P-touch-Vorlage")
        await asyncio.to_thread(self.write, name, data)
        log.info("Vorlage %s vom PC übernommen.", name)
        return True

    async def _seed(self, hub: "HelperHub", conn: _Connection, local: dict[str, str], errors: list[str]) -> None:
        for name in sorted(local):
            try:
                data = await asyncio.to_thread((self.folder / name).read_bytes)
                self.protect(name)
                ans = await hub._arequest(conn, {"typ": "vorlage_speichern", "name": name, "sha256": sha256(data),
                                                 "daten": base64.b64encode(data).decode("ascii"),
                                                 "ueberschreiben": False}, TIMEOUT_FILE)
                if not ans.get("ok"):
                    errors.append(f"{name}: {ans.get('fehler')}")
            except (HelperError, OSError) as e:
                errors.append(f"{name}: {e}")
        log.info("Leerer Vorlagen-Ordner auf dem PC: %d Vorlage(n) vom Server übernommen.", len(local))

    def status(self, conn: _Connection | None) -> dict:
        """"vorlagen"-Teil von /api/status im Modus helfer."""
        st = self.state
        out = {"ordner": st["ordner"], "anzahl": st["anzahl"], "abgeglichen": st["abgeglichen"],
               "fehler": st["fehler"], "hinweis": None, "ausstehend": list(st["ausstehend"])}
        if conn is not None:
            if int(conn.info.get("protokoll") or 1) < 2:
                out.update(ordner=None, anzahl=self.count(), abgeglichen=None, fehler=None, hinweis=OLD_HELPER,
                           ausstehend=[])
            elif not conn.info.get("vorlagen_ordner"):
                out.update(ordner=None, anzahl=self.count(), abgeglichen=None, fehler=None, hinweis=NO_FOLDER,
                           ausstehend=[])
        if out["hinweis"] is None and st["uebersprungen"]:
            waiting = [n for n, why in st["uebersprungen"].items() if "gerade gespeichert" not in why
                       and "gesperrt" not in why]
            if waiting:
                out["hinweis"] = "Nicht abgeglichen: " + ", ".join(
                    f"{n} ({st['uebersprungen'][n]})" for n in waiting)
        return out


class HelperHub:
    """Verwaltet die (eine) Helfer-Verbindung und leitet Aufträge weiter."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._conn: _Connection | None = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.getrennt: str | None = None      # Zeitpunkt der letzten Trennung (Anzeige)
        self.sync = TemplateSync(cfg)

    # ----------------------------------------------------------- Zustand
    @property
    def token(self) -> str:
        return str(getattr(self.cfg, "helper_token", "") or "")

    def connection(self) -> _Connection | None:
        with self._lock:
            conn = self._conn
        return conn if conn is not None and not conn.closed else None

    def connected(self) -> bool:
        return self.connection() is not None

    def info(self) -> dict:
        conn = self.connection()
        return dict(conn.info) if conn else {}

    # ----------------------------------------------------------- WebSocket-Endpunkt
    async def endpoint(self, ws: WebSocket) -> None:
        await ws.accept()
        token = self.token
        header = ws.headers.get("authorization", "")
        authed = header.lower().startswith("bearer ") and _same(header[7:].strip(), token)
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT))
        except (asyncio.TimeoutError, ValueError):
            await self._reject(ws, CLOSE_PROTOCOL, "Keine gültige hallo-Nachricht erhalten")
            return
        except (WebSocketDisconnect, RuntimeError):
            return
        if not isinstance(first, dict) or first.get("typ") != "hallo":
            await self._reject(ws, CLOSE_PROTOCOL, "Erste Nachricht muss 'hallo' sein")
            return
        if not token:
            await self._reject(ws, CLOSE_AUTH, "Auf dem Server ist noch kein Helfer-Token eingerichtet")
            return
        if not (authed or _same(first.get("token"), token)):
            await self._reject(ws, CLOSE_AUTH, "Helfer-Token ungültig")
            return

        conn = _Connection(ws, asyncio.get_running_loop(), _clean_info(first))
        with self._lock:
            old, self._conn = self._conn, conn
        if old is not None:
            await self._close_conn(old, CLOSE_REPLACED, "Ein anderer Helfer hat sich angemeldet")
        await conn.send({"typ": "willkommen", "protokoll": PROTOKOLL, "server_version": __version__,
                         "drucker": getattr(self.cfg, "printer", "") or ""})
        pinger = asyncio.create_task(self._pinger(conn))
        try:
            while not conn.closed:
                try:
                    text = await asyncio.wait_for(ws.receive_text(), RECEIVE_TIMEOUT)
                except asyncio.TimeoutError:
                    await conn.close(CLOSE_TIMEOUT, "Keine Lebenszeichen vom Helfer")
                    break
                except (WebSocketDisconnect, RuntimeError):
                    break
                self._on_message(conn, text)
        finally:
            pinger.cancel()
            if conn.sync_task is not None:
                conn.sync_task.cancel()
            conn.closed = True
            conn.fail_all("Die Verbindung zum Druck-Helfer wurde getrennt.")
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    self.getrennt = _now_iso()

    @staticmethod
    async def _reject(ws: WebSocket, code: int, reason: str) -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass

    async def _pinger(self, conn: _Connection) -> None:
        try:
            while not conn.closed:
                await asyncio.sleep(PING_INTERVAL)
                await conn.send({"typ": "ping"})
        except asyncio.CancelledError:
            pass
        except Exception:
            await conn.close(CLOSE_TIMEOUT, "Senden an den Helfer fehlgeschlagen")

    def _on_message(self, conn: _Connection, text: str) -> None:
        conn.last_seen = time.monotonic()
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        typ = msg.get("typ")
        if typ in ("status", "hallo"):
            conn.info.update(_clean_info(msg))
        elif typ == "manifest":
            conn.last_manifest = msg
            conn.manifest = msg            # ältere, noch nicht bearbeitete Manifeste sind überholt
            if conn.sync_task is None or conn.sync_task.done():
                conn.sync_task = asyncio.get_running_loop().create_task(self._sync_loop(conn))
        elif "id" in msg and "ok" in msg:
            fut = conn.pending.pop(str(msg["id"]), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        # "pong" und Unbekanntes: nur Lebenszeichen

    async def _sync_loop(self, conn: _Connection) -> None:
        """Manifeste nacheinander abgleichen – kommt währenddessen ein neues, gilt nur das neueste."""
        while conn.manifest is not None and not conn.closed:
            msg, conn.manifest = conn.manifest, None
            try:
                await self.sync.apply(self, conn, msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Abgleich darf die Verbindung nie stören
                log.exception("Vorlagen-Abgleich fehlgeschlagen")
                self.sync.state["fehler"] = f"Abgleich fehlgeschlagen: {e}"
            finally:
                self.sync.state["runden"] += 1

    def resync(self) -> bool:
        """Letztes Manifest sofort erneut abgleichen (z. B. nach bestätigtem Löschen). Aus jedem Thread."""
        conn = self.connection()
        if conn is None or conn.last_manifest is None or conn.loop.is_closed():
            return False

        def kick():
            conn.manifest = conn.manifest or conn.last_manifest
            if conn.sync_task is None or conn.sync_task.done():
                conn.sync_task = conn.loop.create_task(self._sync_loop(conn))
        conn.loop.call_soon_threadsafe(kick)
        return True

    def token_ok(self, token) -> bool:
        """Helfer-Token prüfen (konstante Zeit) – für den Einrichtungs-Assistenten, ohne WebSocket."""
        return _same(token, self.token)

    def push_template(self, path: Path) -> dict | None:
        """Neue Vorlage (Kopie) in den Vorlagen-Ordner des PCs schreiben (aus einem Worker-Thread).

        None, wenn der Helfer keinen Vorlagen-Ordner hat (dann gilt der Server-Ordner).
        """
        conn = self.connection()
        if conn is None:
            raise HelperError(NOT_CONNECTED)
        if not conn.info.get("vorlagen_ordner"):
            return None
        path = Path(path)
        data = path.read_bytes()
        self.sync.protect(path.name)
        answer = self.request({"typ": "vorlage_speichern", "name": path.name, "sha256": sha256(data),
                               "daten": base64.b64encode(data).decode("ascii"), "ueberschreiben": False},
                              TIMEOUT_FILE, conn)
        if not answer.get("ok"):
            raise HelperError(str(answer.get("fehler") or "Vorlage konnte nicht zum PC geschickt werden."))
        return answer.get("daten") or {}

    def start_update(self, erzwingen: bool = False) -> dict:
        """Update per Knopf: dem verbundenen (installierten) Helfer den Auftrag „aktualisieren“ schicken.

        Der Helfer lädt das Setup selbst über SETUP_PFAD (mit seinem Token), prüft die sha256, startet es mit
        Rechteanhebung und beendet sich. Aus einem Worker-Thread aufrufen (blockiert bis zur Antwort).
        """
        conn = self.connection()
        if conn is None:
            raise UpdateError(NOT_CONNECTED, 409)
        upd = update_status(conn.info)
        setup = downloads.installer_pfad()
        if setup is None:
            raise UpdateError(UPD_NO_SETUP, 400)
        if conn.info.get("installiert") is not True:
            raise UpdateError(UPD_NOT_INSTALLED, 400)
        cmp = compare_versions(upd["installiert"], upd["angeboten"])
        if not erzwingen and cmp is not None and cmp >= 0:
            raise UpdateError(upd["grund"] + " Mit „erzwingen“ trotzdem neu installieren.", 409)
        try:
            sha = downloads.setup_sha256(setup)
        except OSError as e:
            raise UpdateError(f"Setup-Datei auf dem Server nicht lesbar: {e}", 400)
        msg = {"typ": "aktualisieren", "pfad": SETUP_PFAD, "version": upd["angeboten"], "sha256": sha}
        try:
            answer = self.request(msg, TIMEOUT_UPDATE, conn)
        except HelperError as e:
            raise UpdateError(f"Update fehlgeschlagen: {e}", 400) from e
        if not answer.get("ok"):
            raise UpdateError("Update fehlgeschlagen: " + str(answer.get("fehler") or "unbekannter Fehler im Helfer."),
                              400)
        log.info("Helfer-Update gestartet (%s -> %s) auf %s.", upd["installiert"], upd["angeboten"] or "?",
                 conn.info.get("rechner"))
        return {"gestartet": True, "version": upd["angeboten"], "von": upd["installiert"]}

    # ----------------------------------------------------------- Trennen
    async def _close_conn(self, conn: _Connection, code: int, reason: str) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:
            await conn.close(code, reason)
        elif not conn.loop.is_closed():
            asyncio.run_coroutine_threadsafe(conn.close(code, reason), conn.loop)

    def disconnect_all(self, code: int = CLOSE_AUTH, reason: str = "Helfer-Token wurde geändert") -> None:
        """Bestehende Verbindung trennen (z. B. nach Token-Wechsel). Aus jedem Thread aufrufbar."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                self.getrennt = _now_iso()
        if conn is None or conn.closed:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:
            running.create_task(conn.close(code, reason))
        elif not conn.loop.is_closed():
            fut = asyncio.run_coroutine_threadsafe(conn.close(code, reason), conn.loop)
            try:
                fut.result(3)
            except Exception:
                pass
        else:
            conn.closed = True

    # ----------------------------------------------------------- Aufträge
    async def _arequest(self, conn: _Connection, msg: dict, timeout: float) -> dict:
        rid = str(next(self._ids))
        fut = conn.loop.create_future()
        conn.pending[rid] = fut
        try:
            await conn.send(dict(msg, id=rid))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise HelperError(f"Der Druck-Helfer hat nicht rechtzeitig geantwortet ({timeout:g} s).")
        except (WebSocketDisconnect, RuntimeError, OSError):
            raise HelperError("Die Verbindung zum Druck-Helfer ist unterbrochen.")
        finally:
            conn.pending.pop(rid, None)

    def request(self, msg: dict, timeout: float, conn: _Connection | None = None) -> dict:
        """Auftrag senden und blockierend auf die Antwort warten (aus einem Worker-Thread)."""
        conn = conn or self.connection()
        if conn is None or conn.closed:
            raise HelperError(NOT_CONNECTED)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:   # würde die eigene Loop blockieren
            raise RuntimeError("HelperHub.request() darf nicht in der Event-Loop der Verbindung laufen.")
        fut = asyncio.run_coroutine_threadsafe(self._arequest(conn, msg, timeout), conn.loop)
        try:
            return fut.result(timeout + 5)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise HelperError(f"Der Druck-Helfer hat nicht rechtzeitig geantwortet ({timeout:g} s).")

    def call_batch(self, typ: str, payload: dict, parts: list[tuple[Path, dict]], timeout: float) -> dict:
        """Wie call(), aber mit mehreren Vorlagen: payload["teile"] = [{"vorlage", **teil}, …]."""
        conn = self.connection()
        if conn is None:
            raise HelperError(NOT_CONNECTED)
        files = []
        for template, extra in parts:
            template = Path(template)
            if not template.is_file():
                raise HelperError(f"Vorlage nicht gefunden: {template}")
            data = template.read_bytes()
            files.append((template, hashlib.sha256(data).hexdigest(), data, extra))
        for attempt in range(2):
            teile = []
            for template, sha, data, extra in files:
                tpl = {"name": template.name, "sha256": sha}
                if sha not in conn.known or attempt:
                    tpl["daten"] = base64.b64encode(data).decode("ascii")
                teile.append(dict(extra, vorlage=tpl))
            answer = self.request(dict(payload, typ=typ, teile=teile), timeout, conn)
            if answer.get("ok"):
                conn.known.update(sha for _, sha, _, _ in files)
                return answer.get("daten") or {}
            if answer.get("code") == "vorlage_fehlt" and not attempt:
                conn.known.difference_update(sha for _, sha, _, _ in files)
                continue
            break
        raise HelperError(str(answer.get("fehler") or "Unbekannter Fehler im Druck-Helfer."))

    def call(self, typ: str, payload: dict, timeout: float, template: Path | None = None) -> dict:
        """Auftrag ausführen und `daten` liefern; Fehler des Helfers -> HelperError.

        template: wird als {"name", "sha256", "daten": base64} mitgeschickt – die Bytes nur,
        wenn der Helfer diese Fassung noch nicht hat (meldet er code "vorlage_fehlt", erneut mit Bytes).
        """
        conn = self.connection()
        if conn is None:
            raise HelperError(NOT_CONNECTED)
        msg = dict(payload, typ=typ)
        sha = data = None
        if template is not None:
            template = Path(template)
            if not template.is_file():
                raise HelperError(f"Vorlage nicht gefunden: {template}")
            data = template.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
        for attempt in range(2):
            if template is not None:
                tpl = {"name": template.name, "sha256": sha}
                if sha not in conn.known or attempt:
                    tpl["daten"] = base64.b64encode(data).decode("ascii")
                msg["vorlage"] = tpl
            answer = self.request(msg, timeout, conn)
            if answer.get("ok"):
                if sha:
                    conn.known.add(sha)
                return answer.get("daten") or {}
            if answer.get("code") == "vorlage_fehlt" and sha and not attempt:
                conn.known.discard(sha)
                continue
            break
        raise HelperError(str(answer.get("fehler") or "Unbekannter Fehler im Druck-Helfer."))

    # ----------------------------------------------------------- Status
    def printer_name(self) -> str | None:
        info = self.info()
        return (getattr(self.cfg, "printer", "") or info.get("drucker")
                or default_printer(info.get("drucker_liste") or []))

    def printer_status(self, drucker: str) -> dict:
        """Wie bpac.status(): {"ok", "grund", "drucker", "hilfe_url"} + "angeschlossen"."""
        conn = self.connection()
        if conn is None:
            return {"ok": False, "grund": NOT_CONNECTED, "drucker": [], "hilfe_url": "", "angeschlossen": None}
        info = conn.info
        names = list(info.get("drucker_liste") or [])
        out = {"ok": False, "drucker": names, "hilfe_url": "", "angeschlossen": None}
        if not info.get("bpac"):
            return dict(out, grund=info.get("bpac_grund") or "Brother b-PAC Client ist auf dem Helfer-PC nicht installiert.",
                        hilfe_url=bpac.DOWNLOAD_URL)
        if drucker and drucker not in names:
            return dict(out, grund=f"Drucker '{drucker}' nicht gefunden. Installiert: {', '.join(names) or 'keiner'}")
        if not names:
            return dict(out, grund="Kein Brother-Etikettendrucker installiert – PT-P710BT per USB anschließen "
                                   "und Treiber installieren.")
        name = drucker or self.printer_name()
        state = (info.get("drucker_status") or {}).get(name) or {}
        out["angeschlossen"] = state.get("angeschlossen")
        if state.get("angeschlossen") is False:
            return dict(out, grund=f"Drucker '{name}' ist nicht verbunden oder ausgeschaltet.")
        return dict(out, ok=True, grund="")

    def status(self) -> dict:
        """Status-Form für /api/status im Modus helfer."""
        conn = self.connection()
        if conn is None:
            return {"helfer": {"modus": "helfer", "verbunden": False, "rechner": None, "seit": None, "version": None,
                               "bpac": False, "drucker_liste": [], "getrennt": self.getrennt,
                               "vorlagen": self.sync.status(None), "drucker_modell": None,
                               "update": update_status(None)},
                    "drucker": {"name": getattr(self.cfg, "printer", "") or None, "angeschlossen": None,
                                "status_text": "Druck-Helfer nicht verbunden", "bereit": False}}
        info = conn.info
        name = self.printer_name()
        state = (info.get("drucker_status") or {}).get(name) or {}
        st = self.printer_status(getattr(self.cfg, "printer", "") or "")
        text = state.get("status_text") or ("" if st["ok"] else st["grund"]) or "unbekannt"
        if not st["ok"] and st["grund"] and state.get("angeschlossen") is not False:
            text = st["grund"]
        return {"helfer": {"modus": "helfer", "verbunden": True, "rechner": info.get("rechner"), "seit": conn.seit,
                           "version": info.get("version"), "bpac": bool(info.get("bpac")),
                           "drucker_liste": list(info.get("drucker_liste") or []),
                           "vorlagen": self.sync.status(conn), "drucker_modell": info.get("drucker_modell") or None,
                           "update": update_status(info)},
                "drucker": {"name": name, "angeschlossen": state.get("angeschlossen"), "status_text": text,
                            "bereit": bool(getattr(self.cfg, "direct_print", False) and st["ok"]
                                           and state.get("angeschlossen") is True),
                            "band": state.get("band") if state.get("angeschlossen") else None}}


# ------------------------------------------------------------------ Drucker/Desktop über den Helfer
class HelperPrinter:
    """Gleiche Schnittstelle wie das bpac-Modul, aber über den verbundenen Helfer."""
    BpacError = bpac.BpacError

    def __init__(self, hub: HelperHub):
        self.hub = hub

    def status(self, printer: str) -> dict:
        return self.hub.printer_status(printer)

    def preview_available(self) -> bool:
        return bool(self.hub.info().get("bpac"))

    def render_png(self, template: Path, row: dict, dpi: int | None = None) -> bytes:
        job = {"zeile": row, **({"dpi": int(dpi)} if dpi else {})}   # ältere Helfer ignorieren dpi
        daten = self.hub.call("vorschau", job, TIMEOUT_PREVIEW, template=template)
        try:
            return base64.b64decode(daten["png"])
        except (KeyError, TypeError, ValueError):
            raise HelperError("Der Druck-Helfer hat kein Vorschaubild geliefert.")

    def print_labels(self, template: Path, rows: list[dict], printer: str, flags: int) -> int:
        daten = self.hub.call("drucken", {"zeilen": rows, "drucker": printer or "", "flags": int(flags)},
                              TIMEOUT_PRINT, template=template)
        return int(daten.get("gedruckt", len(rows)))

    def print_batch(self, jobs: list[tuple[Path, list[dict]]], printer: str, flags: int) -> int:
        """Ein Druckauftrag über mehrere Vorlagen; ältere Helfer drucken je Vorlage einzeln."""
        jobs = [(Path(t), list(r)) for t, r in jobs if r]
        if not self.hub.info().get("sammeldruck"):
            return sum(self.print_labels(t, r, printer, flags) for t, r in jobs)
        daten = self.hub.call_batch("drucken_sammel", {"drucker": printer or "", "flags": int(flags)},
                                    [(t, {"zeilen": r}) for t, r in jobs], TIMEOUT_PRINT)
        return int(daten.get("gedruckt", sum(len(r) for _, r in jobs)))


def _file_spec(path: Path) -> dict:
    """CSV als {"name", "inhalt": base64} – der Helfer legt sie lokal ab (keine Server-Pfade nötig)."""
    path = Path(path)
    if not valid_name(path.name, "csv"):
        raise HelperDesktopError(f"Ungültiger CSV-Name '{path.name}'.")
    data = path.read_bytes()
    if len(data) > MAX_FILE:
        raise HelperDesktopError(f"{path.name} ist größer als {MAX_FILE // 2**20} MB.")
    return {"name": path.name, "inhalt": base64.b64encode(data).decode("ascii")}


class HelperDesktop:
    """Desktop-Aktionen auf dem Helfer-PC.

    Protokoll 2: CSVs gehen als Inhalt mit, Vorlagen als Name (Vorlagen-Ordner des PCs) bzw.
    als Bytes (verknüpfte Kopie). Ältere Helfer (Protokoll 1) bekommen wie früher Server-Pfade.
    """

    def __init__(self, hub: HelperHub):
        self.hub = hub

    def _new(self) -> bool:
        return int(self.hub.info().get("protokoll") or 1) >= 2

    def _call(self, typ: str, payload: dict, template: Path | None = None) -> dict:
        try:
            return self.hub.call(typ, payload, TIMEOUT_DESKTOP, template=template)
        except HelperError as e:
            raise HelperDesktopError(str(e)) from e

    def open_linked(self, template: Path, csv_path: Path) -> str:
        csv = _file_spec(csv_path) if self._new() else str(csv_path)
        return str(self._call("oeffnen", {"csv": csv}, template=Path(template)).get("vorlage") or template)

    def edit_original(self, template: Path, csv_path: Path) -> dict | None:
        template = Path(template)
        if not self._new():
            self._call("bearbeiten", {"pfad": str(template), "csv": str(csv_path)})
            return None
        return self._call("bearbeiten", {"name": template.name, "pfad": str(template), "csv": _file_spec(csv_path)})

    def open_folder(self, path: Path) -> None:
        path = Path(path)
        if not self._new():
            self._call("ordner", {"pfad": str(path)})
            return
        specs = []
        if path.is_dir():
            for p in sorted(path.glob("*.csv")):
                if valid_name(p.name, "csv") and p.is_file() and p.stat().st_size <= MAX_FILE:
                    specs.append(_file_spec(p))
        self._call("ordner", {"pfad": str(path), "dateien": specs})

    def new_template(self, path: Path) -> None:
        try:
            self.hub.push_template(path)
        except HelperError as e:
            raise HelperDesktopError(str(e)) from e

    def remote_backup(self) -> bool:
        """Mit Vorlagen-Ordner sichert und ändert der Helfer die Vorlage auf dem PC."""
        info = self.hub.info()
        return int(info.get("protokoll") or 1) >= 2 and bool(info.get("vorlagen_ordner"))


# ------------------------------------------------------------------ lokal (b-PAC im Prozess)
class LocalPrinter:
    """bpac-Modul plus Verbindungsprüfung: kein Druckauftrag an einen ausgeschalteten Drucker
    (er bliebe sonst in der Windows-Warteschlange und käme beim nächsten Einschalten heraus)."""
    BpacError = bpac.BpacError

    def __init__(self, module=bpac, state_fn=printer_state):
        self.module, self.state_fn = module, state_fn
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def printer_state(self, name: str | None, fresh: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(name or "")
            if hit and not fresh and now - hit[0] < STATE_TTL:
                return hit[1]
        state = self.state_fn(name)
        with self._lock:
            self._cache[name or ""] = (now, state)
        return state

    def status(self, printer: str) -> dict:
        st = dict(self.module.status(printer))
        if st.get("ok"):
            name = printer or default_printer(st.get("drucker") or [])
            state = self.printer_state(name)
            st["angeschlossen"] = state.get("angeschlossen")
            if state.get("angeschlossen") is False:
                st.update(ok=False, grund=f"Drucker '{name}' ist nicht verbunden oder ausgeschaltet.")
        return st

    def preview_available(self) -> bool:
        return self.module.preview_available()

    def render_png(self, template: Path, row: dict, dpi: int | None = None) -> bytes:
        return self.module.render_png(template, row, **({"dpi": int(dpi)} if dpi else {}))

    def print_labels(self, template: Path, rows: list[dict], printer: str, flags: int) -> int:
        name = printer
        if not name:   # Drucker aus der Vorlage – b-PAC nimmt dann in der Regel den einzigen P-touch
            try:
                name = default_printer(self.module.status("").get("drucker") or [])
            except Exception:
                name = None
        require_online(name, self.state_fn)
        return self.module.print_labels(template, rows, printer, flags)

    def print_batch(self, jobs, printer: str, flags: int) -> int:
        batch = getattr(self.module, "print_batch", None)
        if batch is None:            # z. B. Test-Attrappen ohne Sammeldruck
            return sum(self.print_labels(t, r, printer, flags) for t, r in jobs if r)
        name = printer
        if not name:
            try:
                name = default_printer(self.module.status("").get("drucker") or [])
            except Exception:
                name = None
        require_online(name, self.state_fn)
        return batch(jobs, printer, flags)


class ModePrinter:
    """Wählt bei jedem Aufruf je nach cfg.print_mode lokal oder Helfer."""
    BpacError = bpac.BpacError

    def __init__(self, cfg, local, helper):
        self.cfg, self.local, self.helper = cfg, local, helper

    def current(self):
        return self.helper if print_mode(self.cfg) == "helfer" else self.local

    def status(self, printer):
        return self.current().status(printer)

    def preview_available(self):
        check = getattr(self.current(), "preview_available", None)
        return bool(check and check())

    def render_png(self, template, row):
        dpi = getattr(self.cfg, "preview_dpi", None)
        if not dpi or dpi == bpac.PREVIEW_DPI:      # Standard: Aufruf wie bisher (auch für ältere Helfer)
            return self.current().render_png(template, row)
        return self.current().render_png(template, row, dpi=dpi)

    def print_labels(self, template, rows, printer, flags):
        return self.current().print_labels(template, rows, printer, flags)

    def print_batch(self, jobs, printer, flags):
        return self.current().print_batch(jobs, printer, flags)


class ModeDesktop:
    def __init__(self, cfg, local, helper):
        self.cfg, self.local, self.helper = cfg, local, helper

    def current(self):
        return self.helper if print_mode(self.cfg) == "helfer" else self.local

    def open_linked(self, template, csv_path):
        return self.current().open_linked(template, csv_path)

    def edit_original(self, template, csv_path):
        return self.current().edit_original(template, csv_path)

    def open_folder(self, path):
        return self.current().open_folder(path)

    def new_template(self, path):
        fn = getattr(self.current(), "new_template", None)
        return fn(path) if fn else None

    def remote_backup(self) -> bool:
        fn = getattr(self.current(), "remote_backup", None)
        return bool(fn and fn())


class StatusProvider:
    """helper_status() für /api/status in beiden Modi (lokal: b-PAC im Prozess, kurz gecacht)."""

    def __init__(self, cfg, hub: HelperHub, local_printer):
        self.cfg, self.hub, self.local = cfg, hub, local_printer
        self.started = _now_iso()
        self._cache: tuple[float, tuple, dict] | None = None
        self._lock = threading.Lock()

    def __call__(self) -> dict:
        if print_mode(self.cfg) == "helfer":
            return self.hub.status()
        key = (getattr(self.cfg, "printer", ""), getattr(self.cfg, "direct_print", False))
        with self._lock:
            if self._cache and self._cache[1] == key and time.monotonic() - self._cache[0] < STATE_TTL:
                return self._cache[2]
        out = self._local()
        with self._lock:
            self._cache = (time.monotonic(), key, out)
        return out

    def _local(self) -> dict:
        printer = getattr(self.cfg, "printer", "") or ""
        try:
            st = self.local.status(printer)
        except Exception as e:  # b-PAC/COM darf den Status nicht kaputt machen
            st = {"ok": False, "grund": f"b-PAC-Status nicht lesbar: {e}", "drucker": []}
        names = list(st.get("drucker") or [])
        name = printer or default_printer(names)
        get_state = getattr(self.local, "printer_state", printer_state)
        state = get_state(name) if name else {"angeschlossen": None, "status_text": st.get("grund") or ""}
        try:
            avail = bool(self.local.preview_available())
        except Exception:
            avail = False
        text = state.get("status_text") or st.get("grund") or ""
        if not st.get("ok") and st.get("grund") and state.get("angeschlossen") is not False:
            text = st["grund"]
        return {"helfer": {"modus": "lokal", "verbunden": True, "rechner": socket.gethostname(), "seit": self.started,
                           "version": __version__, "bpac": avail, "drucker_liste": names,
                           "vorlagen": {"ordner": str(self.cfg.template_dir), "anzahl": self.hub.sync.count(),
                                        "abgeglichen": None, "fehler": None, "hinweis": None},
                           "drucker_modell": None, "update": update_status(None, "lokal")},
                "drucker": {"name": name or None, "angeschlossen": state.get("angeschlossen"), "status_text": text,
                            "bereit": bool(getattr(self.cfg, "direct_print", False) and st.get("ok")
                                           and state.get("angeschlossen") is True),
                            "band": state.get("band") if state.get("angeschlossen") else None}}


class Hooks(dict):
    """Hooks für routes_einstellungen.register(): als Dict und als Attribute nutzbar."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def local_desktop(cfg, opener) -> LocalDesktop:
    return LocalDesktop(opener=opener, out_dir=lambda: cfg.output_dir)
