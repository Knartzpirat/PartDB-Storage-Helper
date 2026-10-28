"""Download des Windows-Druck-Helfers.

  GET /api/helfer/installer        Labeltool-Helfer-Installer.zip: Setup-Programm + helfer.vorgabe.yaml + LIESMICH.txt
  GET /api/helfer/installer/info   {"installer_verfuegbar": bool, ...} – ob die Setup-Datei auf dem Server liegt
  GET /api/helfer/download         Labeltool-Helfer.zip: Python-Variante ohne Installation (siehe unten)
  GET /api/helfer/installer/setup  nur die Setup-Datei – für das Update per Knopf; NUR mit Helfer-Token
                                   (Authorization: Bearer …), Header X-Setup-Version / X-Setup-Sha256

Einbinden in create_app:  routes_helfer_download.register(app, cfg, app.state.hooks)

Die Setup-Datei baut die CI (PyInstaller + Inno Setup) und legt sie ins Image unter
/app/helfer-installer/Labeltool-Helfer-Setup.exe; anderer Ort per Umgebungsvariable LABELTOOL_INSTALLER.
Ohne Variable wird lokal zusätzlich installer/dist/Labeltool-Helfer-Setup.exe im Projekt gesucht.
Neben der Setup-Datei liegt Labeltool-Helfer-Setup.version (nur die Build-Version, z. B. 1.2.3); fehlt sie,
ist die angebotene Version unbekannt (Update per Knopf geht trotzdem). Die sha256 der Setup-Datei
berechnet der Server selbst (gecacht nach Größe/Änderungszeit).

Python-Variante:

Das Paket enthält nur, was der Helfer unter Windows braucht (kein fastapi/uvicorn):
  labeltool/…            Module, die helfer.py, helfer_setup.py und ptouch_modelle.py importieren
                         (wird aus den Importen ermittelt – neue Abhängigkeiten kommen automatisch mit)
  requirements-helfer.txt  pyyaml, websockets, pywin32
  helfer.bat, helfer-autostart.bat, LIESMICH.txt
  helfer.vorgabe.yaml    Server-Adresse (aus der Anfrage, X-Forwarded-* beachtet) und Helfer-Token
Gibt es noch keinen Helfer-Token, wird einer erzeugt und gespeichert (es kann dann noch kein
Helfer verbunden sein – niemand wird getrennt). Tokens werden nicht geloggt.
"""
from __future__ import annotations

import ast
import io
import json
import hashlib
import os
import re
import secrets
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from . import __version__
from . import einstellungen as es
from .config import Config

PAKET_DIR = Path(__file__).resolve().parent          # labeltool/
PROJEKT_DIR = PAKET_DIR.parent                       # Repo bzw. /app im Docker-Image
START_MODULE = ("helfer", "helfer_setup", "ptouch_modelle")
BAT_DATEIEN = ("helfer.bat", "helfer-autostart.bat")
ZIP_NAME = "Labeltool-Helfer.zip"
# Server-Pakete dürfen nicht in den Helfer rutschen
VERBOTEN = ("fastapi", "uvicorn", "starlette", "httpx", "pydantic")

REQUIREMENTS = """\
# Pakete für den Labeltool-Druck-Helfer (Windows) – helfer.bat installiert sie in .venv
pyyaml>=6.0
websockets>=13.0
pywin32>=306; sys_platform == "win32"
"""


