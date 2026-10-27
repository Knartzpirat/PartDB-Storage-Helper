"""P-touch-Vorlagen (.lbx) lesen: Band, Länge, Objekte mit Position, Schrift und CSV-Spalte.

Eine .lbx ist ein ZIP mit label.xml. Koordinaten stehen in pt (1 pt = 0,3528 mm).
Bei orientation="landscape" ist paper.width die Bandbreite und paper.height die
Labellänge; Objekt-x läuft entlang der Länge, Objekt-y quer über das Band.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

PT_TO_MM = 25.4 / 72

NS = {
    "pt": "http://schemas.brother.info/ptouch/2007/lbx/main",
    "style": "http://schemas.brother.info/ptouch/2007/lbx/style",
    "text": "http://schemas.brother.info/ptouch/2007/lbx/text",
    "barcode": "http://schemas.brother.info/ptouch/2007/lbx/barcode",
    "draw": "http://schemas.brother.info/ptouch/2007/lbx/draw",
    "image": "http://schemas.brother.info/ptouch/2007/lbx/image",
}
_PREFIX = {v: k for k, v in NS.items()}


def _pt(value: str | None) -> float:
    m = re.match(r"\s*(-?[\d.]+)", value or "")
    return float(m.group(1)) if m else 0.0


@dataclass
class LbxObject:
    index: int          # Position in der Objektliste der Vorlage – NICHT der b-PAC-Index
                        # (b-PAC lässt Formen wie draw:poly weg, siehe bpac.object_map)
    name: str           # objectName, in deinen Vorlagen nicht eindeutig
    kind: str           # "text", "barcode", "symbol", "draw", "image", ...
    field: str          # verknüpfte CSV-Spalte oder ""
    x: float            # pt, entlang der Labellänge (landscape)
    y: float            # pt, quer über das Band
    width: float
    height: float
    angle: int = 0      # 0/90/180/270
    text: str = ""      # gespeicherter Text (Beispielwert aus der Vorlage)
    font: str = ""
    font_size: float = 0.0   # pt
    bold: bool = False
    shrink: bool = False     # Text wird verkleinert, wenn er nicht passt
    align_h: str = ""        # LEFT/CENTER/RIGHT/JUSTIFY
    align_v: str = ""        # TOP/CENTER/BOTTOM
    barcode: str = ""        # z. B. DATAMATRIX, CODE128


@dataclass
class LbxTemplate:
    file: str
    tape_pt: float          # Bandbreite
    length_pt: float        # Labellänge
    margins: dict           # left/top/right/bottom in pt (wie in der Datei)
    orientation: str
    printer: str
    database: str           # verknüpfte CSV (Pfad aus der Vorlage)
    objects: list[LbxObject] = field(default_factory=list)

    @property
    def tape_mm(self) -> int:
        """Nominelle Bandbreite (3,5/6/9/12/18/24 mm) – Papierbreite ist kleiner als das Band."""
        mm = self.tape_pt * PT_TO_MM
        return min((3.5, 6, 9, 12, 18, 24, 36), key=lambda t: abs(t - mm) if t >= mm - 1 else 99)

    @property
    def length_mm(self) -> float:
        return round(self.length_pt * PT_TO_MM, 1)

    @property
    def fields(self) -> list[str]:
        return sorted({o.field for o in self.objects if o.field}, key=_field_sort)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(tape_mm=self.tape_mm, length_mm=self.length_mm, fields=self.fields)
        return d


def _field_sort(name: str):
    m = re.match(r"(\D+)(\d+)$", name)
    return (m.group(1), int(m.group(2))) if m else (name, 0)


def _attr(el, path: str, name: str, default: str = "") -> str:
    found = el.find(path, NS)
    return found.get(name, default) if found is not None else default


def read_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("label.xml").decode("utf-8")


def parse(path: str | Path) -> LbxTemplate:
    path = Path(path)
    root = ET.fromstring(read_xml(path))
    paper = root.find(".//style:paper", NS)
    db = root.find(".//{%s}database" % "http://schemas.brother.info/ptouch/2007/lbx/database")
    tpl = LbxTemplate(
        file=path.name,
        tape_pt=_pt(paper.get("width")),
        length_pt=_pt(paper.get("height")),
        margins={k: _pt(paper.get("margin" + k.capitalize())) for k in ("left", "top", "right", "bottom")},
        orientation=paper.get("orientation", ""),
        printer=paper.get("printerName", ""),
        database=db.get("databasePath", "") if db is not None else "",
    )
    objects = root.find(".//pt:objects", NS)
    for i, el in enumerate(list(objects) if objects is not None else []):
        style = el.find("pt:objectStyle", NS)
        exp = style.find("pt:expanded", NS) if style is not None else None
        ns, _, local = el.tag[1:].partition("}")
        kind = local if _PREFIX.get(ns) in ("text", "barcode") else f"{_PREFIX.get(ns, '?')}:{local}"
        if kind == "text" and _PREFIX.get(ns) == "text" and local != "text":
            kind = local
        tpl.objects.append(LbxObject(
            index=i,
            name=exp.get("objectName", "") if exp is not None else "",
            kind=kind,
            field=exp.get("dbMergeFieldStyleName", "") if exp is not None else "",
            x=_pt(style.get("x")) if style is not None else 0,
            y=_pt(style.get("y")) if style is not None else 0,
            width=_pt(style.get("width")) if style is not None else 0,
            height=_pt(style.get("height")) if style is not None else 0,
            angle=int(_pt(style.get("angle"))) if style is not None else 0,
            text=(el.findtext("pt:data", "", NS) or ""),
            font=_attr(el, "text:ptFontInfo/text:logFont", "name"),
            font_size=_pt(_attr(el, "text:ptFontInfo/text:fontExt", "size")),
            bold=_attr(el, "text:ptFontInfo/text:logFont", "weight", "400") not in ("", "400", "0"),
            shrink=_attr(el, "text:textControl", "shrink") == "true",
            align_h=_attr(el, "text:textAlign", "horizontalAlignment"),
            align_v=_attr(el, "text:textAlign", "verticalAlignment"),
            barcode=_attr(el, "barcode:barcodeStyle", "protocol"),
        ))
    return tpl
