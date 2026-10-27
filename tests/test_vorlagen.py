"""Vorlagen-Reiter: Prüfung, Beispieldaten, Vorschau-Weiche, Bearbeiten im P-touch Editor, Kopie."""
import re
import sys
import types
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from labeltool.app import create_app
from labeltool.bpac import BpacError
from test_smoke import FakePartDB, make, read_csv

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakePreviewPrinter:
    """b-PAC-Vorschau: verfügbar oder nicht, optional mit Fehler beim Rendern."""
    BpacError = BpacError

    def __init__(self, available=True, fail=False):
        self.available, self.fail, self.rendered = available, fail, []

    def status(self, printer):
        return {"ok": True, "grund": "", "drucker": []}

    def preview_available(self):
        return self.available

    def render_png(self, template, row):
        if self.fail:
            raise BpacError("Kein Drucker")
        self.rendered.append((template.name, row))
        return PNG


def client(tmp_path, printer=None):
    c, fake, cfg = make(tmp_path)
    opened = []
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=opened.append,
                              printer=printer or FakePreviewPrinter()))
    c.opened = opened
    return c, fake, cfg


def xml_of(path):
    return zipfile.ZipFile(path).read("label.xml").decode("utf-8")


def without_db(xml):
    for attr in ("databasePath", "mergeTable"):
        xml = re.sub(attr + r'="[^"]*"', attr + '=""', xml)
    return re.sub(r'(<database:dbTable name=)"[^"]*"', r'\1""', xml)


def test_uebersicht(tmp_path):
    c, _, cfg = client(tmp_path)
    data = c.get("/api/vorlagen").json()
    assert data["ordner"] == str(cfg.template_dir) and data["bpac_vorschau"] is True
    v = {t["datei"]: t for t in data["vorlagen"]}
    assert len(v) == 7
    box = v["Layout_Lagerbox_PartDB.lbx"]
    assert box["box_label"] and box["band_mm"] == 24 and box["lagerarten"] == [] and box["warnungen"] == []
    assert v["PartDB_3U-3.lbx"]["lagerarten"] == ["3U2x1_3"] and v["PartDB_3U-3.lbx"]["band_mm"] == 12
    assert "Section3" in v["PartDB_3U-3.lbx"]["felder"] and v["PartDB_3U-3.lbx"]["warnungen"] == []
    assert v["PartDB_Window-Div-Bin-5.lbx"]["warnungen"] == []
    assert "WinBin4" in data["ohne_vorlage"] and data["fehlende_dateien"] == []
    assert c.get("/api/meta").json()["bpac_vorschau"] is True

    # Lagerart mit weniger Abteilen als die Vorlage Felder hat – und umgekehrt
    cfg.box_types["WinBin5"].compartments = 4
    cfg.box_types["WinBin2"].compartments = 3
    cfg.box_types["WinBin4"].lbx = "fehlt.lbx"
    data = c.get("/api/vorlagen").json()
    v = {t["datei"]: t for t in data["vorlagen"]}
    assert any("Section5" in w and "'WinBin5'" in w for w in v["PartDB_Window-Div-Bin-5.lbx"]["warnungen"])
    assert any("'WinBin2' hat 3 Abteile" in w for w in v["PartDB_Window-Div-Bin-2.lbx"]["warnungen"])
    assert data["fehlende_dateien"] == [{"lagerart": "WinBin4", "datei": "fehlt.lbx"}]

    # Nicht genutzt / Box-Label mit fremden Spalten
    cfg.box_types["WinBin1"].lbx = ""
    cfg.box_template = "PartDB_SMD15.lbx"
    v = {t["datei"]: t for t in c.get("/api/vorlagen").json()["vorlagen"]}
    assert any("keiner Lagerart" in w for w in v["PartDB_Window-Div-Bin-1.lbx"]["warnungen"])
    assert any("nicht liefert" in w and "Section1" in w for w in v["PartDB_SMD15.lbx"]["warnungen"])