# ------------------------------------------------------------------ Module ermitteln
def _importe(path: Path) -> tuple[set[str], set[str]]:
    """(labeltool-Module, fremde Top-Level-Pakete), die eine Datei importiert – auch in Funktionen."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    eigene, fremde = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:          # from . import x / from .x import y
                if node.module:
                    eigene.add(node.module.split(".")[0])
                else:
                    eigene |= {a.name for a in node.names}
            elif node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top == "labeltool":
                    parts = node.module.split(".")
                    if len(parts) > 1:
                        eigene.add(parts[1])
                    else:
                        eigene |= {a.name for a in node.names}
                else:
                    fremde.add(top)
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "labeltool" and len(parts) > 1:
                    eigene.add(parts[1])
                elif parts[0] != "labeltool":
                    fremde.add(parts[0])
    return eigene, fremde


def helfer_module(paket_dir: Path = PAKET_DIR, start=START_MODULE) -> list[str]:
    """Alle labeltool-Module, die die Startmodule (transitiv) brauchen, plus __init__."""
    todo, fertig = list(start), set()
    while todo:
        name = todo.pop()
        if name in fertig or name == "__version__":
            continue
        path = paket_dir / f"{name}.py"
        if not path.is_file():
            continue                     # z. B. "__version__" aus "from . import __version__"
        fertig.add(name)
        eigene, _ = _importe(path)
        todo += sorted(eigene - fertig)
    return sorted(fertig | {"__init__"})


def fremde_pakete(paket_dir: Path = PAKET_DIR, module=None) -> set[str]:
    out = set()
    for name in module or helfer_module(paket_dir):
        out |= _importe(paket_dir / f"{name}.py")[1]
    return out


# ------------------------------------------------------------------ Inhalt
def server_url(request: Request) -> str:
    """Adresse, unter der der Browser den Server erreicht – hinter einem Reverse-Proxy aus X-Forwarded-*."""
    h = request.headers
    first = lambda v: (v or "").split(",")[0].strip()
    proto = first(h.get("x-forwarded-proto")) or request.url.scheme
    host = first(h.get("x-forwarded-host")) or h.get("host") or request.url.netloc
    port = first(h.get("x-forwarded-port"))
    if port and ":" not in host.rsplit("]", 1)[-1] and port not in (("443",) if proto == "https" else ("80",)):
        host = f"{host}:{port}"
    prefix = first(h.get("x-forwarded-prefix")).strip("/")
    return f"{proto}://{host}" + (f"/{prefix}" if prefix else "")


def vorgabe_yaml(server: str, token: str) -> str:
    return (
        "# Vorgaben für den Einrichtungs-Assistenten (Setup bzw. helfer.bat) – aus dem Labeltool heruntergeladen\n"
        f"# am {datetime.now():%d.%m.%Y %H:%M}. Der Assistent schreibt daraus helfer.yaml.\n"
        "# Der Token ist geheim: Datei nicht weitergeben.\n"
        f"server: {json.dumps(server)}\n"
        f"token: {json.dumps(token)}\n"
    )


LIESMICH = """\
Labeltool Druck-Helfer für Windows (Version {version})
=====================================================

Der Druck-Helfer verbindet diesen PC mit dem Labeltool-Server ({server}).
Er druckt über b-PAC auf den Brother-Etikettendrucker, zeigt die exakte Vorschau,
öffnet Vorlagen im P-touch Editor und hält den Vorlagen-Ordner auf diesem PC mit
dem Server abgeglichen. Am PC muss kein Port geöffnet werden.

Voraussetzungen
---------------
- Python 3.11 oder neuer (python.org oder Microsoft Store), "py" bzw. "python" im PATH
- Brother P-touch Editor und der Druckertreiber für dein Modell
- Brother b-PAC Client Component (64 bit, passend zu Python) für Direktdruck und Vorschau:
  https://support.brother.com/g/s/es/dev/en/bpac/download/index.html

Einrichten
----------
1. ZIP in einen festen Ordner entpacken, z. B. C:\\Labeltool-Helfer
   (nicht im Download-Ordner lassen, wenn der Autostart genutzt wird).
2. helfer.bat doppelklicken. Beim ersten Start wird eine Python-Umgebung (.venv)
   angelegt (dauert 1-2 Minuten), dann öffnet sich der Einrichtungs-Assistent:
   - Server-Adresse und Helfer-Token sind schon eingetragen ("Verbindung testen")
   - Vorlagen-Ordner: Ordner mit deinen .lbx-Vorlagen auf diesem PC. Er ist die
     Quelle der Vorlagen – der Server übernimmt neue/geänderte Dateien von hier.
   - Druckermodell: installierte Drucker stehen oben ("installiert")
   - Autostart: Helfer startet bei jeder Windows-Anmeldung minimiert
3. "Speichern & starten" – der Helfer läuft im Konsolenfenster. Im Labeltool zeigt
   die Kopfzeile "Helfer: verbunden".

