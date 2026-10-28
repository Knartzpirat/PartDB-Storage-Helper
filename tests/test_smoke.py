"""End-to-end-Test gegen ein simuliertes Part-DB (httpx.MockTransport)."""
import csv
import json
import re
import shutil
import zipfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from labeltool.app import create_app
from labeltool.config import load_config

ROOT = Path(__file__).resolve().parents[1]
VORLAGEN = Path(__file__).resolve().parent / "vorlagen"   # Originalvorlagen für die Tests


class FakePartDB:
    def __init__(self):
        self.locations = {
            203: {"id": 203, "name": "MIC-2.0020",
                  "comment": "Lagerinhalt: Microcontroller Lagerfarbe: Dunkelgrau Lagerabteile: 2",
                  "parameters": [{"name": "WinBin2", "id": 204}]},
            250: {"id": 250, "name": "SMDICs-1.0013",
                  "comment": "<p>Lagerinhalt: SMD ICs Lagerfarbe: Rot Lagerabteile: 1</p>",
                  "parameters": [{"name": "3U2x1_3"}]},
        }
        self.parts = {
            304: {"id": 304, "name": "MT3608", "tags": "noLabel", "footprint": "/api/footprints/22",
                  "category": "/api/categories/65",
                  "partLots": [{"id": 1, "description": "Fach 3", "storage_location": "/api/storage_locations/250"}]},
            305: {"id": 305, "name": "AMS1117-3.3", "tags": "", "footprint": "/api/footprints/23",
                  "category": "/api/categories/66",
                  "partLots": [{"id": 2, "description": "", "storage_location": "/api/storage_locations/250"}]},
            306: {"id": 306, "name": "ESP32-C3", "tags": "", "footprint": None, "category": None,
                  "partLots": [{"id": 3, "description": "F1", "storage_location": {"id": 203}}]},
        }
        self.footprints = [{"id": 22, "name": "SOT-23"}, {"id": 23, "name": "SOT-223"}]
        self.categories = [{"id": 65, "name": "Spannungsregler - Schalten"},
                           {"id": 66, "name": "Spannungsregler - Linear"}]
        self.next_id = 400
        self.patches = []

    def page(self, items, req):
        p = int(req.url.params.get("page", 1)); n = int(req.url.params.get("itemsPerPage", 30))
        return items[(p - 1) * n: p * n]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer test"
        path, m = req.url.path, req.method
        if m == "GET" and path == "/api/storage_locations":
            return httpx.Response(200, json=self.page(list(self.locations.values()), req))
        if m == "POST" and path == "/api/storage_locations":
            body = json.loads(req.content)
            loc = {"id": self.next_id, **body}; self.locations[self.next_id] = loc; self.next_id += 1
            return httpx.Response(201, json=loc)
        if m == "GET" and path == "/api/parts":
            return httpx.Response(200, json=self.page(list(self.parts.values()), req))
        if m == "GET" and path == "/api/footprints":
            return httpx.Response(200, json=self.page(self.footprints, req))
        if m == "GET" and path == "/api/categories":
            return httpx.Response(200, json=self.page(self.categories, req))
        if (mm := re.match(r"/api/parts/(\d+)$", path)):
            pid = int(mm.group(1))
            if m == "GET":
                return httpx.Response(200, json=self.parts[pid])
            if m == "PATCH":
                assert req.headers["Content-Type"] == "application/merge-patch+json"
                self.parts[pid].update(json.loads(req.content)); self.patches.append(pid)
                return httpx.Response(200, json=self.parts[pid])
        return httpx.Response(404, json={"detail": f"{m} {path}"})


def make(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    text = text.replace('token: ""', 'token: "test"')
    text = re.sub(r'(?m)^  ordner: ".*"', '  ordner: "out"', text)
    cfg_file.write_text(text, encoding="utf-8")
    # fester Testsatz der Originalvorlagen – der echte Ordner ändert sich im Betrieb
    shutil.copytree(VORLAGEN, tmp_path / "BeispielEtiketten")
    cfg = load_config(cfg_file)
    fake = FakePartDB()
    opened = []
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=opened.append))
    c.opened = opened
    return c, fake, cfg