def test_detail_und_traversal(tmp_path):
    c, _, cfg = client(tmp_path)
    d = c.get("/api/vorlagen/PartDB_SMD15.lbx").json()
    assert d["tape_mm"] == 12 and d["fields"] == ["Category", "Footprint1", "Location", "Section1"]
    assert d["lagerarten"] == ["SMD15"] and d["box_label"] is False and isinstance(d["objects"], list)
    (tmp_path / "geheim.lbx").write_bytes(b"x")
    assert c.get("/api/vorlagen/gibtsnicht.lbx").status_code == 404
    for bad in ("..%5Cgeheim.lbx", "config.yaml", ".backup.lbx", "a%3Ab.lbx"):
        r = c.get(f"/api/vorlagen/{bad}")
        assert r.status_code == 400 and "Ungültiger Vorlagenname" in r.json()["detail"], bad
    assert c.get("/api/vorlagen/..%2Fgeheim.lbx").status_code == 404   # Schrägstrich: keine Route
    assert c.post("/api/vorlagen/..%5Cgeheim.lbx/bearbeiten", json={}).status_code == 400
    assert c.get("/api/vorlagen/PartDB_SMD15.lbx/beispiel", params={"quelle": "x"}).status_code == 400


def test_beispiele(tmp_path):
    c, _, cfg = client(tmp_path)
    url = "/api/vorlagen/PartDB_Window-Div-Bin-5.lbx/beispiel"

    r = c.get(url, params={"quelle": "vorlage"}).json()
    row = r["zeilen"][0]
    assert r["quelle"] == "vorlage" and row["Location"] == "EL-5.0060" and row["Section2"] == "TPS63060DSCR"
    assert row["Section3"] == "" and "Section6" in r["spalten"]

    r = c.get(url).json()     # Standard: worst
    row = r["zeilen"][0]
    assert r["quelle"] == "worst" and len(row["Section1"]) == 18   # längster Name zuerst
    assert all(row[f"Section{i}"] and row[f"Footprint{i}"] for i in range(1, 6))
    assert len({row[f"Section{i}"] for i in range(1, 6)}) == 5 and "ESP32-C3" not in row.values()
    assert re.fullmatch(r".+-12\.0123", row["Location"]) and row["Barcode"] == "S1234"
    assert row["Category"] == "Spannungsregler - Schalten"

    box = c.get("/api/vorlagen/Layout_Lagerbox_PartDB.lbx/beispiel").json()
    assert box["spalten"][:3] == ["ID", "Lagername", "Barcodeinhalt"]
    assert box["zeilen"][0]["Lagerabteile"] == "12" and box["zeilen"][0]["Barcodeinhalt"] == "S1234"

    # echte Box: Bauteil-Label mit ALLEN Teilen (ESP32-C3 hat keinen Tag)
    r = c.get("/api/vorlagen/PartDB_Window-Div-Bin-2.lbx/beispiel", params={"quelle": "box", "box_id": 203}).json()
    assert r["zeilen"][0]["Section1"] == "ESP32-C3" and r["zeilen"][0]["Location"] == "MIC-2.0020"
    r = c.get("/api/vorlagen/Layout_Lagerbox_PartDB.lbx/beispiel", params={"quelle": "box", "box_id": 250}).json()
    assert r["zeilen"][0]["Lagername"] == "SMDICs-1.0013" and r["zeilen"][0]["Barcodeinhalt"] == "S0250"
    assert c.get(url, params={"quelle": "box"}).status_code == 400
    assert c.get(url, params={"quelle": "box", "box_id": 999}).status_code == 404


def test_worst_ohne_partdb(tmp_path):
    c, fake, cfg = client(tmp_path)
    cfg.partdb_token = ""   # Part-DB nicht nutzbar -> feste Werte, kein Fehler
    r = c.get("/api/vorlagen/PartDB_SMD15.lbx/beispiel", params={"quelle": "worst"})
    assert r.status_code == 200 and r.json()["zeilen"][0]["Section1"]


