"""Vorschau: SVG-Skizze aus der .lbx, PNG über b-PAC (live nur wenn verfügbar) und der
b-PAC-Ablauf (Objektabgleich, Close) mit Attrappen ohne COM."""
import struct
import sys
import types
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

from labeltool import bpac, lbx, preview_svg

ROOT = Path(__file__).resolve().parents[1]
BEISPIELE = Path(__file__).resolve().parent / "vorlagen"
BOX = BEISPIELE / "Layout_Lagerbox_PartDB.lbx"
SMD15 = BEISPIELE / "PartDB_SMD15.lbx"
SVG = "{http://www.w3.org/2000/svg}"

BOX_ROW = {"Lagername": "MIC-2.0021", "Barcodeinhalt": "S0204", "Lagerinhalt": "Dübel 6x30",
           "Lagerabteile": "4", "Lagerfarbe": "Grün", "Lagerart": "WinBin3"}


def _texts(root) -> list[str]:
    return ["".join(t.itertext()) for t in root.iter(SVG + "text")]


def _group(root, index: int):
    return next(g for g in root.iter(SVG + "g") if g.get("data-index") == str(index))


# --- SVG-Skizze ---------------------------------------------------------------------------

@pytest.mark.parametrize("template", sorted(BEISPIELE.glob("*.lbx")), ids=lambda p: p.stem)
def test_svg_wohlgeformt_fuer_alle_vorlagen(template):
    tpl = lbx.parse(template)
    row = {f: f"Wert-{f}" for f in tpl.fields}
    root = ET.fromstring(preview_svg.render_svg(template, row))
    assert root.tag == SVG + "svg"
    x, y, w, h = map(float, root.get("viewBox").split())
    assert w > h                                   # Labels sind quer
    assert w == pytest.approx(tpl.length_pt + 2 * preview_svg.PAD)
    alles = " ".join(_texts(root))
    for f in tpl.fields:
        assert f"Wert-{f}" in alles


def test_svg_zeigt_werte_und_statische_texte():
    root = ET.fromstring(preview_svg.render_svg(BOX, BOX_ROW))
    texte = _texts(root)
    for wert in ("MIC-2.0021", "Dübel 6x30", "Grün", "WinBin3", "S0204"):
        assert wert in texte
    for statisch in ("Inhalt", "Fächer", "Farbe", "Art"):
        assert statisch in texte
    assert "Microcontroller" not in texte          # alter Vorlagentext ist ersetzt
    # Barcodes als Platzhalter mit Wert, DataMatrix als Raster
    barcodes = [g for g in root.iter(SVG + "g") if "barcode" in g.get("class", "").split()]
    assert {g.get("data-wert") for g in barcodes} == {"S0204", "MIC-2.0021"}
    assert all(g.find(SVG + "path") is not None for g in barcodes)
    # Form (Trennlinie) dezent als Linie
    assert root.find(f".//{SVG}line") is not None
    # Label weiß, Schrift schwarz – unabhängig vom Theme
    band = next(r for r in root.iter(SVG + "rect") if r.get("class") == "band")
    assert band.get("fill") == "#fff"


def test_svg_escaping():
    boese = 'Schrauben & <Muttern> "M3" \x01\'x\''
    svg = preview_svg.render_svg(BOX, {**BOX_ROW, "Lagerinhalt": boese, "Barcodeinhalt": "<&>"})
    root = ET.fromstring(svg)                      # wäre sonst kein gültiges XML
    assert 'Schrauben & <Muttern> "M3" \'x\'' in _texts(root)
    assert "<Muttern>" not in svg
    assert any(g.get("data-wert") == "<&>" for g in root.iter(SVG + "g"))


def test_svg_rotation():
    root = ET.fromstring(preview_svg.render_svg(SMD15, {"Location": "SMDICs-1.0013"}))
    tpl = lbx.parse(SMD15)
    gedreht = [o for o in tpl.objects if o.angle]
    assert gedreht
    for o in gedreht:
        g = _group(root, o.index)
        assert f"rotate({o.angle})" in g.get("transform", "")
    assert "rotate(" not in (_group(root, 2).get("transform") or "")