def read_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_full_flow(tmp_path):
    c, fake, cfg = make(tmp_path)

    # Boxen lesen (Legacy-Format: Lagerart als Parameter, HTML im Kommentar)
    boxes = {b["name"]: b for b in c.get("/api/boxes").json()}
    assert boxes["MIC-2.0020"]["box_type"] == "WinBin2"
    assert boxes["MIC-2.0020"]["barcode"] == "S0203"
    assert boxes["SMDICs-1.0013"]["content"] == "SMD ICs"
    assert boxes["SMDICs-1.0013"]["box_type"] == "3U2x1_3"

    # Neue Box: Nummer läuft pro Kürzel weiter
    pv = c.get("/api/boxes/preview", params={"abbr": "MIC", "compartments": 2, "count": 2}).json()
    assert pv["names"] == ["MIC-2.0021", "MIC-2.0022"] and pv["color"] == "Dunkelgrau"
    r = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 2})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert [b["name"] for b in created] == ["MIC-2.0021", "MIC-2.0022"]
    assert fake.locations[400]["comment"] == \
        "Lagerinhalt: Microcontroller Lagerfarbe: Dunkelgrau Lagerabteile: 2 Lagerart: WinBin2"

    # Namenslabels
    q = c.get("/api/name-labels").json()
    assert [b["name"] for b in q["boxes"]] == ["MIC-2.0021", "MIC-2.0022"]
    ex = c.post("/api/name-labels/export", json={"open_template": True}).json()
    rows = read_csv(ex["datei"])
    # Box-Label-Vorlage (24 mm): Kopie verknüpft mit der CSV, Original unverändert
    assert ex["vorlage"].endswith("Layout_Lagerbox_PartDB.lbx") and c.opened == [Path(ex["vorlage"])]
    linked = zipfile.ZipFile(ex["vorlage"]).read("label.xml").decode("utf-8")
    assert f'databasePath="{Path(ex["datei"]).resolve()}"' in linked and 'mergeTable="Namenslabels.csv"' in linked
    assert "Downloads" in zipfile.ZipFile(tmp_path / "BeispielEtiketten/Layout_Lagerbox_PartDB.lbx").read("label.xml").decode("utf-8")
    assert rows[0]["Barcodeinhalt"] == "S0400" and rows[0]["Lagerart"] == "WinBin2"
    assert list(rows[0].keys())[:3] == ["ID", "Lagername", "Barcodeinhalt"]
    c.post("/api/name-labels/done")
    assert c.get("/api/name-labels").json()["boxes"] == []

    # Inhaltslabels: nur Box 250 (getaggtes Teil), aber mit allen Teilen
    prev = c.get("/api/content-labels").json()
    assert len(prev) == 1 and prev[0]["box"]["name"] == "SMDICs-1.0013"
    slots = prev[0]["slots"]
    assert slots[2]["name"] == "MT3608"        # Fach 3
    assert slots[0]["name"] == "AMS1117-3.3"  # ohne Fach -> erstes freies Feld
    ex = c.post("/api/content-labels/export").json()
    smd = [d for d in ex["dateien"] if d["datei"].endswith("Inhalt_3U2x1_3.csv")][0]
    assert smd["lbx"] == ["PartDB_3U-3.lbx"]
    row = read_csv(smd["datei"])[0]
    assert "Section6" in row and "Footprint6" in row   # Vorlagen erwarten immer 6 Spalten
    assert row["Location"] == "SMDICs-1.0013" and row["Section3"] == "MT3608" and row["Footprint3"] == "SOT-23"
    assert row["Footprint1"] == "SOT-223" and row["Barcode"] == "S0250"
    assert c.post("/api/templates/open", json={"box_type": "3U2x1_3"}).status_code == 200
    assert c.post("/api/templates/open", json={"box_type": "Rack"}).status_code == 400   # keine Vorlage
    # leere Vorlagen werden geleert
    assert read_csv(cfg.output_dir / "Inhalt_WinBin2.csv") == []

    # Tags entfernen
    d = c.post("/api/content-labels/done").json()
    assert d["entfernt"] == 1 and fake.patches == [304] and fake.parts[304]["tags"] == ""
    assert c.get("/api/content-labels").json() == []

    # UI wird ausgeliefert
    assert "Lagerbox Labels" in c.get("/").text


