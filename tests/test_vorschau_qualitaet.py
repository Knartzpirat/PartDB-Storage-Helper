"""Trennstrich-Symbole für b-PAC und einstellbare Vorschauqualität."""
import zipfile
import xml.etree.ElementTree as ET

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from labeltool import bpac, routes_einstellungen
from labeltool.helfer_server import ModePrinter
from test_smoke import VORLAGEN, make


def xml_of(name):
    return zipfile.ZipFile(VORLAGEN / name).read("label.xml").decode("utf-8")


def test_trennstriche_werden_linien():
    # WinBin-n hat n-1 Trennstriche aus "PTC Dingbats3", das Box-Label keine
    erwartet = {"PartDB_Window-Div-Bin-1.lbx": 0, "PartDB_Window-Div-Bin-2.lbx": 1,
                "PartDB_Window-Div-Bin-3.lbx": 2, "PartDB_Window-Div-Bin-5.lbx": 4,
                "PartDB_3U-3.lbx": 2, "Layout_Lagerbox_PartDB.lbx": 0}
    for name, n in erwartet.items():
        xml, count = bpac.fix_symbols(xml_of(name))
        assert count == n, name
        ET.fromstring(xml)                                   # bleibt gültiges XML
        assert "FONT,PTC Dingbats3" not in xml or name == "PartDB_SMD15.lbx"
    xml, _ = bpac.fix_symbols(xml_of("PartDB_Window-Div-Bin-2.lbx"))
    assert 'objectName="Symbol1"' in xml and 'shape="LINE"' in xml


def test_quadratische_symbole_bleiben():
    sym = ('<image:clipart><pt:objectStyle x="10pt" y="2pt" width="12pt" height="12pt">'
           '<pt:expanded objectName="Pfeil"></pt:expanded></pt:objectStyle>'
           '<image:clipartStyle originalName="FONT,PTC Dingbats1,123,12"></image:clipartStyle></image:clipart>')
    assert bpac.fix_symbols(sym) == (sym, 0)


def test_renderbare_kopie_laesst_original_unveraendert(tmp_path, monkeypatch):
    from labeltool import ptouch_fonts
    monkeypatch.setattr(ptouch_fonts, "load", lambda: [])       # kein P-touch Editor 6 -> Linien-Ersatz
    src = tmp_path / "v.lbx"
    src.write_bytes((VORLAGEN / "PartDB_Window-Div-Bin-3.lbx").read_bytes())
    before = src.read_bytes()
    with bpac._renderable(src) as copy:
        assert copy != src and "shape=\"LINE\"" in zipfile.ZipFile(copy).read("label.xml").decode("utf-8")
        assert [n for n, _ in bpac.merge_fields(copy)] == [n for n, _ in bpac.merge_fields(src)]   # Reihenfolge gleich
    assert src.read_bytes() == before and not copy.exists()
    plain = tmp_path / "box.lbx"
    plain.write_bytes((VORLAGEN / "Layout_Lagerbox_PartDB.lbx").read_bytes())
    with bpac._renderable(plain) as same:
        assert same == plain                                  # nichts zu ersetzen -> keine Kopie


def test_vorschau_dpi_einstellung(tmp_path):
    _, _, cfg = make(tmp_path)
    app = FastAPI()
    routes_einstellungen.register(app, cfg, None, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    c = TestClient(app)
    d = c.get("/api/einstellungen").json()["druck"]
    assert d["vorschau_dpi"] == 360 and d["vorschau_dpi_optionen"] == [180, 360, 600, 720]
    assert c.put("/api/einstellungen/druck", json={"modus": "lokal", "vorschau_dpi": 500}).status_code == 400
    r = c.put("/api/einstellungen/druck", json={"modus": "lokal", "vorschau_dpi": 720})
    assert r.status_code == 200 and cfg.preview_dpi == 720
    # ohne Angabe bleibt der Wert erhalten
    assert c.put("/api/einstellungen/druck", json={"modus": "lokal"}).json()["druck"]["vorschau_dpi"] == 720


def test_dpi_wird_nur_abweichend_weitergereicht(tmp_path):
    _, _, cfg = make(tmp_path)
    calls = []

    class P:
        def render_png(self, template, row, **kw):
            calls.append(kw)
            return b"png"

    mp = ModePrinter(cfg, P(), P())
    mp.render_png("x.lbx", {})
    cfg.preview_dpi = 600
    mp.render_png("x.lbx", {})
    assert calls == [{}, {"dpi": 600}]


def test_druckqualitaet_flags():
    assert bpac.print_flags(["autocut", "cutatend"]) == 0x1 | 0x4000000
    assert bpac.print_flags(["autocut"], "hoch") == 0x1 | 0x2000000              # bpoHighResolution
    assert bpac.print_flags([], "hoch_langsam") == 0x2000000 | 0x10000           # + bpoQuality
    import pytest
    with pytest.raises(bpac.BpacError):
        bpac.print_flags([], "ultra")


def test_druckqualitaet_einstellung_und_direktdruck(tmp_path):
    from test_smoke import FakePrinter
    from labeltool.app import create_app
    _, fake, cfg = make(tmp_path)
    prn = FakePrinter()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn))
    assert c.put("/api/einstellungen/druck", json={"modus": "lokal", "qualitaet": "super"}).status_code == 400
    r = c.put("/api/einstellungen/druck", json={"modus": "lokal", "direktdruck": True,
                                               "schnitt": ["autocut"], "qualitaet": "hoch"})
    assert r.status_code == 200 and r.json()["druck"]["qualitaet"] == "hoch"
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 1})
    assert c.post("/api/name-labels/print").status_code == 200
    assert prn.jobs[-1][2] == 0x1 | 0x2000000                                      # Flags beim Drucker