def test_svg_ueberlauf_shrink_staucht_sonst_rot(monkeypatch):
    lang = "Dübel Grün 6x30 mit sehr langem Namen für Überlauf"
    tpl = lbx.parse(BOX)
    inhalt = next(o for o in tpl.objects if o.field == "Lagerinhalt")
    assert inhalt.shrink

    root = ET.fromstring(preview_svg.render_svg(BOX, {**BOX_ROW, "Lagerinhalt": lang}))
    g = _group(root, inhalt.index)
    assert "gestaucht" in g.get("class")
    span = g.find(f"{SVG}text/{SVG}tspan")
    assert span.get("lengthAdjust") == "spacingAndGlyphs"
    assert float(span.get("textLength")) == pytest.approx(inhalt.width, abs=0.01)
    assert g.find(SVG + "text").get("fill") == "#000"

    # ohne shrink: rot markieren
    original = lbx.parse

    def ohne_shrink(path):
        t = original(path)
        for o in t.objects:
            o.shrink = False
        return t

    monkeypatch.setattr(preview_svg.lbx, "parse", ohne_shrink)
    root = ET.fromstring(preview_svg.render_svg(BOX, {**BOX_ROW, "Lagerinhalt": lang}))
    g = _group(root, inhalt.index)
    assert "ueberlauf" in g.get("class")
    assert g.find(SVG + "text").get("fill") == preview_svg.OVERFLOW
    assert g.find(SVG + "rect").get("stroke") == preview_svg.OVERFLOW
    # passender Text bleibt unmarkiert
    kurz = _group(root, next(o.index for o in tpl.objects if o.field == "Lagerfarbe"))
    assert "ueberlauf" not in kurz.get("class")


def test_svg_mehrere_inline_eindeutige_ids():
    a = ET.fromstring(preview_svg.render_svg(BOX, BOX_ROW))
    b = ET.fromstring(preview_svg.render_svg(BOX, BOX_ROW))
    ids = lambda r: {e.get("id") for e in r.iter() if e.get("id")}
    assert ids(a) and not ids(a) & ids(b)


# --- b-PAC ohne COM -------------------------------------------------------------------------

def test_object_map_formen_fehlen_in_bpac():
    fields = bpac.merge_fields(BOX)
    # b-PAC listet Shape1 (draw:poly) nicht
    names = [n for n, _ in fields if n != "Shape1"]
    mapping = bpac.object_map(fields, names)
    assert (7, "Barcodeinhalt") in mapping         # Barcode1 ist in b-PAC Index 7, in der Vorlage 8
    assert (0, "Lagername") in mapping and (9, "Lagername") in mapping
    assert bpac.object_map(fields, [n for n, _ in fields]) is not None
    assert bpac.object_map(fields, names[:-1]) == mapping        # fehlt nur statischer Text: egal
    assert bpac.object_map(fields, names[:-2]) is None           # verknüpftes Text9 fehlt
    assert bpac.object_map(fields, names + ["Extra"]) is None    # b-PAC kennt mehr Objekte
    assert bpac.object_map(fields, ["Text1", "Text3"] + names[3:]) is None   # verknüpftes Objekt fehlt
    assert bpac.object_map(fields, list(reversed(names))) is None


def _bmp_1bit(w: int, h: int, schwarz: set[tuple[int, int]]) -> bytes:
    stride = (w + 31) // 32 * 4
    rows = []
    for y in range(h - 1, -1, -1):                # bottom-up
        line = bytearray(stride)
        for x in range(w):
            if (x, y) not in schwarz:             # Palette [schwarz, weiß] -> Bit 1 = weiß
                line[x >> 3] |= 0x80 >> (x & 7)
        rows.append(bytes(line))
    data = b"".join(rows)
    off = 14 + 40 + 8
    return (b"BM" + struct.pack("<IHHI", off + len(data), 0, 0, off)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 1, 0, len(data), 0, 0, 2, 0)
            + bytes([0, 0, 0, 0, 255, 255, 255, 0]) + data)


def _png_pixels(png: bytes) -> tuple[int, int, int, bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + data)
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    w, h, depth = ihdr[0], ihdr[1], ihdr[2]
    return w, h, depth, zlib.decompress(idat)


