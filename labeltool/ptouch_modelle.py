"""Brother-Etikettendrucker: Modellliste und installierte Drucker (für den Einrichtungs-Assistenten).

alle_modelle()                    -> ["PT-18R", …, "PT-P710BT", …, "QL-820NWB", …]
    Liest die Modelle aus dem installierten P-touch Editor (Version 6: lbxcore.Win32.dll enthält
    alle unterstützten Modellnamen als Text; Version 5.x und andere Pfade werden durchsucht).
    Ergebnis wird im Speicher und in %LOCALAPPDATA%\\Labeltool-Helfer\\ptouch_modelle.json
    zwischengespeichert (gültig, solange sich die gelesenen Dateien nicht ändern).
    Ohne P-touch Editor: eingebaute Liste (Stand P-touch Editor 6.13).
installierte_drucker()            -> [{"name": "Brother PT-P710BT", "modell": "PT-P710BT", "treiber": …}]
    Windows-Drucker (win32print) plus b-PAC-Liste (über den b-PAC-COM-Thread), nur P-touch/QL/TD/….
passender_drucker(modell, installierte) -> "Brother PT-P710BT" | None
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

# Serien in der Reihenfolge, in der sie angezeigt werden (P-touch zuerst)
SERIEN = ("PT", "QL", "TD", "RJ", "PJ", "TP", "VC")
_MODELL = rb"(?:PT|QL|TD|RJ|PJ|TP|VC)-[A-Z]{0,3}\d{2,5}[A-Z0-9]{0,6}"
_MODELL_RE = re.compile(rb"(?<![A-Za-z0-9_\-])" + _MODELL + rb"(?![A-Za-z0-9_])")
_MODELL_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_\-])((?:PT|QL|TD|RJ|PJ|TP|VC)-[A-Z]{0,3}\d{2,5}[A-Z0-9]{0,6})(?![A-Za-z0-9_])",
                             re.IGNORECASE)
_TEILE_RE = re.compile(r"^([A-Z]{2})-([A-Z]{0,3})(\d{2,5})([A-Z0-9]*)$")

MIN_TREFFER = 10            # weniger Modelle in einer Datei = Zufallstreffer, nicht die Modellliste
MAX_DATEI = 64 * 1024 * 1024
TREIBER_URL = "https://support.brother.com/g/b/productsearch.aspx?c=de&lang=de&content=dl"

# Stand: P-touch Editor 6.13 (lbxcore.Win32.dll), 126 Modelle
EINGEBAUT = (
    "PT-18NR", "PT-18R", "PT-2430PC", "PT-2700", "PT-2730", "PT-3600", "PT-9500PC", "PT-9600", "PT-9700PC",
    "PT-9800PCN", "PT-D410", "PT-D450", "PT-D460BT", "PT-D600", "PT-D610BT", "PT-D800W", "PT-E310BT", "PT-E500",
    "PT-E510", "PT-E550W", "PT-E560BT", "PT-E720BT", "PT-E800T", "PT-E800TK", "PT-E800W", "PT-E850TKW",
    "PT-E920BT", "PT-H500", "PT-N25BT", "PT-N30BT", "PT-N31BT", "PT-P300BT", "PT-P320BT", "PT-P700", "PT-P710BT",
    "PT-P750W", "PT-P900", "PT-P900W", "PT-P910BT", "PT-P950NW",
    "QL-1050", "QL-1060N", "QL-1100", "QL-1110NWB", "QL-1115NWB", "QL-2010", "QL-2110", "QL-2140NW", "QL-2150NWB",
    "QL-4110", "QL-4150NWB", "QL-500", "QL-550", "QL-560", "QL-570", "QL-580N", "QL-600", "QL-650TD", "QL-700",
    "QL-710W", "QL-720NW", "QL-800", "QL-810W", "QL-820NWB",
    "TD-2020", "TD-2030A", "TD-2120N", "TD-2125N", "TD-2125NWB", "TD-2130N", "TD-2135N", "TD-2135NWB", "TD-2310D",
    "TD-2320D", "TD-2320DF", "TD-2320DSA", "TD-2350D", "TD-2350DF", "TD-2350DFSA", "TD-2350DSA", "TD-4210D",
    "TD-4215D", "TD-4410D", "TD-4415D", "TD-4420DN", "TD-4420TN", "TD-4425DN", "TD-4425DNF", "TD-4455DNWB",
    "TD-4510D", "TD-4520DN", "TD-4520TN", "TD-4525DN", "TD-4550DNWB", "TD-4555DNWB", "TD-4555DNWBF",
    "TD-4650TNWB", "TD-4650TNWBR", "TD-4750TNWB", "TD-4750TNWBR",
    "RJ-2030", "RJ-2031", "RJ-2035B", "RJ-2050", "RJ-2055WB", "RJ-2131", "RJ-2140", "RJ-2150", "RJ-3035B",
    "RJ-3055WB", "RJ-3150", "RJ-3230B", "RJ-3235B", "RJ-3250WB", "RJ-3255WB", "RJ-4230B", "RJ-4235B", "RJ-4250WB",
    "RJ-4255WB",
    "PJ-822", "PJ-823", "PJ-862", "PJ-863", "PJ-883",
    "TP-M5000N", "VC-500W",
)

_lock = threading.Lock()
_cache: dict | None = None     # {"modelle": [...], "quelle": str}


# ------------------------------------------------------------------ Modellnamen
def sortierschluessel(modell: str) -> tuple:
    """Serie (PT, QL, TD …), dann Buchstaben vor der Nummer, Nummer numerisch, Zusatz."""
    m = _TEILE_RE.match(modell.upper())
    if not m:
        return (len(SERIEN), modell, 0, "")
    serie, pre, nr, rest = m.groups()
    idx = SERIEN.index(serie) if serie in SERIEN else len(SERIEN)
    return (idx, pre, int(nr), rest)


def sortiert(modelle) -> list[str]:
    return sorted({str(m).upper() for m in modelle if m}, key=sortierschluessel)


def plausibel(modell: str) -> bool:
    """Filtert Zufallstreffer (z. B. Versions- oder Ressourcennamen) heraus."""
    m = _TEILE_RE.match(modell.upper())
    if not m or len(modell) > 14:
        return False
    _, pre, nr, rest = m.groups()
    if int(nr) == 0 or nr.startswith("0"):
        return False
    return not re.search(r"\d[A-Z]+\d", rest)     # "PT-P7X1X" o. ä. ist kein Modell


def modelle_aus_bytes(data: bytes) -> set[str]:
    """Modellnamen aus Dateiinhalt (ASCII/UTF-8 und UTF-16LE)."""
    found = {m.group(0).decode("ascii") for m in _MODELL_RE.finditer(data)}
    if b"-\x00" in data:      # UTF-16LE-Texte (.NET-Ressourcen): Nullbytes entfernen und erneut suchen
        for m in re.finditer(rb"(?:[A-Z0-9\-]\x00){5,16}", data):
            found |= {x.group(0).decode("ascii") for x in _MODELL_RE.finditer(m.group(0).replace(b"\x00", b""))}
    return {m for m in found if plausibel(m)}


def modell_aus_name(name: str | None) -> str | None:
    """'Brother PT-P710BT' -> 'PT-P710BT'; kein P-touch/QL/TD/… -> None."""
    m = _MODELL_TEXT_RE.search(str(name or ""))
    return m.group(1).upper() if m and plausibel(m.group(1)) else None


# ------------------------------------------------------------------ P-touch Editor suchen
def _registry_ordner() -> list[Path]:
    """InstallLocation aller 'P-touch Editor'-Einträge unter Uninstall (32/64 bit)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    keys = (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in keys:
            try:
                h = winreg.OpenKey(root, key)
            except OSError:
                continue
            with h:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(h, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(h, sub) as s:
                            name = str(winreg.QueryValueEx(s, "DisplayName")[0])
                            if "p-touch editor" not in name.lower():
                                continue
                            loc = str(winreg.QueryValueEx(s, "InstallLocation")[0] or "")
                    except OSError:
                        continue
                    if loc:
                        out.append(Path(loc))
    return out


def editor_ordner() -> list[Path]:
    """Mögliche Installationsordner des P-touch Editors (neueste Version zuerst, nur vorhandene)."""
    cands = list(_registry_ordner())
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        brother = Path(base) / "Brother"
        pte = brother / "P-touch Editor"
        cands += sorted((p for p in _safe_iter(pte) if p.is_dir()), reverse=True)   # …\P-touch Editor\6, …\5.4
        cands.append(pte)
        cands += sorted((p for p in _safe_iter(brother) if p.is_dir() and p.name.lower().startswith("ptedit")),
                        reverse=True)                                                # …\Brother\Ptedit54
    out, seen = [], set()
    for p in cands:
        key = str(p).rstrip("\\/").lower()
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return out


def _safe_iter(folder: Path):
    try:
        return list(folder.iterdir())
    except OSError:
        return []


def modell_dateien(ordner: Path) -> list[Path]:
    """Dateien, in denen die Modellliste steht: bevorzugt lbxcore*.dll (Version 6), sonst
    DLL/EXE/XML/INI direkt im Ordner und eine Ebene tiefer (Version 5.x)."""
    core = [p for p in list(ordner.glob("lbxcore*.dll")) + list(ordner.glob("*/lbxcore*.dll")) if p.is_file()]
    if core:
        return sorted(core)
    out = []
    for pattern in ("*.dll", "*.exe", "*.xml", "*.ini", "*/*.dll", "*/*.xml", "*/*.ini"):
        for p in ordner.glob(pattern):
            try:
                if p.is_file() and p.stat().st_size <= MAX_DATEI:
                    out.append(p)
            except OSError:
                continue
    return sorted(set(out))


def _signatur(files: list[Path]) -> list[list]:
    sig = []
    for p in files:
        try:
            st = p.stat()
            sig.append([str(p), st.st_size, int(st.st_mtime)])
        except OSError:
            pass
    return sig


def standard_cache_datei() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "Labeltool-Helfer" / "ptouch_modelle.json"


def modelle_lesen(ordner_liste: list[Path] | None = None, cache_datei: Path | None = None) -> dict:
    """-> {"modelle": [...], "quelle": "<Datei>" | "eingebaut"} – ohne Speicher-Cache."""
    ordner_liste = editor_ordner() if ordner_liste is None else ordner_liste
    for ordner in ordner_liste:
        files = modell_dateien(Path(ordner))
        if not files:
            continue
        sig = _signatur(files)
        if cache_datei is not None:
            try:
                c = json.loads(Path(cache_datei).read_text(encoding="utf-8"))
                if c.get("signatur") == sig and len(c.get("modelle") or []) >= MIN_TREFFER:
                    return {"modelle": sortiert(c["modelle"]), "quelle": c.get("quelle") or str(ordner)}
            except (OSError, ValueError, AttributeError):
                pass
        found: set[str] = set()
        quelle = []
        for f in files:
            try:
                hit = modelle_aus_bytes(f.read_bytes())
            except OSError:
                continue
            if len(hit) >= MIN_TREFFER:
                found |= hit
                quelle.append(str(f))
        if len(found) >= MIN_TREFFER:
            result = {"modelle": sortiert(found), "quelle": "; ".join(quelle)}
            if cache_datei is not None:
                try:
                    Path(cache_datei).parent.mkdir(parents=True, exist_ok=True)
                    Path(cache_datei).write_text(json.dumps(dict(result, signatur=sig), ensure_ascii=False),
                                                 encoding="utf-8")
                except OSError:
                    pass
            return result
    return {"modelle": sortiert(EINGEBAUT), "quelle": "eingebaut"}


def modelle_info(neu: bool = False) -> dict:
    """Wie modelle_lesen(), einmal pro Prozess (danach aus dem Speicher)."""
    global _cache
    with _lock:
        if _cache is None or neu:
            try:
                _cache = modelle_lesen(cache_datei=standard_cache_datei())
            except Exception:    # nie am Modellverzeichnis scheitern
                _cache = {"modelle": sortiert(EINGEBAUT), "quelle": "eingebaut"}
        return dict(_cache, modelle=list(_cache["modelle"]))


def alle_modelle(neu: bool = False) -> list[str]:
    return modelle_info(neu)["modelle"]


# ------------------------------------------------------------------ installierte Drucker
def _windows_drucker() -> list[tuple[str, str]]:
    """[(Druckername, Treibername)] aller lokalen und verbundenen Windows-Drucker."""
    try:
        import win32print
    except ImportError:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return [(str(p.get("pPrinterName") or ""), str(p.get("pDriverName") or ""))
                for p in win32print.EnumPrinters(flags, None, 2)]
    except Exception:
        return []


def _bpac_drucker() -> list[str]:
    """b-PAC-Druckerliste – läuft im einen b-PAC-COM-Thread (siehe bpac.run_com)."""
    try:
        from . import bpac
        st = bpac.run_com(bpac.status, "", timeout=15)
    except Exception:
        return []
    return [str(n) for n in (st or {}).get("drucker") or []]


def installierte_drucker(windows=_windows_drucker, bpac_liste=_bpac_drucker) -> list[dict]:
    """Installierte Brother-Etikettendrucker: [{"name", "modell", "treiber", "bpac": bool}].

    Ein Drucker zählt, wenn Treiber- oder Druckername ein Modell enthält (PT-/QL-/TD-…) oder
    b-PAC ihn kennt. Andere Brother-Geräte (MFC-…, DCP-…) fallen heraus.
    """
    win = list(windows() if callable(windows) else windows or [])
    bp = list(bpac_liste() if callable(bpac_liste) else bpac_liste or [])
    bp_keys = {n.casefold() for n in bp}
    out: dict[str, dict] = {}
    for name, treiber in win:
        if not name:
            continue
        modell = modell_aus_name(treiber) or modell_aus_name(name)
        if modell or name.casefold() in bp_keys:
            out[name.casefold()] = {"name": name, "modell": modell, "treiber": treiber,
                                    "bpac": name.casefold() in bp_keys}
    for name in bp:
        if name.casefold() not in out:
            out[name.casefold()] = {"name": name, "modell": modell_aus_name(name), "treiber": "", "bpac": True}
    return sorted(out.values(), key=lambda d: (sortierschluessel(d["modell"] or "ZZ-9"), d["name"].casefold()))


def _namen(installierte) -> list[tuple[str, str | None]]:
    out = []
    for d in installierte or []:
        if isinstance(d, dict):
            out.append((str(d.get("name") or ""), d.get("modell") or modell_aus_name(d.get("name"))))
        else:
            out.append((str(d), modell_aus_name(str(d))))
    return [(n, m) for n, m in out if n]


def passender_drucker(modell: str | None, installierte) -> str | None:
    """Windows-Druckername zum Modell: gleiches Modell (aus Treiber/Name) vor 'Name enthält Modell'.

    'PT-P700' passt also nicht zu 'Brother PT-P700W'. installierte: Liste von Dicts
    (installierte_drucker) oder reinen Druckernamen.
    """
    modell = (modell or "").strip().upper()
    if not modell:
        return None
    namen = _namen(installierte)
    for name, m in namen:
        if m and m.upper() == modell:
            return name
    pat = re.compile(r"(?<![A-Za-z0-9\-])" + re.escape(modell) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    return next((name for name, _ in namen if pat.search(name)), None)


if __name__ == "__main__":   # Diagnose: python -m labeltool.ptouch_modelle
    info = modelle_info()
    print(f"{len(info['modelle'])} Modelle aus {info['quelle']}:")
    print(", ".join(info["modelle"]))
    print("Installierte Brother-Etikettendrucker:", installierte_drucker() or "keine", file=sys.stdout)