def test_vorschau_weiche(tmp_path):
    prn = FakePreviewPrinter()
    c, _, cfg = client(tmp_path, prn)
    r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317", "Location": 7}})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["X-Vorschau"] == "bpac" and r.content == PNG
    assert prn.rendered[-1] == ("PartDB_SMD15.lbx", {"Section1": "LM317", "Location": "7"})

    r = c.get("/api/vorlagen/PartDB_SMD15.lbx/vorschau", params={"quelle": "vorlage"})
    assert r.headers["X-Vorschau"] == "bpac" and prn.rendered[-1][1]["Section1"] == "MT3608"

    r = c.get("/api/name-labels/203/vorschau")
    assert r.status_code == 200 and prn.rendered[-1][0] == "Layout_Lagerbox_PartDB.lbx"
    assert prn.rendered[-1][1]["Lagername"] == "MIC-2.0020"

    r = c.get("/api/content-labels/250/vorschau")
    assert r.status_code == 200 and prn.rendered[-1][0] == "PartDB_3U-3.lbx"
    assert prn.rendered[-1][1]["Section3"] == "MT3608" and prn.rendered[-1][1]["Section1"] == "AMS1117-3.3"
    r = c.get("/api/content-labels/203/vorschau")       # WinBin2 -> eigene Vorlage
    assert r.status_code == 200 and prn.rendered[-1][0] == "PartDB_Window-Div-Bin-2.lbx"
    cfg.box_types["WinBin2"].lbx = ""
    r = c.get("/api/content-labels/203/vorschau")
    assert r.status_code == 400 and "WinBin2" in r.json()["detail"]
    assert c.get("/api/content-labels/999/vorschau").status_code == 404


@pytest.mark.parametrize("prn", [FakePreviewPrinter(available=False), FakePreviewPrinter(fail=True)])
def test_vorschau_weiche_skizze(tmp_path, prn, monkeypatch):
    """Weiche unabhängig vom echten Skizzen-Modul: Stub statt preview_svg."""
    stub = types.ModuleType("labeltool.preview_svg")
    stub.render_svg = lambda template, row: f'<svg data-t="{template.name}">{row["Section1"]}</svg>'
    monkeypatch.setitem(sys.modules, "labeltool.preview_svg", stub)
    c, _, cfg = client(tmp_path, prn)
    r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317"}})
    assert r.status_code == 200 and r.headers["X-Vorschau"] == "skizze"
    assert r.headers["content-type"].startswith("image/svg+xml") and r.text == '<svg data-t="PartDB_SMD15.lbx">LM317</svg>'
    assert c.get("/api/vorlagen").json()["bpac_vorschau"] is prn.available


@pytest.mark.parametrize("prn", [FakePreviewPrinter(available=False), FakePreviewPrinter(fail=True)])
def test_vorschau_skizze(tmp_path, prn):
    pytest.importorskip("labeltool.preview_svg")
    c, _, cfg = client(tmp_path, prn)
    r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317"}})
    assert r.status_code == 200 and r.headers["X-Vorschau"] == "skizze"
    assert r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text
    assert c.get("/api/meta").json()["bpac_vorschau"] is prn.available


