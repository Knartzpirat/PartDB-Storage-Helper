"""Start: python -m labeltool [pfad/zur/config.yaml]

Konfig-Pfad: Argument > Umgebungsvariable LABELTOOL_CONFIG > config.yaml im aktuellen Ordner.
Weitere Umgebungsvariablen (v. a. für Docker):
  LABELTOOL_HOST, LABELTOOL_PORT   überschreiben server.host / server.port
  LABELTOOL_NO_BROWSER=1           keinen Browser öffnen
  LABELTOOL_DOCKER=1               läuft im Container (setzt das Dockerfile)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_config

# Beispiel-Konfiguration für den Container (im Image unter /app/docker/config.yaml)
DOCKER_EXAMPLE = Path(__file__).resolve().parent.parent / "docker" / "config.yaml"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "ja", "yes", "on"}


def in_container() -> bool:
    return _env_flag("LABELTOOL_DOCKER") or Path("/.dockerenv").exists()


def config_path() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env = os.environ.get("LABELTOOL_CONFIG", "").strip()
    return Path(env) if env else Path("config.yaml")


def ensure_config(path: Path) -> None:
    """Fehlt die Konfiguration im Container, wird sie aus docker/config.yaml angelegt."""
    if path.exists():
        return
    if in_container() and DOCKER_EXAMPLE.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DOCKER_EXAMPLE, path)
        print(f"Konfiguration angelegt: {path} (aus {DOCKER_EXAMPLE}). "
              f"Part-DB-URL und Token im Reiter Einstellungen eintragen.")
        return
    sys.exit(f"Konfiguration nicht gefunden: {path.resolve()}\n"
             f"Kopiere config.example.yaml nach config.yaml und trage URL + Token ein.")


def open_browser_wanted(host: str) -> bool:
    return not (_env_flag("LABELTOOL_NO_BROWSER") or in_container() or host not in LOCAL_HOSTS)


def main() -> None:
    cfg_path = config_path()
    ensure_config(cfg_path)
    cfg = load_config(cfg_path)

    host = os.environ.get("LABELTOOL_HOST", "").strip() or cfg.host
    port_env = os.environ.get("LABELTOOL_PORT", "").strip()
    try:
        port = int(port_env) if port_env else cfg.port
    except ValueError:
        sys.exit(f"LABELTOOL_PORT ist keine Zahl: {port_env!r}")
    cfg.host, cfg.port = host, port

    if not any(importlib.util.find_spec(m) for m in ("websockets", "wsproto")):
        print("Hinweis: weder 'websockets' noch 'wsproto' installiert – der Druck-Helfer kann sich nicht verbinden.")

    shown = "localhost" if host in {"0.0.0.0", "::"} else host
    url = f"http://{shown}:{port}/"
    if open_browser_wanted(host):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Labeltool läuft auf {url}  (Konfiguration: {cfg_path.resolve()}, Beenden mit Strg+C)")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