def test_bmp_to_png_1bit_und_24bit():
    w, h = 13, 5
    png = bpac.bmp_to_png(_bmp_1bit(w, h, {(0, 0), (12, 4)}))
    pw, ph, depth, raw = _png_pixels(png)
    assert (pw, ph, depth) == (w, h, 1)
    row_bytes = (w + 7) // 8 + 1
    first, last = raw[:row_bytes], raw[-row_bytes:]
    assert first[1] & 0x80 == 0                    # (0,0) schwarz, oben links
    assert first[1] & 0x40                         # Nachbar weiß
    assert last[2] & (0x80 >> 4) == 0              # (12,4) schwarz

    # 24 bit, top-down: ein rotes Pixel
    pix = bytes([0, 0, 255]) + bytes([255, 255, 255])
    data = pix + b"\0\0"                           # Zeile auf 4 Byte auffüllen
    bmp = (b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54)
           + struct.pack("<IiiHHIIiiII", 40, 2, -1, 1, 24, 0, len(data), 0, 0, 0, 0) + data)
    pw, ph, depth, raw = _png_pixels(bpac.bmp_to_png(bmp))
    assert (pw, ph, depth) == (2, 1, 8)
    assert raw == b"\0" + bytes([255, 0, 0, 255, 255, 255])

    with pytest.raises(bpac.BpacError):
        bpac.bmp_to_png(b"\x89PNG kein bmp")


class FakeItem:
    def __init__(self, name):
        self.Name, self.Text = name, "alt"


class FakeObjects:
    def __init__(self, names):
        self.items = [FakeItem(n) for n in names]

    @property
    def Count(self):
        return len(self.items)

    def Item(self, i):
        return self.items[i]


class FakePrinter:
    def GetInstalledPrinters(self):
        return ("Brother PT-P710BT",)


class FakeDoc:
    """Attrappe ohne _oleobj_ – bpac._call ruft dann die Python-Methode auf."""

    def __init__(self, names):
        self.Objects = FakeObjects(names)
        self.Printer = FakePrinter()
        self.ErrorCode = 0
        self.calls = []

    def Open(self, path):
        self.calls.append("Open")
        return True

    def Close(self):
        self.calls.append("Close")
        return True

    def Save(self, *a):                            # darf nie aufgerufen werden
        raise AssertionError("Vorlage darf nicht gespeichert werden")

    SaveAs = Save

    def GetPrinterName(self):
        return "Brother PT-P710BT"

    def SetPrinter(self, name, fit):
        self.calls.append("SetPrinter")
        return True                                # b-PAC sagt auch bei Unsinn True

    def StartPrint(self, name, flags):
        self.calls.append(("StartPrint", flags))
        return True

    def PrintOut(self, copies, flags):
        self.calls.append(("PrintOut", tuple(i.Text for i in self.Objects.items), flags))
        return True

    def EndPrint(self):
        self.calls.append("EndPrint")
        return True

    def Export(self, kind, path, dpi):
        self.calls.append(("Export", kind, dpi))
        Path(path).write_bytes(_bmp_1bit(16, 8, {(1, 1)}))
        return True


@pytest.fixture
def fake_com(monkeypatch):
    """bpac mit Attrappe statt b-PAC; liefert eine Liste der erzeugten Dokumente."""
    docs = _Docs()
    docs.names = [[n for n, _ in bpac.merge_fields(BOX) if n != "Shape1"]]   # wie echtes b-PAC

    class Client:
        @staticmethod
        def Dispatch(progid):
            assert progid == "bpac.Document"
            docs.append(FakeDoc(docs.names[0]))
            return docs[-1]

    fake_pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None,
                                           DISPATCH_METHOD=1)
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setattr(bpac, "_com", lambda: Client)
    return docs


class _Docs(list):
    names: list


def test_render_png_mit_attrappe_setzt_felder_und_schliesst(fake_com):
    png = bpac.render_png(BOX, BOX_ROW)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    doc = fake_com[-1]
    texte = {i.Name: i.Text for i in doc.Objects.items if i.Name != "Text2"}
    assert texte["Barcode1"] == "S0204"
    assert texte["Barcode2"] == "MIC-2.0021"
    assert texte["Text4"] == "Dübel 6x30"
    assert texte["Text3"] == "alt"                 # statischer Text bleibt
    assert ("Export", bpac.EXPORT_BMP, bpac.PREVIEW_DPI) in doc.calls
    assert doc.calls[-1] == "Close"


def test_render_png_falsche_reihenfolge_schliesst(fake_com):
    fake_com.names[0] = ["Text1", "Barcode1"]
    with pytest.raises(bpac.BpacError, match="Reihenfolge"):
        bpac.render_png(BOX, BOX_ROW)
    assert fake_com[-1].calls == ["Open", "Close"]


