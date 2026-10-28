"""Desktop-Aktionen (P-touch Editor öffnen, Ordner öffnen) und Druckerzustand unter Windows.

`LocalDesktop` ist das bisherige Verhalten im selben Prozess. Im Helfer-Modus reicht
`helfer_server.HelperDesktop` dieselben Aufrufe an den Windows-Helfer weiter, der dort
wiederum `LocalDesktop` nutzt.

Schnittstelle (duck typing):
- open_linked(template, csv_path) -> Path   verknüpfte Kopie anlegen und öffnen
- edit_original(template, csv_path) -> None Original auf csv_path umhängen und öffnen
                                            (die Sicherung macht der Server vorher)
- open_folder(path) -> None
- new_template(path) -> None               neue Vorlage (Kopie) an die Quelle der Vorlagen geben
                                            (lokal: nichts zu tun, Helfer: in den PC-Ordner schreiben)

Außerdem die Datei-Bausteine, die Server UND Helfer für den Vorlagen-Abgleich brauchen
(Namensprüfung, atomares Schreiben, Sicherung) – dieses Modul hat keine Server-Abhängigkeiten.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .ptouch import TemplateError, _relink, linked_copy, open_file

# win32print: PRINTER_INFO_2.Status / .Attributes
_ATTR_WORK_OFFLINE = 0x400
_STATUS_TEXT = [            # (Bit, Text, Druck blockiert?)
    (0x80, "offline", True),
    (0x4000, "nicht verfügbar", True),
    (0x400000, "Benutzereingriff nötig", True),
    (0x2, "Fehler", True),
    (0x8, "Papierstau", True),
    (0x10, "kein Band", True),
    (0x40, "Bandproblem", True),
    (0x80000, "Abdeckung offen", True),
    (0x1, "angehalten", True),
    (0x4, "Aufträge werden gelöscht", False),
    (0x400, "druckt", False),
    (0x200, "beschäftigt", False),
]


BACKUP_DIR = ".backup"       # im Vorlagenordner: Sicherungen vor dem ersten Bearbeiten
MAX_FILE = 10 * 1024 * 1024  # Bytes je Vorlage/CSV im Abgleich (base64 passt unter das 16-MB-Limit von uvicorn)

# Dateiname ohne Ordner, ohne unter Windows verbotene Zeichen, nicht versteckt
_FILE_RE = r'^(?!\.)[^\\/:*?"<>|\x00-\x1f]+\.{}$'
_NAME_RES = {s: re.compile(_FILE_RE.format(s), re.IGNORECASE) for s in ("lbx", "csv")}


def valid_name(name, suffix: str = "lbx") -> bool:
    """Nur ein Dateiname mit der Endung `suffix` – keine Ordner, kein Traversal, nicht versteckt."""
    return (isinstance(name, str) and bool(_NAME_RES[suffix].match(name)) and name == Path(name).name
            and ".." not in name and name == name.strip() and not name.endswith((" ", ".")))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_atomic(target: Path, data: bytes) -> None:
    """Erst in eine versteckte Nachbardatei schreiben, dann ersetzen – nie halbe Dateien."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(target)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise TemplateError(f"{target.name} ist gerade geöffnet (z. B. im P-touch Editor) – dort schließen "
                            f"und erneut versuchen.")
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def backup_once(folder: Path, path: Path) -> Path | None:
    """Sicherung der Vorlage unter <folder>/.backup/ – nur, wenn es noch keine gibt.

    Liefert die neue Sicherung oder None.
    """
    folder = Path(folder) / BACKUP_DIR
    pattern = re.compile(re.escape(path.stem) + r"\.\d{8}-\d{6}\.lbx", re.IGNORECASE)
    if folder.is_dir() and any(pattern.fullmatch(p.name) for p in folder.iterdir()):
        return None
    target = folder / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}.lbx"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def lbx_ok(data_or_path) -> bool:
    """Lässt sich die Datei als vollständige .lbx (ZIP mit label.xml) lesen?"""
    import io
    try:
        src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
        with zipfile.ZipFile(src) as z:
            return "label.xml" in z.namelist() and z.testzip() is None
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, RuntimeError):
        return False