Später
------
- Einstellungen ändern:   helfer.bat einrichten   (laufenden Helfer vorher schließen)
- Autostart entfernen:    helfer-autostart.bat entfernen
- Neuer Helfer-Token im Labeltool erzeugt? Paket neu herunterladen und in denselben
  Ordner entpacken, dann "helfer.bat einrichten" – oder den Token in helfer.yaml eintragen.
- Diagnose Druckermodelle: .venv\\Scripts\\python -m labeltool.ptouch_modelle
"""


def _crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def bat_dateien(projekt_dir: Path | None = None) -> dict[str, bytes]:
    projekt_dir = Path(projekt_dir or PROJEKT_DIR)
    out = {}
    for name in BAT_DATEIEN:
        p = projekt_dir / name
        if not p.is_file():
            raise FileNotFoundError(name)
        out[name] = _crlf(p.read_text(encoding="utf-8"))
    return out


def baue_zip(server: str, token: str, paket_dir: Path = PAKET_DIR, projekt_dir: Path | None = None) -> bytes:
    module = helfer_module(paket_dir)
    verboten = fremde_pakete(paket_dir, module) & set(VERBOTEN)
    if verboten:
        raise RuntimeError(f"Helfer-Module importieren Server-Pakete: {', '.join(sorted(verboten))}")
    bats = bat_dateien(projekt_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in module:
            z.write(paket_dir / f"{name}.py", f"labeltool/{name}.py")
        for name, data in bats.items():
            z.writestr(name, data)
        z.writestr("requirements-helfer.txt", REQUIREMENTS)
        z.writestr("helfer.vorgabe.yaml", vorgabe_yaml(server, token))
        z.writestr("LIESMICH.txt", _crlf(LIESMICH.format(version=__version__, server=server)))
    return buf.getvalue()


def helfer_token(cfg: Config) -> str:
    """Aktueller Helfer-Token; fehlt er, einen erzeugen und speichern (ohne Verbindungen zu trennen)."""
    if not cfg.helper_token:
        es.save_section(cfg, "helfer", {"token": es.new_helper_token()})
    return cfg.helper_token


# ------------------------------------------------------------------ Installer (Setup-Programm)
INSTALLER_ENV = "LABELTOOL_INSTALLER"
SETUP_NAME = "Labeltool-Helfer-Setup.exe"
INSTALLER_STANDARD = Path("/app/helfer-installer") / SETUP_NAME
INSTALLER_ZIP_NAME = "Labeltool-Helfer-Installer.zip"
INSTALLER_FEHLT = ("Der Windows-Installer liegt auf diesem Server nicht vor (z. B. selbst gebautes Image ohne "
                   "Installer). Bitte die Alternative ohne Installation nutzen: „Helfer als Python-Paket "
                   "herunterladen“ – oder den Installer als Release-Asset von GitHub laden.")

LIESMICH_INSTALLER = """\
Labeltool Druck-Helfer für Windows – Installer (Server-Version {version})
=========================================================================

Der Druck-Helfer verbindet diesen PC mit dem Labeltool-Server ({server}).
Er druckt über b-PAC auf den Brother-Etikettendrucker, zeigt die exakte Vorschau,
öffnet Vorlagen im P-touch Editor und hält den Vorlagen-Ordner auf diesem PC mit
dem Server abgeglichen. Am PC muss kein Port geöffnet werden. Python wird nicht
benötigt – das Setup bringt alles mit.

Voraussetzungen
---------------
- Windows 10 oder 11 (64 bit)
- Brother P-touch Editor und der Druckertreiber für dein Modell
- Brother b-PAC Client Component (64 bit) für Direktdruck und Vorschau:
  https://support.brother.com/g/s/es/dev/en/bpac/download/index.html

Installieren
------------
1. Diese ZIP-Datei vollständig entpacken (Rechtsklick > "Alle extrahieren ...").
   Wichtig: Labeltool-Helfer-Setup.exe und helfer.vorgabe.yaml müssen im selben
   Ordner liegen – das Setup nicht direkt aus dem ZIP heraus starten, sonst fehlen
   Server-Adresse und Token im Assistenten.
