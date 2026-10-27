"""Einstellungen, die in der Oberfläche geändert werden (Reiter "Einstellungen").

Gespeichert in einstellungen.yaml im Ordner der config.yaml. Die Datei überlagert
config.yaml abschnittsweise – es stehen nur Werte darin, die im Tool gespeichert wurden:

    partdb: {url, token, ssl_pruefen}
    druck:  {modus, direktdruck, drucker, schnitt}
    helfer: {token}

Änderungen werden sofort im cfg-Objekt übernommen (in place), die Datei wird atomar
geschrieben (erst .tmp, dann umbenennen). Tokens gehen nie im Klartext an die UI,
nur als Hinweis wie "tcp_1a2b…9c8d".
"""
from __future__ import annotations

import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .bpac import CUT_OPTIONS
from .config import Config

PRINT_MODES = ("lokal", "helfer")
SECTIONS = ("partdb", "druck", "helfer")

_lock = threading.Lock()


class SettingsError(ValueError):
    pass


# ------------------------------------------------------------ Datei
def load_settings(path: Path) -> dict:
    """Inhalt von einstellungen.yaml, {} wenn es sie (noch) nicht gibt oder sie kaputt ist."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return {}
    return {k: v for k, v in data.items() if k in SECTIONS and isinstance(v, dict)} \
        if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    header = ("# Wird vom Labeltool verwaltet (Reiter \"Einstellungen\") und überlagert config.yaml.\n"
              "# Einen Abschnitt löschen = wieder der Wert aus config.yaml gilt.\n")
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)


def save_section(cfg: Config, section: str, values: dict) -> None:
    """Einen Abschnitt in einstellungen.yaml ersetzen und sofort auf cfg anwenden."""
    if section not in SECTIONS:
        raise SettingsError(f"Unbekannter Abschnitt '{section}'.")
    with _lock:
        data = load_settings(cfg.settings_file)
        data[section] = dict(values)
        _write(cfg.settings_file, data)
        apply_settings(cfg, {section: data[section]})


# ------------------------------------------------------------ Anwenden
def apply_settings(cfg: Config, data: dict) -> None:
    """Überlagerung auf cfg übertragen – nur Schlüssel, die in data stehen."""
    pdb = data.get("partdb") or {}
    if "url" in pdb:
        cfg.partdb_url = str(pdb["url"] or "").strip().rstrip("/")
    if pdb.get("token"):
        cfg.partdb_token = str(pdb["token"])
    if "ssl_pruefen" in pdb:
        cfg.verify_ssl = bool(pdb["ssl_pruefen"])

    prn = data.get("druck") or {}
    if prn.get("modus") in PRINT_MODES:
        cfg.print_mode = prn["modus"]
    if "direktdruck" in prn:
        cfg.direct_print = bool(prn["direktdruck"])
    if "drucker" in prn:
        cfg.printer = str(prn["drucker"] or "")
    if isinstance(prn.get("schnitt"), list):
        cfg.cut_options = [str(o) for o in prn["schnitt"]]

    hlp = data.get("helfer") or {}
    if hlp.get("token"):
        cfg.helper_token = str(hlp["token"])


# ------------------------------------------------------------ Prüfen / Hilfen
def normalize_url(url: str | None) -> str:
    """'http://host:4444/api/' -> 'http://host:4444'. Leer bleibt leer, Unsinn -> SettingsError."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.lower().endswith("/api"):
        url = url[:-4].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SettingsError("Die Part-DB-URL muss mit http:// oder https:// beginnen, "
                            "z. B. http://192.168.0.100:4444 (ohne /api).")
    try:
        parts.port   # wirft bei ungültigem Port
    except ValueError:
        raise SettingsError("Die Part-DB-URL enthält einen ungültigen Port.")
    return url


def check_print(modus: str, schnitt: list[str]) -> tuple[str, list[str]]:
    modus = (modus or "").strip().lower()
    if modus not in PRINT_MODES:
        raise SettingsError(f"Druckmodus muss {' oder '.join(PRINT_MODES)} sein.")
    out = []
    for o in schnitt or []:
        key = str(o).strip().lower()
        if key not in CUT_OPTIONS:
            raise SettingsError(f"Unbekannte Schnittoption '{o}'. Möglich: {', '.join(CUT_OPTIONS)}")
        if key not in out:
            out.append(key)
    return modus, out


def token_hint(token: str | None) -> str:
    """'tcp_1a2b…9c8d' – genug zum Wiedererkennen, nicht genug zum Benutzen."""
    token = token or ""
    if not token:
        return ""
    if len(token) <= 12:
        return token[:2] + "…"
    return f"{token[:8]}…{token[-4:]}"


def new_helper_token() -> str:
    return secrets.token_urlsafe(32)


def overview(cfg: Config) -> dict:
    """Antwort für GET /api/einstellungen (ohne Tokens im Klartext)."""
    return {
        "partdb": {"url": cfg.partdb_url, "token_gesetzt": bool(cfg.partdb_token),
                   "token_hinweis": token_hint(cfg.partdb_token), "ssl_pruefen": cfg.verify_ssl},
        "druck": {"modus": cfg.print_mode, "direktdruck": cfg.direct_print, "drucker": cfg.printer,
                  "schnitt": list(cfg.cut_options), "schnitt_optionen": list(CUT_OPTIONS)},
        "helfer": {"token_gesetzt": bool(cfg.helper_token), "token_hinweis": token_hint(cfg.helper_token)},
    }
