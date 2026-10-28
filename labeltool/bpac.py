"""Optionaler Direktdruck über Brother b-PAC (nur Windows, b-PAC Client + pywin32 nötig).

Die Vorlagen sind per Datenbank mit CSV-Spalten verknüpft. b-PAC kann diese
Verknüpfung nicht nutzen, deshalb liest das Tool aus label.xml, welches Objekt
welche Spalte zeigt, und setzt die Texte vor jedem Label selbst. Objektnamen sind in
den Vorlagen nicht eindeutig (z. B. zweimal "Text2"), also wird über die Position
in der Objektliste gearbeitet und vorher abgeglichen, dass b-PAC dieselbe Reihenfolge
sieht (b-PAC lässt nur Formen wie draw:poly weg, s. object_map).

Achtung late binding: b-PAC liefert keine Typinformation (makepy/EnsureDispatch geht
nicht: "can not automate the makepy process"). pywin32 ruft deshalb argumentlose
Methoden schon beim Attributzugriff auf – ``doc.Close()`` schließt das Dokument und
wirft danach "'bool' object is not callable". Argumentlose Methoden (Close, EndPrint,
GetInstalledPrinters) laufen daher über _call() mit DISPATCH_METHOD.

Vorschau: b-PAC rendert die Vorlage selbst (Export als 1-bit-BMP), das BMP wird ohne
Zusatzpaket in PNG umgewandelt.
"""
from __future__ import annotations

import contextlib
import functools
import os
import re
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# b-PAC PrintOptionConstants
CUT_OPTIONS = {
    "autocut": 0x1,          # nach jedem Label schneiden
    "cutmark": 0x2,
    "halfcut": 0x200,
    "chainprint": 0x400,     # kein Vorschub am Ende -> spart Band beim nächsten Druck
    "tailcut": 0x800,
    "cutatend": 0x4000000,   # nach dem letzten Label schneiden
}

# Erlaubte Schnittoptionen je Modellgruppe laut b-PAC-Hilfe ("Printout samples and cut option
# combinations for each major device type"). Für diese Geräte gibt es weder "Schnitt am Ende" noch
# Halbschnitt/Schnittmarke – nur Autocut, Kettendruck, beides oder keinen Schnitt.
CUT_SUPPORT = {
    ("PT-2430PC", "PT-2730", "PT-P700", "PT-P710BT", "PT-P715EBT", "PT-H500", "PT-E500", "PT-D600",
     "PT-D610BT", "PT-E720BT"): {"autocut", "chainprint"},
}
_ALL_CUT_BITS = 0
for _v in CUT_OPTIONS.values():
    _ALL_CUT_BITS |= _v


def supported_cuts(printer_name: str) -> set[str] | None:
    """Erlaubte Schnittoptionen für den Drucker (Name enthält das Modell) oder None = unbekannt."""
    name = (printer_name or "").upper()
    for models, cuts in CUT_SUPPORT.items():
        if any(re.search(rf"(?<![A-Z0-9-]){re.escape(m)}(?![A-Z0-9])", name) for m in models):
            return cuts
    return None


def flags_for_printer(flags: int, printer_name: str) -> int:
    """Nicht unterstützte Schnitt-Bits entfernen – sonst verhält sich z. B. der PT-P710BT unerwartet."""
    cuts = supported_cuts(printer_name)
    if cuts is None:
        return flags
    allowed = 0
    for key in cuts:
        allowed |= CUT_OPTIONS[key]
    return (flags & ~_ALL_CUT_BITS) | (flags & allowed)


# Druckqualität (PrintOptionConstants aus der b-PAC-Typbibliothek): bpoHighResolution = 0x2000000
# (z. B. PT-P710BT: 180 × 360 dpi statt 180 × 180), bpoQuality = 0x10000 (Qualität vor Tempo).
# Modelle ohne hohe Auflösung drucken mit der Standardauflösung.
QUALITY_OPTIONS = {
    "standard": 0,
    "hoch": 0x2000000,
    "hoch_langsam": 0x2000000 | 0x10000,
}