def test_print_labels_unbekannter_drucker_druckt_nicht(fake_com):
    with pytest.raises(bpac.BpacError, match="Gibt-es-nicht"):
        bpac.print_labels(BOX, [BOX_ROW], "Gibt-es-nicht", 0)
    calls = fake_com[-1].calls
    assert not any(c[0] == "StartPrint" for c in calls if isinstance(c, tuple)) and calls[-1] == "Close"


def test_print_labels_ablauf(fake_com):
    """Ein Auftrag wie im b-PAC-Beispiel: StartPrint, je Label Open -> PrintOut, dann EndPrint."""
    rows = [BOX_ROW, {**BOX_ROW, "Lagername": "MIC-2.0022"}]
    assert bpac.print_labels(BOX, rows, "Brother PT-P710BT", 0x1 | 0x4000000) == 2
    calls = fake_com[-1].calls
    names = [c if isinstance(c, str) else c[0] for c in calls]
    assert names == ["Open", "Close", "StartPrint", "Open", "SetPrinter", "PrintOut",
                     "Open", "SetPrinter", "PrintOut", "EndPrint", "Close"]
    assert calls[2] == ("StartPrint", 0x1)            # PT-P710BT: "Schnitt am Ende" entfernt
    printouts = [c for c in calls if isinstance(c, tuple) and c[0] == "PrintOut"]
    assert [p[2] for p in printouts] == [0, 0]          # PrintOut immer mit 0
    assert printouts[1][1][0] == "MIC-2.0022"


def test_print_labels_drucker_aus_vorlage(fake_com):
    """Ohne Druckerangabe zählt das Modell der Vorlage (GetPrinterName) für die Schnittoptionen."""
    assert bpac.print_labels(BOX, [BOX_ROW], "", 0x1 | 0x4000000) == 1
    calls = fake_com[-1].calls
    assert ("StartPrint", 0x1) in calls and "SetPrinter" not in calls


def test_call_nutzt_dispatch_method():
    """Argumentlose Methoden müssen als Methode (nicht als Property) aufgerufen werden."""
    seen = []

    class Ole:
        def GetIDsOfNames(self, name):
            return {"Close": 7}[name]

        def Invoke(self, dispid, lcid, flags, result, *args):
            seen.append((dispid, flags, args))
            return True

    pythoncom = pytest.importorskip("pythoncom")
    assert bpac._call(types.SimpleNamespace(_oleobj_=Ole()), "Close") is True
    assert seen == [(7, pythoncom.DISPATCH_METHOD, ())]


# --- b-PAC live (nur wenn installiert) ------------------------------------------------------

def test_render_png_live():
    if not bpac.preview_available():
        pytest.skip("b-PAC nicht verfügbar")
    png = bpac.render_png(BOX, BOX_ROW)
    w, h, depth, raw = _png_pixels(png)
    tpl = lbx.parse(BOX)
    assert w > h                                   # quer
    assert w == pytest.approx(tpl.length_pt / 72 * bpac.PREVIEW_DPI, abs=3)
    assert h == pytest.approx(tpl.tape_pt / 72 * bpac.PREVIEW_DPI, abs=3)
    assert 500 < len(png) < 200_000
    # es ist tatsächlich etwas schwarz und überwiegend weiß
    bits = bin(int.from_bytes(raw, "big")).count("1")
    assert 0.5 * w * h < bits < w * h


def test_print_batch_ein_auftrag_fuer_mehrere_vorlagen(fake_com, tmp_path):
    """Mehrere Vorlagen: ein StartPrint/EndPrint, dazwischen je Label Open -> PrintOut."""
    zweite = tmp_path / "Zweite.lbx"
    zweite.write_bytes(BOX.read_bytes())
    rows2 = [{**BOX_ROW, "Lagername": "B-1"}, {**BOX_ROW, "Lagername": "B-2"}]
    assert bpac.print_batch([(BOX, [BOX_ROW]), (zweite, rows2), (BOX, [])], "Brother PT-P710BT", 0x1) == 3
    calls = fake_com[-1].calls
    names = [c if isinstance(c, str) else c[0] for c in calls]
    assert names.count("StartPrint") == 1 and names.count("EndPrint") == 1 and names.count("PrintOut") == 3
    assert names.index("StartPrint") < names.index("PrintOut") and names[-2:] == ["EndPrint", "Close"]
    gedruckt = [c[1][0] for c in calls if isinstance(c, tuple) and c[0] == "PrintOut"]
    assert gedruckt == ["MIC-2.0021", "B-1", "B-2"]