class Desktop(Protocol):
    def open_linked(self, template: Path, csv_path: Path) -> Path: ...
    def edit_original(self, template: Path, csv_path: Path) -> None: ...
    def open_folder(self, path: Path) -> None: ...


def relink_in_place(path: Path, csv_path: Path) -> None:
    """Vorlage selbst auf `csv_path` verknüpfen. Nur label.xml ändert sich (DB-Pfad), atomar ersetzt."""
    with zipfile.ZipFile(path) as src:
        entries = [(item, src.read(item.filename)) for item in src.infolist()]
    out = []
    for item, data in entries:   # erst alles im Speicher prüfen, dann schreiben
        if item.filename == "label.xml":
            data = _relink(data.decode("utf-8"), csv_path.resolve()).encode("utf-8")
        out.append((item, data))
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for item, data in out:
                dst.writestr(item, data)
        tmp.replace(path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise TemplateError(f"{path.name} ist gerade im P-touch Editor geöffnet – dort schließen und erneut versuchen.")


class LocalDesktop:
    """Heutiges Verhalten: Dateien im selben Prozess anlegen und mit dem Standardprogramm öffnen.

    out_dir: Ausgabeordner für verknüpfte Kopien (Unterordner `Vorlagen`); None = Ordner der CSV.
    """

    def __init__(self, opener: Callable[[Path], None] = open_file, out_dir: Path | Callable[[], Path] | None = None):
        self.opener = opener
        self._out_dir = out_dir

    def _out(self, csv_path: Path) -> Path:
        out = self._out_dir() if callable(self._out_dir) else self._out_dir
        return Path(out) if out is not None else Path(csv_path).parent

    def _open(self, path: Path, what: str) -> None:
        try:
            self.opener(path)
        except TemplateError:
            raise
        except Exception as e:  # pragma: no cover
            raise TemplateError(f"{what} konnte nicht gestartet werden: {e}")

    def open_linked(self, template: Path, csv_path: Path) -> Path:
        linked = linked_copy(Path(template), Path(csv_path), self._out(csv_path))
        self._open(linked, "P-touch Editor")
        return linked

    def edit_original(self, template: Path, csv_path: Path) -> None:
        relink_in_place(Path(template), Path(csv_path))
        self._open(Path(template), "P-touch Editor")

    def open_folder(self, path: Path) -> None:
        self._open(Path(path), "Explorer")

    def new_template(self, path: Path) -> None:
        """Lokal liegt die neue Vorlage schon im Vorlagenordner."""

    def remote_backup(self) -> bool:
        """Sichert die Gegenseite die Vorlage vor dem Bearbeiten selbst? (lokal: nein)"""
        return False


def as_desktop(obj) -> Desktop:
    """Desktop-Objekt oder (für ältere Aufrufer/Tests) eine reine opener-Funktion."""
    if hasattr(obj, "edit_original"):
        return obj
    return LocalDesktop(opener=obj)


# ------------------------------------------------------------------ Druckerzustand (Windows)
def _win32_state(name: str) -> dict | None:
    """Spooler-Sicht: {"online", "fehler", "text", "auftraege"} oder None ohne win32print/Drucker."""
    try:
        import win32print
    except ImportError:
        return None
    try:
        h = win32print.OpenPrinter(name)
    except Exception:
        return {"online": False, "fehler": True, "text": "nicht installiert", "auftraege": 0}
    try:
        info = win32print.GetPrinter(h, 2)
    finally:
        win32print.ClosePrinter(h)
    status, attrs = int(info.get("Status") or 0), int(info.get("Attributes") or 0)
    texts = [t for bit, t, _ in _STATUS_TEXT if status & bit]
    blocked = any(status & bit for bit, _, b in _STATUS_TEXT if b)
    # USB-Drucker (Dynamic Print Monitor): Windows setzt WORK_OFFLINE selbst, wenn das Gerät fehlt
    offline = bool(attrs & _ATTR_WORK_OFFLINE) or bool(status & (0x80 | 0x4000))
    return {"online": not offline, "fehler": blocked, "text": ", ".join(texts), "auftraege": int(info.get("cJobs") or 0)}


# win32print: JOB_INFO_1.Status – Bits, bei denen der Auftrag nicht von selbst weiterläuft
_JOB_ERRORS = [
    (0x40, "Band leer oder fehlt"),          # JOB_STATUS_PAPEROUT
    (0x400, "Benutzereingriff nötig"),       # JOB_STATUS_USER_INTERVENTION
    (0x2, "Fehler"),                         # JOB_STATUS_ERROR
    (0x20, "Drucker offline"),               # JOB_STATUS_OFFLINE
    (0x200, "blockiert"),                    # JOB_STATUS_BLOCKED_DEVQ
    (0x1, "angehalten"),                     # JOB_STATUS_PAUSED
]


def print_queue(name: str | None) -> dict | None:
    """Windows-Warteschlange des Druckers nach dem Senden.

    -> {"auftraege", "fehler": Text oder "", "seiten", "seiten_gesamt"}; None ohne win32print/Drucker.
    seiten = an den Drucker übertragene Seiten (≈ Labels) – nicht zwingend schon ausgedruckt.
    Fehler kommen aus dem Auftrag (z. B. „Band leer“) oder aus dem Druckerstatus.
    """
    if not name:
        return None
    try:
        import win32print
    except ImportError:
        return None
    try:
        h = win32print.OpenPrinter(name)
    except Exception:
        return None
    try:
        jobs = win32print.EnumJobs(h, 0, 999, 1) or []
    except Exception:
        jobs = []
    finally:
        win32print.ClosePrinter(h)
    errors, pages, total = [], 0, 0
    for job in jobs:
        status = int(job.get("Status") or 0)
        texts = [t for bit, t in _JOB_ERRORS if status & bit]
        driver = str(job.get("pStatus") or "").strip()     # Text des Brother-Treibers, falls vorhanden
        if driver:
            texts.append(driver)
        errors += [t for t in texts if t not in errors]
        pages += int(job.get("PagesPrinted") or 0)
        total += int(job.get("TotalPages") or 0)
    win = _win32_state(name) or {}
    if jobs and win.get("fehler") and win.get("text") and win["text"] not in errors:
        errors.append(win["text"])
    return {"auftraege": len(jobs), "fehler": ", ".join(errors), "seiten": pages, "seiten_gesamt": total}


def _bpac_online(name: str) -> bool | None:
    """b-PAC Printer.IsPrinterOnline(name) im b-PAC-Thread; None, wenn b-PAC fehlt oder belegt ist."""
    return _bpac_info(name)[0]


def _bpac_info(name: str) -> tuple[bool | None, str | None]:
    """(online, eingelegtes Band wie "12 mm") über b-PAC; (None, None), wenn b-PAC fehlt oder belegt ist."""
    from . import bpac
    try:
        return bpac.run_com(_bpac_info_now, name, timeout=8)   # erster Aufruf startet b-PAC (~2–4 s)
    except Exception:   # Timeout: b-PAC druckt/rendert gerade – dann zählt der Windows-Status
        return None, None


def _bpac_info_now(name: str) -> tuple[bool | None, str | None]:
    from . import bpac
    online = _bpac_online_now(name)
    if not online:
        return online, None
    try:
        client = bpac._com()
        if not bpac._lock.acquire(timeout=2):
            return online, None
        try:
            bpac.com_init()
            mid, mname = bpac._media(client.Dispatch("bpac.Document").Printer)
            return online, (mname or None) if mid else None
        finally:
            bpac._lock.release()
    except Exception:
        return online, None


def _bpac_online_now(name: str) -> bool | None:
    """b-PAC Printer.IsPrinterOnline(name); None, wenn b-PAC fehlt oder gerade druckt."""
    from . import bpac
    try:
        client = bpac._com()
    except Exception:
        return None
    if not bpac._lock.acquire(timeout=2):   # b-PAC verträgt keine parallelen Aufrufe
        return None
    try:
        bpac.com_init()
        doc = client.Dispatch("bpac.Document")
        return bool(bpac._call(doc.Printer, "IsPrinterOnline", name))
    except Exception:
        return None
    finally:
        bpac._lock.release()


def _drive_remote(letter: str) -> str | None:
    """UNC-Ziel eines Netzlaufwerks: erst live (WNetGetConnection), sonst die gespeicherte Zuordnung
    unter HKCU\\Network\\<Buchstabe> – die gibt es auch, wenn das Laufwerk gerade nicht verbunden ist."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.mpr.WNetGetConnectionW(f"{letter}:", buf, ctypes.byref(size)) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Network\{letter}") as key:
            return str(winreg.QueryValueEx(key, "RemotePath")[0]) or None
    except OSError:
        return None


def unc_path(path: str | Path, remote=_drive_remote) -> str:
    """Z:\\Ordner -> \\\\Server\\Freigabe\\Ordner, wenn Z: ein Netzlaufwerk ist; sonst unverändert.

    Laufwerksbuchstaben gelten nur in der Anmeldesitzung, die sie verbunden hat. Ein mit Admin-Rechten
    oder sehr früh beim Anmelden gestarteter Helfer sieht sie nicht – der UNC-Pfad funktioniert immer.
    """
    text = str(path or "")
    m = re.match(r"^([A-Za-z]):[\\/]?(.*)$", text)
    if not m:
        return text
    target = remote(m.group(1).upper())
    if not target:
        return text
    rest = m.group(2).replace("/", "\\").strip("\\")
    return target.rstrip("\\") + ("\\" + rest if rest else "")


def printer_state(name: str | None) -> dict:
    """Ist der Drucker wirklich verbunden (nicht nur installiert)?

    -> {"name", "angeschlossen": bool|None, "status_text", "bereit": bool}. Maßgeblich ist
    b-PAC IsPrinterOnline, sonst das Spooler-Attribut WORK_OFFLINE (Windows setzt es bei
    USB-Druckern automatisch, sobald das Gerät fehlt). None = unbekannt (kein Windows).
    """
    if not name:
        return {"name": None, "angeschlossen": None, "status_text": "Kein Drucker gewählt", "bereit": False}
    win = _win32_state(name)
    if win is None:
        return {"name": name, "angeschlossen": None, "bereit": False,
                "status_text": "Druckerstatus nur unter Windows verfügbar"}
    if win.get("text") == "nicht installiert":
        return {"name": name, "angeschlossen": False, "bereit": False,
                "status_text": f"Drucker '{name}' ist unter Windows nicht installiert"}
    online, band = _bpac_info(name)
    if online is None:
        online = win["online"]
    if not online:
        text = "nicht verbunden oder ausgeschaltet"
    elif win["fehler"]:
        text = win["text"]
    else:
        text = "bereit" + (f" ({win['text']})" if win["text"] else "")
    if win["auftraege"]:
        text += f", {win['auftraege']} Auftrag/Aufträge in der Warteschlange"
    return {"name": name, "angeschlossen": bool(online), "status_text": text,
            "bereit": bool(online) and not win["fehler"], "band": band if online else None}


def default_printer(names: list[str]) -> str | None:
    """Drucker, den b-PAC ohne ausdrückliche Wahl nimmt: bevorzugt ein P-touch, sonst der erste."""
    names = [str(n) for n in names or [] if n]
    return next((n for n in names if "PT-" in n.upper()), names[0] if names else None)


def require_online(name: str | None, state_fn: Callable[[str | None], dict] = printer_state) -> None:
    """Druck nur an einen wirklich verbundenen Drucker – sonst bliebe der Auftrag in der
    Windows-Warteschlange hängen und käme beim nächsten Einschalten unerwartet heraus."""
    if not name:
        return
    from .bpac import BpacError
    state = state_fn(name)
    if state.get("angeschlossen") is False:
        raise BpacError(f"Drucker '{name}' ist nicht verbunden oder ausgeschaltet ({state.get('status_text') or 'offline'}) "
                        f"– Auftrag wurde NICHT gesendet. Drucker einschalten bzw. per USB anschließen.")