# b-PAC ExportType (empirisch): 1 = .lbx, 2 = .lbl, 3 = .lbi, 4 = .bmp (1 bit, dpi frei
# wählbar), 5 = Text (UTF-16); 0 und 6 schlagen fehl. GetImageData(4, Breite_px, Höhe_px)
# liefert ein 24-bit-BMP (0, 0 = 180 dpi), alle anderen Formate dort None. PNG kann b-PAC nicht.
EXPORT_BMP = 4
PREVIEW_DPI = 360            # doppelte Druckerauflösung (180 dpi) – scharf für die Anzeige

# Download: Abschnitt "b-PAC Client Component Only", 64-bit (.msi) – passend zum 64-bit-Python
DOWNLOAD_URL = "https://support.brother.com/g/s/es/dev/en/bpac/download/index.html"

_lock = threading.Lock()     # b-PAC verträgt keine parallelen Aufträge
_com_threads = threading.local()
_avail_lock = threading.Lock()
_avail_cache: tuple[float, bool] | None = None
AVAIL_TTL = 60.0             # s – preview_available() wird pro Vorschau abgefragt


class BpacError(Exception):
    pass


def _mark_com_thread() -> None:
    _com_threads.is_com = True


# Alle b-PAC-Aufrufe laufen in genau diesem einen Thread (= ein COM-Apartment). Mit mehreren
# Threads (z. B. Statusabfrage + Vorschau) blockierte Dispatch("bpac.Document") im zweiten
# Thread dauerhaft, weil b-PAC den ersten, nicht pumpenden Thread braucht.
_com_executor = ThreadPoolExecutor(1, thread_name_prefix="bpac-com", initializer=_mark_com_thread)


def run_com(fn, *args, timeout: float | None = None, **kwargs):
    """fn im b-PAC-Thread ausführen (direkt, wenn wir schon dort sind)."""
    if getattr(_com_threads, "is_com", False):
        return fn(*args, **kwargs)
    return _com_executor.submit(fn, *args, **kwargs).result(timeout)


def on_com_thread(fn):
    """Dekorator: Funktion immer im b-PAC-Thread ausführen."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run_com(fn, *args, **kwargs)
    return wrapper


def com_init() -> None:
    """COM einmal pro Thread initialisieren und NICHT pro Aufruf wieder abbauen.

    Würde CoUninitialize nach jedem Aufruf laufen, gäbe Python b-PAC-Objekte (Variablen,
    Zwischenobjekte wie doc.Printer) erst danach frei – in einem schon abgebauten
    COM-Kontext ("Win32 exception occurred releasing IUnknown"). Danach blockierte das
    nächste Dispatch("bpac.Document") dauerhaft. Worker-Threads behalten ihren Kontext.
    """
    if not getattr(_com_threads, "ok", False):
        import pythoncom
        pythoncom.CoInitialize()
        _com_threads.ok = True


def print_flags(names: list[str], quality: str = "standard") -> int:
    if quality not in QUALITY_OPTIONS:
        raise BpacError(f"Unbekannte Druckqualität '{quality}'. Möglich: {', '.join(QUALITY_OPTIONS)}")
    flags = QUALITY_OPTIONS[quality]
    for n in names:
        key = str(n).strip().lower()
        if key not in CUT_OPTIONS:
            raise BpacError(f"Unbekannte Schnittoption '{n}'. Möglich: {', '.join(CUT_OPTIONS)}")
        flags |= CUT_OPTIONS[key]
    return flags


def merge_fields(template: Path) -> list[tuple[str, str]]:
    """[(Objektname, CSV-Spalte oder "")] in der Reihenfolge der Vorlage."""
    with zipfile.ZipFile(template) as z:
        xml = z.read("label.xml").decode("utf-8")
    out = []
    for m in re.finditer(r"<pt:expanded ([^>]*)>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        out.append((attrs.get("objectName", ""), attrs.get("dbMergeFieldStyleName", "")))
    return out


def _com():
    try:
        import pythoncom  # noqa: F401
        import win32com.client
    except ImportError:
        raise BpacError("pywin32 fehlt – in der venv 'pip install pywin32' ausführen.")
    return win32com.client


def _call(obj, name: str, *args):
    """Methode ausdrücklich als Methode aufrufen (late binding, s. Modulkommentar)."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:                       # kein COM-Objekt (Tests mit Attrappen)
        return getattr(obj, name)(*args)
    import pythoncom
    return ole.Invoke(ole.GetIDsOfNames(name), 0, pythoncom.DISPATCH_METHOD, True, *args)