2. Labeltool-Helfer-Setup.exe doppelklicken.
   - Windows SmartScreen meldet "Der Computer wurde durch Windows geschützt", weil das
     Setup nicht digital signiert ist: auf "Weitere Informationen" klicken, dann auf
     "Trotzdem ausführen".
   - Die Benutzerkontensteuerung (UAC) fragt nach Administratorrechten, weil das
     Programm nach C:\\Program Files\\Labeltool-Helfer installiert wird: "Ja".
3. Im Setup die Aufgaben wählen (Autostart bei der Windows-Anmeldung ist
   voreingestellt) und installieren. Das Setup legt im Startmenü die Einträge
   "Labeltool Druck-Helfer", "Druck-Helfer einrichten" und "Deinstallieren" an.
4. Am Ende öffnet sich der Einrichtungs-Assistent:
   - Server-Adresse und Helfer-Token sind schon eingetragen ("Verbindung testen")
   - Vorlagen-Ordner: Ordner mit deinen .lbx-Vorlagen auf diesem PC. Er ist die
     Quelle der Vorlagen – der Server übernimmt neue/geänderte Dateien von hier.
   - Druckermodell: installierte Drucker stehen oben ("installiert")
   "Speichern & starten" – der Helfer läuft danach unsichtbar im Hintergrund. Im
   Labeltool zeigt die Kopfzeile "Helfer: verbunden".

Einstellungen und Protokoll
---------------------------
Alle Einstellungen liegen in %LOCALAPPDATA%\\Labeltool-Helfer\\
(helfer.yaml, Zwischenspeicher, Protokoll helfer.log). Ändern: Startmenü >
"Druck-Helfer einrichten". Neuer Helfer-Token im Labeltool erzeugt? Den Installer
neu herunterladen und erneut installieren – oder den Token im Assistenten eintragen.

Update
------
Neues Setup herunterladen und einfach über die bestehende Installation
installieren. Die Einstellungen bleiben erhalten.