def test_errors(tmp_path):
    c, fake, cfg = make(tmp_path)
    assert c.post("/api/boxes", json={"abbr": "XX", "compartments": 1, "box_type": "SMD15"}).status_code == 400
    assert c.post("/api/content-labels/done").status_code == 400


def test_stammdaten(tmp_path):
    c, fake, cfg = make(tmp_path)
    info = c.get("/api/stammdaten").json()
    types = {t["name"]: t for t in info["lagerarten"]}
    assert types["WinBin3"]["compartments"] == 3 and types["SMD15"]["vorlage"] == {"file": "Inhalt_SMD15.csv", "slots": 1}
    assert types["SMD15"]["lbx_ok"] and "PartDB_SMD15.lbx" in info["vorlagen"]
    assert info["rueckgaengig"] is None
    assert (tmp_path / "stammdaten.yaml").exists()

    # Inhalt anlegen, doppeltes Kürzel abweisen, umbenennen (Position bleibt)
    new = {"key": "M12", "name": "M12", "color": "Pink"}
    assert c.post("/api/stammdaten/kategorien", json=new).status_code == 400      # Farbe nicht in Palette
    assert c.post("/api/stammdaten/farben", json={"name": "Pink", "hex": "#FF69B4"}).status_code == 200
    assert c.post("/api/stammdaten/farben", json={"name": "pink", "hex": "#000000"}).status_code == 400
    assert c.post("/api/stammdaten/kategorien", json=dict(new, color="PINK")).status_code == 200
    assert cfg.categories["M12"].color == "Pink" and cfg.categories["M12"].color_hex == "#ff69b4"

    # Gleiche Farbe = gleicher Hex, Umbenennen wirkt auf alle Inhalte, genutzte Farbe nicht löschbar
    assert cfg.categories["M6"].color_hex == cfg.categories["SMDICs"].color_hex == cfg.colors["Rot"]
    r = c.put("/api/stammdaten/farben/Rot", json={"name": "Signalrot", "hex": "#cc0000"}).json()
    assert cfg.categories["M6"].color == "Signalrot" and cfg.categories["SMDICs"].color_hex == "#cc0000"
    assert c.delete("/api/stammdaten/farben/Signalrot").status_code == 400
    c.post("/api/stammdaten/undo")
    assert cfg.categories["M6"].color == "Rot" and "Signalrot" not in cfg.colors
    assert cfg.hex_for("hell blau") == cfg.hex_for("Hell Blau") != ""   # Schreibweise egal
    assert c.post("/api/stammdaten/kategorien", json=new).status_code == 400
    assert c.post("/api/stammdaten/kategorien", json=dict(new, key="A B")).status_code == 400
    r = c.put("/api/stammdaten/kategorien/M8", json={"key": "M8x", "name": "M8 lang", "color": "Grün", "color_hex": "#2e8b57"})
    keys = [k["key"] for k in r.json()["kategorien"]]
    assert keys.index("M8x") == 7 and "M8" not in keys

    # Lagerart: neu, Abteile ändern, löschen + rückgängig
    assert c.post("/api/stammdaten/lagerarten", json={"name": "WinBin8", "compartments": 8}).status_code == 200
    assert c.put("/api/stammdaten/lagerarten/Rack", json={"name": "Rack", "compartments": 4}).status_code == 200
    assert cfg.box_types["Rack"].compartments == 4
    assert c.post("/api/stammdaten/lagerarten", json={"name": "X", "file": "a/b.csv"}).status_code == 400
    assert c.post("/api/stammdaten/lagerarten", json={"name": "X", "lbx": "x.txt"}).status_code == 400
    r = c.delete("/api/stammdaten/lagerarten/SMD15").json()
    assert "SMD15" not in cfg.box_types and r["rueckgaengig"] == "Lagerart 'SMD15' gelöscht"
    assert c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "SMD15"}).status_code == 400
    r = c.post("/api/stammdaten/undo").json()
    assert r["rueckgaengig_gemacht"] == "Lagerart 'SMD15' gelöscht"
    assert [t["name"] for t in r["lagerarten"]].index("SMD15") == 6   # wieder an alter Stelle

    # Meta liefert Lagerarten mit Abteilen für die Vorbelegung bei "Neue Box"
    meta = {t["name"]: t for t in c.get("/api/meta").json()["boxtypen"]}
    assert meta["Rack"]["compartments"] == 4

    # Neustart: Stammdaten + Rückgängig-Verlauf bleiben erhalten
    c2 = TestClient(create_app(load_config(tmp_path / "config.yaml"), transport=httpx.MockTransport(fake)))
    info = c2.get("/api/stammdaten").json()
    assert "M12" in [k["key"] for k in info["kategorien"]]
    assert info["rueckgaengig"] == "Lagerart 'Rack' geändert"
    c2.post("/api/stammdaten/undo"); c2.post("/api/stammdaten/undo")
    info = c2.get("/api/stammdaten").json()
    assert "WinBin8" not in [t["name"] for t in info["lagerarten"]]
    assert {t["name"]: t for t in info["lagerarten"]}["Rack"]["compartments"] == 1