def _close(doc) -> None:
    try:
        _call(doc, "Close")
    except Exception:
        pass                              # Schließen darf den eigentlichen Fehler nicht verdecken


def _dispatch(client):
    try:
        return client.Dispatch("bpac.Document")
    except Exception:
        raise BpacError("Brother b-PAC Client ist nicht installiert.")


def object_map(fields: list[tuple[str, str]], names: list[str]) -> list[tuple[int, str]] | None:
    """Vorlagenobjekte den b-PAC-Objekten zuordnen: [(b-PAC-Index, CSV-Spalte)] für verknüpfte Objekte.

    b-PAC lässt Formen (draw:poly, Linien …) in doc.Objects weg, sonst ist die Reihenfolge
    gleich. Deshalb muss die b-PAC-Liste eine Teilfolge der Vorlage sein; übersprungen
    werden dürfen nur Objekte ohne CSV-Spalte. None, wenn das nicht aufgeht.
    """
    out, j = [], 0
    for name, field in fields:
        if j < len(names) and names[j] == name:
            if field:
                out.append((j, field))
            j += 1
        elif field:
            return None                   # verknüpftes Objekt fehlt in b-PAC
    return out if j == len(names) else None


def _open(doc, template: Path, fields: list[tuple[str, str]]) -> tuple[object, list[tuple[int, str]]]:
    """Vorlage öffnen und die Objekte mit b-PAC abgleichen -> (doc.Objects, object_map).

    Bei Fehler ist das Dokument wieder geschlossen; sonst schließt der Aufrufer.
    """
    if not doc.Open(str(template.resolve())):
        raise BpacError(f"b-PAC konnte {template.name} nicht öffnen.")
    try:
        objects = doc.Objects
        names = [objects.Item(i).Name for i in range(objects.Count)]
    except Exception as e:
        _close(doc)
        raise BpacError(f"b-PAC konnte die Objekte von {template.name} nicht lesen: {e}") from e
    mapping = object_map(fields, names)
    if mapping is None:
        _close(doc)
        raise BpacError(f"Objekte in {template.name} passen nicht zur erwarteten Reihenfolge – "
                        f"bitte CSV-Weg nutzen. b-PAC: {names}")
    return objects, mapping


def _fill(objects, mapping: list[tuple[int, str]], row: dict) -> None:
    # .Text greift auch bei Barcode-Objekten (doc.SetBarcodeData liefert hier nur False)
    for i, field in mapping:
        objects.Item(i).Text = str(row.get(field, "") or "")


@on_com_thread
def status(printer: str) -> dict:
    """Ist Direktdruck möglich? Liefert {"ok": bool, "grund": str, "drucker": [...]}."""
    try:
        client = _com()
    except BpacError as e:
        return {"ok": False, "grund": str(e), "drucker": [], "hilfe_url": ""}
    com_init()
    try:
        doc = client.Dispatch("bpac.Document")
    except Exception:
        return {"ok": False, "drucker": [], "hilfe_url": DOWNLOAD_URL,
                "grund": "Brother b-PAC Client ist nicht installiert. Download: „b-PAC Client Component Only“, "
                         "64-bit-Version (.msi), danach das Tool neu starten."}
    try:
        names = [str(p) for p in (_call(doc.Printer, "GetInstalledPrinters") or [])]
    except Exception:
        names = []
    if printer and printer not in names:
        return {"ok": False, "drucker": names, "hilfe_url": "",
                "grund": f"Drucker '{printer}' nicht gefunden. Installiert: {', '.join(names) or 'keiner'}"}
    if not printer and not names:
        return {"ok": False, "drucker": names, "hilfe_url": "",
                "grund": "Kein Brother-Etikettendrucker installiert – PT-P710BT per USB anschließen "
                         "und Treiber installieren."}
    return {"ok": True, "grund": "", "drucker": names, "hilfe_url": ""}


