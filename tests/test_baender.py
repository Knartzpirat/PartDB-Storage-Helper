"""Box-Label-Vorlage je Lagerart und Druck Band für Band (mehrere Bandbreiten in einem Auftrag)."""
import zipfile

import httpx
from fastapi.testclient import TestClient

from labeltool import bpac
from labeltool.app import create_app
from test_helfer import connected, setup
from test_smoke import FakePrinter, make, read_csv

BOX_12 = "Layout_Lagerbox_12mm.lbx"


def box_template_12mm(cfg):
    """Kopie der 24-mm-Box-Vorlage, die laut Vorlage 12-mm-Band braucht."""
    src = cfg.template_dir / cfg.box_template
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(cfg.template_dir / BOX_12, "w", zipfile.ZIP_DEFLATED) as out:
        for item in z.infolist():
            data = z.read(item.filename)
            if item.filename == "label.xml":
                data = data.decode("utf-8").replace('width="68pt"', 'width="33.6pt"', 1).encode("utf-8")
            out.writestr(item, data)


def set_box_template(c, name, box_lbx):
    t = next(t for t in c.get("/api/stammdaten").json()["lagerarten"] if t["name"] == name)
    r = c.put(f"/api/stammdaten/lagerarten/{name}", json={"name": name, "compartments": t["compartments"],
                                                          "file": t["file"], "lbx": t["lbx"], "box_lbx": box_lbx})
    assert r.status_code == 200, r.text
    return r.json()


def test_box_vorlage_je_lagerart_und_csv_je_vorlage(tmp_path):
    c, fake, cfg = make(tmp_path)
    box_template_12mm(cfg)
    info = set_box_template(c, "SMD15", BOX_12)
    smd = next(t for t in info["lagerarten"] if t["name"] == "SMD15")
    assert smd["box_lbx"] == BOX_12 and smd["box_lbx_ok"] is True
    assert "box_vorlage: " + BOX_12 in cfg.master_file.read_text(encoding="utf-8")
    assert cfg.box_template_for("SMD15") == BOX_12 and cfg.box_template_for("WinBin2") == cfg.box_template
    assert set_box_template(c, "SMD20", "nicht da.lbx")    # erlaubt, wird als „fehlt“ angezeigt
    assert c.put("/api/stammdaten/lagerarten/SMD20", json={"name": "SMD20", "box_lbx": "x.csv"}).status_code == 400

    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2"})
    c.post("/api/boxes", json={"abbr": "SMDICs", "compartments": 1, "box_type": "SMD15", "count": 2})
    r = c.post("/api/name-labels/export", json={"open_template": True}).json()
    files = {f["vorlage"]: f for f in r["dateien"]}
    assert set(files) == {cfg.box_template, BOX_12} and r["labels"] == 3 and "vorlage" not in r
    assert files[BOX_12]["datei"].endswith("_Layout_Lagerbox_12mm.csv") and files[BOX_12]["labels"] == 2
    assert [row["Lagername"] for row in read_csv(files[cfg.box_template]["datei"])] == ["MIC-2.0021"]
    # Vorlage der Lagerart mit ihrer CSV öffnen
    assert c.post("/api/templates/open", json={"box_vorlage": BOX_12}).status_code == 200
    assert c.post("/api/templates/open", json={"box_vorlage": "PartDB_SMD15.lbx"}).status_code == 400


