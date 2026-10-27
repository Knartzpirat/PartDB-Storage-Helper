"""Download des Windows-Druck-Helfers als ZIP (GET /api/helfer/download).

Einbinden in create_app:  routes_helfer_download.register(app, cfg, app.state.hooks)

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
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

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
        "# Vorgaben für den Einrichtungs-Assistenten (helfer.bat) – aus dem Labeltool heruntergeladen\n"
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


def register(app: FastAPI, cfg: Config, hooks: Any = None) -> None:
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