def test_geladene_schrift_wird_nicht_ersetzt(tmp_path, monkeypatch):
    from labeltool import ptouch_fonts
    monkeypatch.setattr(ptouch_fonts, "load", lambda: ["PTC Dingbats3"])
    xml = xml_of("PartDB_Window-Div-Bin-3.lbx")
    assert bpac.fix_symbols(xml, ["PTC Dingbats3"]) == (xml, 0)
    src = tmp_path / "v.lbx"
    src.write_bytes((VORLAGEN / "PartDB_Window-Div-Bin-3.lbx").read_bytes())
    with bpac._renderable(src) as same:
        assert same == src                                        # Original geht unverändert an b-PAC


def _mini_font(family: str) -> bytes:
    """Kleinste sfnt-Datei mit name- und cmap-Tabelle (reicht zum Erkennen, nicht zum Rendern)."""
    import struct
    name_str = family.encode("utf-16-be")
    name = struct.pack(">HHH", 0, 1, 6 + 12) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name_str), 0) + name_str
    cmap = struct.pack(">HH", 0, 0)
    tables = [(b"cmap", cmap), (b"head", bytes(54)), (b"maxp", bytes(6)), (b"name", name)]
    off = 12 + 16 * len(tables)
    head, body = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0), b""
    for tag, data in tables:
        head += struct.pack(">4sIII", tag, 0, off + len(body), len(data))
        body += data
    return head + body


def test_schriften_aus_bytes_extrahieren():
    from labeltool import ptouch_fonts
    blob = b"MZ" + b"\0" * 100 + _mini_font("PTC Dingbats3") + b"\0" * 50 + _mini_font("Arial") + b"Rest"
    fonts = ptouch_fonts.extract_fonts(blob)
    assert list(fonts) == ["PTC Dingbats3"]                      # nur P-touch-Schriften
    assert ptouch_fonts._family(fonts["PTC Dingbats3"]) == "PTC Dingbats3"


def test_schriften_aus_echtem_ptouch_editor():
    import pytest
    from labeltool import ptouch_fonts
    files = ptouch_fonts.font_files()
    if not files:
        pytest.skip("P-touch Editor 6 nicht installiert")
    fonts = ptouch_fonts.find_fonts()
    assert "PTC Dingbats3" in fonts and len(files) == len(set(files))


def test_schnittoptionen_je_modell():
    ac, chain, end, half = 0x1, 0x400, 0x4000000, 0x200
    # PT-P710BT kennt kein "Schnitt am Ende" und keinen Halbschnitt -> werden entfernt, Rest bleibt
    assert bpac.supported_cuts("Brother PT-P710BT") == {"autocut", "chainprint"}
    assert bpac.flags_for_printer(ac | end | 0x2000000, "Brother PT-P710BT") == ac | 0x2000000
    assert bpac.flags_for_printer(ac | chain | half, "Brother PT-P710BT") == ac | chain
    # unbekannte Modelle bleiben unangetastet; ähnliche Namen nicht verwechseln
    assert bpac.flags_for_printer(ac | end, "Brother PT-P750W") == ac | end
    assert bpac.supported_cuts("Brother PT-P7000") is None and bpac.supported_cuts("") is None


def test_falsches_band_bricht_ab():
    # eingelegt 12 mm (259), Vorlage braucht 24 mm (261) -> Abbruch mit Meldung
    import pytest
    with pytest.raises(bpac.BpacError, match="Falsches Band.*12 mm.*24 mm"):
        bpac.check_media((259, "12 mm"), {"Layout_Lagerbox_PartDB.lbx": (261, "24 mm")})
    bpac.check_media((259, "12 mm"), {"a.lbx": (259, "12 mm"), "b.lbx": (259, "12 mm")})   # passt
    bpac.check_media((0, ""), {"a.lbx": (261, "24 mm")})                                    # unbekannt: kein Abbruch