# Symbole aus den Schriften "PTC Dingbats…" bringt nur der P-touch Editor 6 intern mit – Windows hat nur die
# älteren "PT Dingbats", deren Zeichen dort fehlen. b-PAC zeichnet solche Symbole deshalb als leeres Kästchen.
# Normalerweise lädt ptouch_fonts die Schriften aus der lokalen P-touch-Installation, dann bleibt alles wie
# in der Vorlage. Nur wenn eine Schrift fehlt (kein P-touch Editor 6), ersetzen wir schmale, strichförmige
# Symbole (typisch: Trennstrich zwischen Feldern) in einer temporären Kopie durch eine echte Linie.
_CLIPART_RE = re.compile(r"<image:clipart>(?:(?!</image:clipart>).)*?originalName=\"FONT,(PTC [^,\"]*)[^\"]*\""
                         r"(?:(?!</image:clipart>).)*</image:clipart>", re.S)
_STYLE_RE = re.compile(r'<pt:objectStyle x="([\d.]+)pt" y="([\d.]+)pt" width="([\d.]+)pt" height="([\d.]+)pt"')
_NAME_RE = re.compile(r'objectName="([^"]*)"')
LINE_RATIO = 0.35          # Breite/Höhe (bzw. umgekehrt) darunter gilt ein Symbol als Strich
LINE_WIDTH = 0.5           # pt – wie die Linien in den eigenen Vorlagen


def _line_xml(name: str, x: float, y: float, w: float, h: float) -> str:
    """draw:poly-Linie mittig im Rahmen des Symbols (senkrecht oder waagerecht)."""
    if h >= w:
        x1 = x2 = x + w / 2
        y1, y2 = y, y + h
        box = (x1 - LINE_WIDTH / 2, y, LINE_WIDTH, h)
    else:
        y1 = y2 = y + h / 2
        x1, x2 = x, x + w
        box = (x, y1 - LINE_WIDTH / 2, w, LINE_WIDTH)
    bx, by, bw, bh = (f"{v:.1f}pt" for v in box)
    return (f'<draw:poly><pt:objectStyle x="{bx}" y="{by}" width="{bw}" height="{bh}" backColor="#FFFFFF" '
            f'backPrintColorNumber="0" ropMode="COPYPEN" angle="0" anchor="TOPLEFT" flip="NONE">'
            f'<pt:pen style="INSIDEFRAME" widthX="{LINE_WIDTH}pt" widthY="{LINE_WIDTH}pt" color="#000000" '
            f'printColorNumber="1"></pt:pen><pt:brush style="NULL" color="#000000" printColorNumber="1" id="0">'
            f'</pt:brush><pt:expanded objectName="{name}" ID="0" lock="2" templateMergeTarget="LABELLIST" '
            f'templateMergeType="NONE" templateMergeID="0" allowOutOfBoundsTransfer="false" linkStatus="NONE" '
            f'linkID="0"></pt:expanded></pt:objectStyle><draw:polyStyle shape="LINE" arrowBegin="SQUARE" '
            f'arrowEnd="SQUARE"><draw:polyOrgPos x="{bx}" y="{by}" width="{bw}" height="{bh}"></draw:polyOrgPos>'
            f'<draw:polyLinePoints points="{x1:.1f}pt,{y1:.1f}pt {x2:.1f}pt,{y2:.1f}pt"></draw:polyLinePoints>'
            f'</draw:polyStyle></draw:poly>')