Deinstallieren
--------------
Windows-Einstellungen > Apps > Installierte Apps > "Labeltool Druck-Helfer" >
Deinstallieren (oder Startmenü > "Deinstallieren"). Programm, Startmenü-Einträge
und Autostart werden entfernt; die Einstellungen in %LOCALAPPDATA%\\Labeltool-Helfer\\
kannst du danach von Hand löschen.
"""


def installer_pfad() -> Path | None:
    """Setup-Datei auf dem Server oder None.

    Ist LABELTOOL_INSTALLER gesetzt, gilt nur dieser Pfad. Sonst der Ort im Docker-Image und lokal
    installer/dist/ im Projekt (Ergebnis von installer/build.ps1).
    """
    env = os.environ.get(INSTALLER_ENV, "").strip()
    kandidaten = [Path(env)] if env else [INSTALLER_STANDARD, PROJEKT_DIR / "installer" / "dist" / SETUP_NAME]
    for p in kandidaten:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


VERSION_NAME = "Labeltool-Helfer-Setup.version"
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_SHA_CACHE: dict[str, tuple[int, int, str]] = {}     # Pfad -> (Größe, mtime_ns, sha256)
_SHA_LOCK = threading.Lock()


def setup_version(setup: Path | None = None) -> str | None:
    """Build-Version aus Labeltool-Helfer-Setup.version neben der Setup-Datei; None = unbekannt."""
    setup = setup or installer_pfad()
    if setup is None:
        return None
    try:
        with open(Path(setup).with_name(VERSION_NAME), "rb") as f:
            text = f.read(200).decode("utf-8-sig", "replace").strip()
    except OSError:
        return None
    text = text.splitlines()[0].strip() if text else ""
    return text if _VERSION_RE.fullmatch(text) else None


def setup_sha256(setup: Path) -> str:
    """sha256 der Setup-Datei (blockweise gelesen, gecacht solange Größe/mtime gleich bleiben)."""
    setup = Path(setup)
    st = setup.stat()
    key = str(setup.resolve())
    with _SHA_LOCK:
        hit = _SHA_CACHE.get(key)
        if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
            return hit[2]
    h = hashlib.sha256()
    with open(setup, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    sha = h.hexdigest()
    with _SHA_LOCK:
        _SHA_CACHE[key] = (st.st_size, st.st_mtime_ns, sha)
    return sha


def installer_info(cfg: Config | None = None) -> dict:
    """Für /api/meta bzw. /api/helfer/installer/info. Erzeugt keinen Token und liest die Datei nicht."""
    p = installer_pfad()
    if p is None:
        return {"installer_verfuegbar": False}
    st = p.stat()
    return {"installer_verfuegbar": True, "installer_groesse": st.st_size,
            "installer_stand": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "installer_version": setup_version(p)}


def token_aus_header(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def token_gleich(a: str, b: str) -> bool:
    """Konstante-Zeit-Vergleich; ein leerer Server-Token passt nie."""
    return bool(a) and bool(b) and secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def baue_installer_zip(ziel, setup: Path, server: str, token: str) -> None:
    """Schreibt das Installer-ZIP nach `ziel` (Pfad oder Datei-Objekt).

    Die Setup-Datei wird blockweise kopiert (nicht komplett in den Speicher geladen) und nur gespeichert,
    nicht komprimiert – sie ist bereits komprimiert.
    """
    with zipfile.ZipFile(ziel, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(setup, SETUP_NAME, compress_type=zipfile.ZIP_STORED)
        z.writestr("helfer.vorgabe.yaml", _crlf(vorgabe_yaml(server, token)))
        z.writestr("LIESMICH.txt", _crlf(LIESMICH_INSTALLER.format(version=__version__, server=server)))


def _loeschen(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def register(app: FastAPI, cfg: Config, hooks: Any = None) -> None:
    @app.get("/api/helfer/installer/info")
    def installer_status():
        return installer_info(cfg)

    @app.get("/api/helfer/installer")
    def installer_download(request: Request):
        setup = installer_pfad()
        if setup is None:
            raise HTTPException(404, INSTALLER_FEHLT)
        # ZIP in eine temporäre Datei schreiben (Content-Length für die Fortschrittsanzeige, kein
        # zweites Exemplar im Speicher); nach dem Senden wird sie gelöscht.
        fd, tmp = tempfile.mkstemp(prefix="labeltool-installer-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as f:
                baue_installer_zip(f, setup, server_url(request), helfer_token(cfg))
        except OSError as e:
            _loeschen(tmp)
            raise HTTPException(500, f"Installer-Paket konnte nicht gebaut werden: {e}")
        except BaseException:
            _loeschen(tmp)
            raise
        return FileResponse(tmp, media_type="application/zip", filename=INSTALLER_ZIP_NAME,
                            headers={"Cache-Control": "no-store"}, background=BackgroundTask(_loeschen, tmp))

    @app.get("/api/helfer/installer/setup")
    def installer_setup(request: Request):
        """Setup-Datei für das Update per Knopf – lädt der Helfer selbst mit seinem Token."""
        if not token_gleich(token_aus_header(request), str(cfg.helper_token or "")):
            raise HTTPException(401, "Helfer-Token fehlt oder ist ungültig.",
                                headers={"WWW-Authenticate": "Bearer"})
        setup = installer_pfad()
        if setup is None:
            raise HTTPException(404, INSTALLER_FEHLT)
        try:
            sha = setup_sha256(setup)
        except OSError as e:
            raise HTTPException(500, f"Setup-Datei nicht lesbar: {e}")
        headers = {"Cache-Control": "no-store", "X-Setup-Sha256": sha,
                   "X-Setup-Version": setup_version(setup) or ""}
        return FileResponse(setup, media_type="application/octet-stream", filename=SETUP_NAME, headers=headers)

    @app.get("/api/helfer/download")
    def helper_download(request: Request):
        try:
            data = baue_zip(server_url(request), helfer_token(cfg))
        except FileNotFoundError as e:
            raise HTTPException(500, f"Helfer-Paket unvollständig: {e} fehlt auf dem Server.")
        except (OSError, RuntimeError, SyntaxError) as e:
            raise HTTPException(500, f"Helfer-Paket konnte nicht gebaut werden: {e}")
        return Response(data, media_type="application/zip",
                         headers={"Content-Disposition": f'attachment; filename="{ZIP_NAME}"',
                                  "Cache-Control": "no-store"})