class FakePrinter:
    BpacError = Exception

    def __init__(self):
        self.jobs = []

    def status(self, printer):
        return {"ok": True, "grund": "", "drucker": ["Brother PT-P710BT"]}

    def print_labels(self, template, rows, printer, flags):
        # Jede Spalte, die die Vorlage nutzt, muss in den Zeilen stehen
        from labeltool.bpac import merge_fields
        used = {f for _, f in merge_fields(template) if f}
        assert all(used <= set(r) for r in rows), (template.name, used - set(rows[0]))
        self.jobs.append((template.name, rows, flags))
        return len(rows)


def test_direktdruck(tmp_path):
    c, fake, cfg = make(tmp_path)
    prn = FakePrinter()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn))
    assert c.get("/api/print-status").json()["ok"] is True

    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 2})
    r = c.post("/api/name-labels/print").json()
    assert r["gedruckt"] == 2 and prn.jobs[0][0] == "Layout_Lagerbox_PartDB.lbx"
    assert prn.jobs[0][2] == 0x1                       # Standard: nur Autocut
    assert c.post("/api/name-labels/done").json()["erledigt"] == 2

    r = c.post("/api/content-labels/print").json()
    assert r["gedruckt"] == [{"vorlage": "PartDB_3U-3.lbx", "labels": 1}] and r["bauteile"] == 1
    assert prn.jobs[1][1][0]["Section3"] == "MT3608"
    assert c.post("/api/content-labels/done").json()["entfernt"] == 1

    cfg.direct_print = False
    assert c.post("/api/name-labels/print").status_code == 400
    assert c.get("/api/print-status").json()["aktiv"] is False


def test_vorlagen_felder():
    """Alle echten Vorlagen: genutzte Spalten sind im CSV-Schema des Tools enthalten."""
    from labeltool.bpac import merge_fields
    from labeltool.labels import NAME_COLUMNS, content_columns
    for f in VORLAGEN.glob("*.lbx"):
        used = {fld for _, fld in merge_fields(f) if fld}
        cols = NAME_COLUMNS if f.name.startswith("Layout_Lagerbox") else content_columns(1)
        assert used and used <= set(cols), (f.name, used - set(cols))