def test_druck_band_fuer_band(tmp_path):
    zwoelf = lambda name: {"name": name, "angeschlossen": True, "status_text": "bereit", "bereit": True, "band": "12 mm"}
    c, cfg, kern, prn, _ = setup(tmp_path, state_fn=zwoelf)
    box_template_12mm(cfg)
    set_box_template(c, "SMD15", BOX_12)
    with connected(c, kern):
        c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2"})
        c.post("/api/boxes", json={"abbr": "SMDICs", "compartments": 1, "box_type": "SMD15"})

        # Plan: eingelegtes Band (12 mm) zuerst, dann 24 mm
        info = c.post("/api/druck/pruefen", json={"art": "box"}).json()
        assert info["mehrere_baender"] is True and info["passt"] is False and info["passt_start"] is True
        assert [(b["band_mm"], b["labels"], b["vorlagen"]) for b in info["plan"]] == [
            (12, 1, [BOX_12]), (24, 1, [cfg.box_template])]

        # Band 1: nur die 12-mm-Labels
        r = c.post("/api/name-labels/print", json={"nur_band": True, "band_mm": 12}).json()
        assert [l["name"] for l in r["labels"]] == ["SMDICs-1.0014"] and r["labels"][0]["vorlage"] == BOX_12
        assert [j[0] for j in prn.jobs] == [BOX_12]

        # Band 2 ohne Wechsel: 12 mm liegt ein -> 409, nichts gedruckt
        r = c.post("/api/name-labels/print", json={"nur_band": True, "band_mm": 24, "fortsetzen": True})
        assert r.status_code == 409 and "Falsches Band" in r.json()["detail"] and len(prn.jobs) == 1
        r = c.post("/api/name-labels/print", json={"nur_band": True, "band_mm": 24, "fortsetzen": True,
                                                   "band_gewechselt": True}).json()
        assert [l["name"] for l in r["labels"]] == ["MIC-2.0021"] and prn.jobs[-1][0] == cfg.box_template

        # „Gedruckt ✓“ gilt für beide Bänder
        assert c.post("/api/name-labels/done").json()["erledigt"] == 2


class WrongTape(FakePrinter):
    """b-PAC meldet beim Senden ein falsches Band (wie bpac.check_media)."""

    def print_labels(self, template, rows, printer, flags):
        raise bpac.BpacError("Falsches Band eingelegt: 24 mm – die Vorlage braucht 12 mm. Bitte Band wechseln.")


def test_falsches_band_beim_senden_ist_409(tmp_path):
    _, fake, cfg = make(tmp_path)
    cfg.direct_print = True
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=WrongTape()))
    r = c.post("/api/content-labels/print", json={"band_gewechselt": True})
    assert r.status_code == 409 and r.json()["detail"].startswith("Falsches Band")


def test_bauteile_fortsetzen_sammelt_tags(tmp_path):
    _, fake, cfg = make(tmp_path)
    cfg.direct_print = True
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=FakePrinter()))
    fake.parts[306]["tags"] = "noLabel"                    # zweite Box (MIC-2.0020) mit Tag
    r = c.post("/api/content-labels/print", json={"nur_band": True, "band_mm": 12}).json()
    assert r["bauteile"] == 2
    # Band, das kein Label braucht: nichts gedruckt, die vorgemerkten Tags bleiben erhalten
    r = c.post("/api/content-labels/print", json={"nur_band": True, "band_mm": 24, "fortsetzen": True}).json()
    assert r["labels"] == [] and r["bauteile"] == 2
    assert c.post("/api/content-labels/done").json()["entfernt"] == 2


