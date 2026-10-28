"""Boxen nachträglich ändern, löschen und das Anlegen rückgängig machen."""
from test_smoke import make


def test_anlegen_rueckgaengig(tmp_path):
    c, fake, _ = make(tmp_path)
    created = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 2}).json()["created"]
    ids = [b["id"] for b in created]
    assert [b["id"] for b in c.get("/api/name-labels").json()["boxes"]] == ids

    r = c.post("/api/boxes/loeschen", json={"ids": ids}).json()
    assert [d["name"] for d in r["geloescht"]] == ["MIC-2.0021", "MIC-2.0022"] and r["fehler"] == []
    assert fake.deleted == ids
    assert c.get("/api/name-labels").json()["boxes"] == []                 # auch aus der Warteschlange
    # die Nummern sind wieder frei
    assert c.get("/api/boxes/preview", params={"abbr": "MIC", "compartments": 2}).json()["names"] == ["MIC-2.0021"]

    # zweimal: Box gibt es nicht mehr
    r = c.post("/api/boxes/loeschen", json={"ids": [ids[0]]}).json()
    assert r["geloescht"] == [] and "nicht (mehr)" in r["fehler"][0]["grund"]


def test_nur_leere_boxen_loeschen(tmp_path):
    c, fake, _ = make(tmp_path)
    child = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "WinBin2", "parent_id": 203}).json()
    r = c.post("/api/boxes/loeschen", json={"ids": [250, 203, child["created"][0]["id"]]}).json()
    grund = {f["name"]: f["grund"] for f in r["fehler"]}
    assert "2 Bauteil(e): AMS1117-3.3, MT3608" in grund["SMDICs-1.0013"]   # Bauteile darin
    assert "Bauteil" in grund["MIC-2.0020"]                                 # ESP32-C3 liegt darin
    assert [d["name"] for d in r["geloescht"]] == ["MIC-1.0021"]
    assert 250 in fake.locations and 203 in fake.locations
    assert c.post("/api/boxes/loeschen", json={"ids": []}).status_code == 422


def test_unterlagerorte_schuetzen(tmp_path):
    c, fake, _ = make(tmp_path)
    parent = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "WinBin2"}).json()["created"][0]
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "WinBin2", "parent_id": parent["id"]})
    r = c.post("/api/boxes/loeschen", json={"ids": [parent["id"]]}).json()
    assert r["fehler"][0]["grund"] == "hat Unter-Lagerorte" and parent["id"] in fake.locations


def test_box_bearbeiten(tmp_path):
    c, fake, cfg = make(tmp_path)
    box = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2"}).json()["created"][0]
    c.post("/api/name-labels/done")
    lid = box["id"]

    # andere Abteile: gleiche Nummer
    r = c.put(f"/api/boxes/{lid}", json={"abbr": "MIC", "compartments": 3, "box_type": "WinBin2"}).json()
    assert r["name_alt"] == "MIC-2.0021" and r["box"]["name"] == "MIC-3.0021" and r["box"]["compartments"] == "3"
    assert r["box_label_neu"] is True

    # anderer Inhalt: Kürzel + nächste freie Nummer des neuen Kürzels; Farbe des neuen Inhalts
    other = next(k for k in cfg.categories if k != "MIC")
    r = c.put(f"/api/boxes/{lid}", json={"abbr": other, "compartments": 3, "box_type": "WinBin2"}).json()
    assert r["box"]["name"] == f"{other}-3.0001" and r["box"]["content"] == cfg.categories[other].name
    assert r["box"]["color"] == cfg.categories[other].color

    # nur die Lagerart: Name bleibt, Box-Label stimmt noch
    c.post("/api/name-labels/done")
    c.post("/api/name-labels/export")
    c.post("/api/name-labels/done")
    r = c.put(f"/api/boxes/{lid}", json={"abbr": other, "compartments": 3, "box_type": "3U2x1_3"}).json()
    assert r["box"]["box_type"] == "3U2x1_3" and r["box_label_neu"] is False
    assert lid not in [b["id"] for b in c.get("/api/name-labels").json()["boxes"]]

    # nichts geändert
    assert c.put(f"/api/boxes/{lid}", json={"abbr": other, "compartments": 3, "box_type": "3U2x1_3"}).json()["geaendert"] is False

    # Fehler
    assert c.put(f"/api/boxes/{lid}", json={"abbr": "XX", "compartments": 3, "box_type": "WinBin2"}).status_code == 400
    assert c.put(f"/api/boxes/{lid}", json={"abbr": "MIC", "compartments": 3, "box_type": "Nix"}).status_code == 400
    assert c.put("/api/boxes/9999", json={"abbr": "MIC", "compartments": 3, "box_type": "WinBin2"}).status_code == 404


def test_bearbeiten_behaelt_freitext_und_meldet_namenskonflikt(tmp_path):
    c, fake, _ = make(tmp_path)
    fake.locations[250]["comment"] = "<p>Regal oben Lagerinhalt: SMD ICs Lagerfarbe: Rot Lagerabteile: 1</p>"
    r = c.put("/api/boxes/250", json={"abbr": "SMDICs", "compartments": 1, "box_type": "3U2x1_3", "color": "Rot"})
    assert r.status_code == 200, r.text
    assert fake.locations[250]["comment"].startswith("Regal oben Lagerinhalt: SMD ICs")
    assert r.json()["box"]["name"] == "SMDICs-1.0013" and r.json()["box_label_neu"] is False

    # MIC-2.0020 auf 1 Abteil -> MIC-1.0020; gibt es den Namen schon, wird nichts geändert
    fake.locations[777] = {"id": 777, "name": "MIC-1.0020", "comment": ""}
    r = c.put("/api/boxes/203", json={"abbr": "MIC", "compartments": 1, "box_type": "WinBin2"})
    assert r.status_code == 409 and fake.locations[203]["name"] == "MIC-2.0020"