def test_bearbeiten(tmp_path):
    c, _, cfg = client(tmp_path)
    tpl = cfg.template_dir / "PartDB_3U-3.lbx"
    before = zipfile.ZipFile(tpl)
    original = {i.filename: before.read(i.filename) for i in before.infolist()}
    before.close()

    r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "box", "box_id": 250})
    assert r.status_code == 200, r.text
    d = r.json()
    csv_path = cfg.output_dir / "Beispiele" / "PartDB_3U-3.csv"
    assert d["vorlage"] == str(tpl) and d["csv"] == str(csv_path) and c.opened == [tpl]
    assert re.search(r"[\\/]\.backup[\\/]PartDB_3U-3\.\d{8}-\d{6}\.lbx$", d["backup"])
    assert zipfile.ZipFile(d["backup"]).read("label.xml") == original["label.xml"]   # Sicherung = Original
    rows = read_csv(csv_path)
    assert rows[0]["Section3"] == "MT3608" and rows[0]["Location"] == "SMDICs-1.0013"

    # Original ist jetzt mit der Beispiel-CSV verknüpft – sonst bytegleich
    after = zipfile.ZipFile(tpl)
    xml = after.read("label.xml").decode("utf-8")
    assert f'databasePath="{csv_path.resolve()}"' in xml and 'mergeTable="PartDB_3U-3.csv"' in xml
    assert without_db(xml) == without_db(original["label.xml"].decode("utf-8"))
    assert {i.filename for i in after.infolist()} == set(original)
    assert all(after.read(n) == data for n, data in original.items() if n != "label.xml")
    after.close()
    assert not list(cfg.template_dir.glob("*.tmp")) and not list(cfg.template_dir.glob(".*.tmp"))

    # Zweites Mal: kein neues Backup
    r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "worst"}).json()
    assert r["backup"] is None and len(list((cfg.template_dir / ".backup").iterdir())) == 1
    assert "-12.0123" in read_csv(csv_path)[0]["Location"]

    # Vorlage ohne Datenbank-Verknüpfung: verständlicher Fehler, nichts geschrieben
    plain = cfg.template_dir / "ohne_db.lbx"
    with zipfile.ZipFile(tpl) as src, zipfile.ZipFile(plain, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "label.xml":
                data = re.sub(rb'databasePath="[^"]*"', b"", data)
            dst.writestr(item, data)
    r = c.post("/api/vorlagen/ohne_db.lbx/bearbeiten", json={"quelle": "vorlage"})
    assert r.status_code == 400 and "Datenbank-Verknüpfung" in r.json()["detail"]
    assert not (cfg.output_dir / "Beispiele" / "ohne_db.csv").exists()
    ov = {t["datei"]: t for t in c.get("/api/vorlagen").json()["vorlagen"]}
    assert any("Keine Datenbank-Verknüpfung" in w for w in ov["ohne_db.lbx"]["warnungen"])


def test_bearbeiten_gesperrt(tmp_path, monkeypatch):
    c, _, cfg = client(tmp_path)
    from pathlib import Path

    def locked(self, target):
        raise PermissionError("gesperrt")
    monkeypatch.setattr(Path, "replace", locked)
    r = c.post("/api/vorlagen/PartDB_SMD15.lbx/bearbeiten", json={"quelle": "vorlage"})
    assert r.status_code == 400 and "P-touch Editor geöffnet" in r.json()["detail"]
    assert c.opened == [] and not list(cfg.template_dir.glob(".*.tmp"))


def test_kopie(tmp_path):
    c, _, cfg = client(tmp_path)
    r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_Window-Div-Bin-3.lbx",
                                            "name": "PartDB_Window-Div-Bin-4.lbx", "lagerart": "WinBin4"})
    assert r.status_code == 200, r.text
    d = r.json()
    new = cfg.template_dir / "PartDB_Window-Div-Bin-4.lbx"
    assert d["datei"] == new.name and d["lagerart"] == "WinBin4" and d["vorlage"] == str(new)
    assert c.opened == [new] and cfg.box_types["WinBin4"].lbx == new.name
    rows = read_csv(d["csv"])
    assert rows[0]["Section4"] and "Section4" in rows[0]     # Worst Case für 4 Abteile
    # Vorlage hat nur 3 Section-Felder, Lagerart 4 Abteile -> Warnung
    ov = {t["datei"]: t for t in c.get("/api/vorlagen").json()["vorlagen"]}
    assert ov[new.name]["lagerarten"] == ["WinBin4"]
    assert any("4 Abteile" in w for w in ov[new.name]["warnungen"])
    # Original der Kopierquelle unverändert
    assert "Downloads" in xml_of(cfg.template_dir / "PartDB_Window-Div-Bin-3.lbx")

    # Rückgängig nimmt die Zuweisung zurück, die Datei bleibt
    assert c.post("/api/stammdaten/undo").json()["rueckgaengig_gemacht"] == "Lagerart 'WinBin4' geändert"
    assert cfg.box_types["WinBin4"].lbx == "" and new.exists()

    # Ohne Lagerart, Fehlerfälle
    r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "Test.lbx"}).json()
    assert r["lagerart"] is None and (cfg.template_dir / "Test.lbx").exists()
    assert c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "Test.lbx"}).status_code == 409
    for bad in ("x.txt", "../x.lbx", "sub\\x.lbx", ".lbx", ""):
        assert c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": bad}).status_code == 400, bad
    assert c.post("/api/vorlagen/kopie", json={"quelle": "fehlt.lbx", "name": "y.lbx"}).status_code == 404
    r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "z.lbx", "lagerart": "Gibts"})
    assert r.status_code == 400 and not (cfg.template_dir / "z.lbx").exists()