def test_art_der_vorlage(tmp_path):
    c, fake, cfg = make(tmp_path)
    tpl = lambda name: c.get(f"/api/vorlagen/{name}").json()
    # erkannt: Box-Label am Feld „Lagername“, Bauteil-Label sonst
    assert tpl(cfg.box_template)["art"] == "box" and tpl("PartDB_SMD15.lbx")["art"] == "bauteil"
    info = c.get("/api/stammdaten").json()
    assert info["box_vorlagen"] == [cfg.box_template]

    # Kopie einer Bauteil-Vorlage als Box-Label anlegen und SMD15 zuweisen -> box_lbx, nicht lbx
    r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "Box_SMD.lbx",
                                            "lagerart": "SMD15", "art": "box"})
    assert r.status_code == 200, r.text
    assert r.json()["art"] == "box"
    assert cfg.box_types["SMD15"].box_lbx == "Box_SMD.lbx" and cfg.box_types["SMD15"].lbx == "PartDB_SMD15.lbx"
    d = tpl("Box_SMD.lbx")
    assert d["art"] == "box" and d["art_festgelegt"] is True and d["box_lagerarten"] == ["SMD15"]
    assert "Box_SMD.lbx" in c.get("/api/stammdaten").json()["box_vorlagen"]
    # Beispieldaten passend zur Art
    assert "Lagername" in c.get("/api/vorlagen/Box_SMD.lbx/beispiel").json()["spalten"]

    # Art umstellen (rückgängig machbar)
    assert c.put("/api/vorlagen/Box_SMD.lbx/art", json={"art": "bauteil"}).json()["art"] == "bauteil"
    assert "Section1" in c.get("/api/vorlagen/Box_SMD.lbx/beispiel").json()["spalten"]
    assert "vorlagen_art" in cfg.master_file.read_text(encoding="utf-8")
    assert c.post("/api/stammdaten/undo").status_code == 200
    assert tpl("Box_SMD.lbx")["art"] == "box"
    assert c.put("/api/vorlagen/Box_SMD.lbx/art", json={"art": "quatsch"}).status_code == 400
    assert c.put("/api/vorlagen/gibtsnicht.lbx/art", json={"art": "box"}).status_code == 404


def test_csv_sortierung(tmp_path):
    c, fake, cfg = make(tmp_path)
    c.post("/api/boxes", json={"abbr": "SMDICs", "compartments": 1, "box_type": "SMD15"})
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2"})
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "SMD15"})
    names = lambda sort: [row["Lagername"] for row in read_csv(
        c.post("/api/name-labels/export", json={"sortierung": sort}).json()["datei"])]
    assert names("standard") == ["SMDICs-1.0014", "MIC-2.0021", "MIC-1.0022"]          # wie die Liste
    assert names("name") == ["MIC-1.0022", "MIC-2.0021", "SMDICs-1.0014"]
    assert names("lagerart") == ["MIC-1.0022", "SMDICs-1.0014", "MIC-2.0021"]           # SMD15 vor WinBin2
    assert names("inhalt")[:2] == ["MIC-1.0022", "MIC-2.0021"]                          # Microcontroller vor SMD ICs
    assert c.post("/api/name-labels/export", json={"sortierung": "quatsch"}).status_code == 400
    assert c.post("/api/content-labels/export", json={"sortierung": "farbe"}).status_code == 200


def test_standard_box_vorlage_waehlbar(tmp_path):
    c, fake, cfg = make(tmp_path)
    assert c.get("/api/stammdaten").json()["standard_box_vorlage"] == cfg.box_template   # Startwert aus config.yaml
    box_template_12mm(cfg)
    r = c.put("/api/stammdaten/box-vorlage", json={"datei": BOX_12})
    assert r.status_code == 200 and r.json()["standard_box_vorlage"] == BOX_12 and cfg.box_template == BOX_12
    assert "standard_box_vorlage: " + BOX_12 in cfg.master_file.read_text(encoding="utf-8")
    assert c.get("/api/meta").json()["box_vorlage"] == BOX_12

    # keine: Box ohne eigene Vorlage kann kein Box-Label drucken
    assert c.put("/api/stammdaten/box-vorlage", json={"datei": ""}).json()["standard_box_vorlage"] == ""
    cfg.direct_print = True
    box = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2"}).json()["created"][0]
    r = c.get(f"/api/name-labels/{box['id']}/vorschau")
    assert r.status_code == 400 and "Standard-Box-Vorlage" in r.json()["detail"]

    # bleibt nach Neustart (Stammdaten gehen vor config.yaml), Rückgängig stellt sie wieder her
    c2 = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None))
    assert c2.get("/api/stammdaten").json()["standard_box_vorlage"] == ""
    assert c2.post("/api/stammdaten/undo").json()["standard_box_vorlage"] == BOX_12
    assert c2.put("/api/stammdaten/box-vorlage", json={"datei": "fehlt.lbx"}).status_code == 404