def fix_symbols(xml: str, available=()) -> tuple[str, int]:
    """Strichförmige PTC-Dingbats-Symbole durch Linien ersetzen, außer ihre Schrift ist geladen
    (available = Familiennamen). -> (xml, Anzahl ersetzt)."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        if m.group(1) in available:                 # Schrift da -> b-PAC zeichnet das Original
            return m.group(0)
        st = _STYLE_RE.search(m.group(0))
        if not st:
            return m.group(0)
        x, y, w, h = (float(v) for v in st.groups())
        if min(w, h) > LINE_RATIO * max(w, h):      # eher quadratisch -> echtes Symbol, nicht anfassen
            return m.group(0)
        count += 1
        name = _NAME_RE.search(m.group(0))
        return _line_xml(name.group(1) if name else "", x, y, w, h)

    return _CLIPART_RE.sub(repl, xml), count


@contextlib.contextmanager
def _renderable(template: Path):
    """Pfad der Vorlage, die b-PAC öffnet – bei Bedarf eine Kopie mit Linien statt Strich-Symbolen."""
    from . import ptouch_fonts
    with zipfile.ZipFile(template) as z:
        xml, n = fix_symbols(z.read("label.xml").decode("utf-8"), ptouch_fonts.load())
        if not n:
            yield template
            return
        with tempfile.TemporaryDirectory(prefix="labeltool-") as tmp:
            copy = Path(tmp) / template.name
            with zipfile.ZipFile(copy, "w", zipfile.ZIP_DEFLATED) as out:
                for item in z.infolist():
                    out.writestr(item, xml.encode("utf-8") if item.filename == "label.xml" else z.read(item.filename))
            yield copy


@on_com_thread
def print_labels(template: Path, rows: list[dict], printer: str, flags: int) -> int:
    """Druckt ein Label je Zeile mit der Originalvorlage (wird nicht gespeichert)."""
    if not template.is_file():
        raise BpacError(f"Vorlage nicht gefunden: {template}")
    client = _com()

    with _lock, _renderable(Path(template)) as source:
        fields = merge_fields(source)
        com_init()
        doc = _dispatch(client)
        # 1) Vorlage prüfen (Objektabgleich) und Drucker bestimmen – noch ohne Druckauftrag
        _, mapping = _open(doc, source, fields)
        try:
            if printer:
                # SetPrinter meldet auch bei unbekanntem Namen True und druckt dann auf den
                # Drucker aus der Vorlage – deshalb vorher gegen die installierten prüfen.
                installed = [str(p) for p in (_call(doc.Printer, "GetInstalledPrinters") or [])]
                if printer not in installed:
                    raise BpacError(f"Drucker '{printer}' konnte nicht gewählt werden. "
                                    f"Installiert: {', '.join(installed) or 'keiner'}")
            try:
                name = printer or str(_call(doc, "GetPrinterName") or "")
            except Exception:
                name = printer
        finally:
            _close(doc)
        if not rows:
            return 0
        flags = flags_for_printer(flags, name)

        # 2) EIN Auftrag für alle Labels, wie im b-PAC-Beispiel "Continue to print different templates":
        #    erst StartPrint, dann je Label Open -> Texte -> PrintOut, zum Schluss EndPrint. Wird die
        #    Vorlage vor StartPrint geöffnet, wird jedes PrintOut ein eigener Auftrag ("1 von 1") –
        #    mit Vorlauf und Schnitt vor jedem Label. Optionen nur bei StartPrint (PrintOut: 0).
        if not doc.StartPrint("", flags):
            raise BpacError(f"Druckstart fehlgeschlagen (Fehler {doc.ErrorCode}).")
        try:
            for row in rows:
                objects, _ = _open(doc, source, fields)
                if printer and not doc.SetPrinter(printer, True):
                    raise BpacError(f"Drucker '{printer}' konnte nicht gewählt werden.")
                _fill(objects, mapping, row)
                if not doc.PrintOut(1, 0):
                    raise BpacError(f"Label konnte nicht in den Druckauftrag (Fehler {doc.ErrorCode}).")
            if not _call(doc, "EndPrint"):
                raise BpacError(f"Drucken fehlgeschlagen (Fehler {doc.ErrorCode}). Drucker an und Band eingelegt?")
        finally:
            _close(doc)
    return len(rows)


@on_com_thread
def preview_available() -> bool:
    """Kann b-PAC eine Vorschau rendern? Drucker nicht nötig; Ergebnis 60 s gecacht."""
    global _avail_cache
    with _avail_lock:
        now = time.monotonic()
        if _avail_cache is not None and now - _avail_cache[0] < AVAIL_TTL:
            return _avail_cache[1]
        ok = False
        try:
            client = _com()
            com_init()
            client.Dispatch("bpac.Document")
            ok = True
        except Exception:
            ok = False
        _avail_cache = (now, ok)
        return ok


@on_com_thread
def render_png(template: Path, row: dict, dpi: int = PREVIEW_DPI) -> bytes:
    """Rendert ein Label mit den Werten aus row über b-PAC und liefert PNG-Bytes.

    Die Originalvorlage wird nur geöffnet, nie gespeichert. Das Bild ist quer und zeigt
    das ganze Band inkl. Rändern, so wie es aus dem Drucker kommt (schwarz auf weiß).
    """
    template = Path(template)
    if not template.is_file():
        raise BpacError(f"Vorlage nicht gefunden: {template}")
    client = _com()

    with _lock, _renderable(template) as source:
        com_init()
        try:
            fields = merge_fields(source)
            doc = _dispatch(client)
            objects, mapping = _open(doc, source, fields)
            try:
                _fill(objects, mapping, row)
                with tempfile.TemporaryDirectory(prefix="labeltool-") as tmp:
                    path = os.path.join(tmp, "vorschau.bmp")
                    if not doc.Export(EXPORT_BMP, path, int(dpi)) or not os.path.isfile(path):
                        raise BpacError(f"b-PAC konnte keine Vorschau erzeugen (Fehler {doc.ErrorCode}).")
                    with open(path, "rb") as f:
                        bmp = f.read()
            finally:
                _close(doc)
        except BpacError:
            raise
        except Exception as e:
            raise BpacError(f"Vorschau über b-PAC fehlgeschlagen: {e}") from e
    return bmp_to_png(bmp, dpi)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def bmp_to_png(bmp: bytes, dpi: int = PREVIEW_DPI) -> bytes:
    """Unkomprimiertes BMP (1/24/32 bit) in PNG umwandeln – ohne Zusatzpaket.

    1 bit wird als 1-bit-Graustufen-PNG geschrieben (klein und schnell), sonst RGB.
    """
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise BpacError("b-PAC hat kein BMP geliefert.")
    offset, header_size = struct.unpack_from("<II", bmp, 10)
    width, height, _, bpp, compression = struct.unpack_from("<iiHHI", bmp, 18)
    if compression not in (0, 3) or bpp not in (1, 24, 32) or width <= 0 or height == 0:
        raise BpacError(f"Nicht unterstütztes BMP von b-PAC ({bpp} bit, Kompression {compression}).")
    bottom_up = height > 0
    height = abs(height)
    stride = (width * bpp + 31) // 32 * 4
    if len(bmp) < offset + stride * height:
        raise BpacError("BMP von b-PAC ist unvollständig.")
    order = range(height - 1, -1, -1) if bottom_up else range(height)

    raw = bytearray()
    if bpp == 1:
        colors = struct.unpack_from("<I", bmp, 46)[0] or 2
        start = 14 + header_size
        bright = [sum(bmp[start + 4 * i: start + 4 * i + 3]) for i in range(min(colors, 2))]
        bright += [765] * (2 - len(bright))
        # PNG-Graustufen 1 bit: Bit 0 = schwarz, 1 = weiß; b-PAC liefert Palette [schwarz, weiß]
        table = bytes(255 - i for i in range(256)) if bright[0] > bright[1] else None
        row_bytes = (width + 7) // 8
        for y in order:
            line = bmp[offset + y * stride: offset + y * stride + row_bytes]
            raw += b"\0" + (line.translate(table) if table else line)
        ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    else:
        step = bpp // 8
        for y in order:
            line = bmp[offset + y * stride: offset + y * stride + width * step]
            px = bytearray(width * 3)
            px[0::3] = line[2::step]
            px[1::3] = line[1::step]
            px[2::3] = line[0::step]
            raw += b"\0" + px
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    dpm = round(dpi / 0.0254)             # Pixel pro Meter – Bildbetrachter zeigen Originalgröße
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"pHYs", struct.pack(">IIB", dpm, dpm, 1))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _png_chunk(b"IEND", b""))
