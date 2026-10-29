"""Bauteil-Labels: Box suchen und Inhaltslabel nachdrucken (ohne Tags zu ändern)."""
import httpx
from fastapi.testclient import TestClient

from labeltool.app import create_app
from test_smoke import FakePrinter, make, read_csv


def names(r):
    return [t["box"]["name"] for t in r.json()["treffer"]]


def test_suche(tmp_path):
    c, fake, cfg = make(tmp_path)
    fake.parts[305]["manufacturer_product_number"] = "AMS1117-3.3V-SOT223"
    assert c.get("/api/content-labels/suche", params={"q": ""}).json() == {"treffer": [], "anzahl": 0}

    # Box-Barcode, ID und Name
    assert names(c.get("/api/content-labels/suche", params={"q": "S0250"})) == ["SMDICs-1.0013"]
    assert names(c.get("/api/content-labels/suche", params={"q": "203"})) == ["MIC-2.0020"]
    assert names(c.get("/api/content-labels/suche", params={"q": "mic-2"})) == ["MIC-2.0020"]

    # Bauteil in der Box: Name, Teile-ID, MPN, Footprint – passende Bauteile werden genannt
    r = c.get("/api/content-labels/suche", params={"q": "esp32"}).json()
    assert [t["box"]["name"] for t in r["treffer"]] == ["MIC-2.0020"]
    assert r["treffer"][0]["passende_bauteile"] == ["ESP32-C3"]
    assert names(c.get("/api/content-labels/suche", params={"q": "#304"})) == ["SMDICs-1.0013"]
    assert names(c.get("/api/content-labels/suche", params={"q": "sot223"})) == ["SMDICs-1.0013"]
    assert names(c.get("/api/content-labels/suche", params={"q": "SOT-23"})) == ["SMDICs-1.0013"]

    # alle Wörter müssen passen (Box + Bauteil kombinierbar)
    assert names(c.get("/api/content-labels/suche", params={"q": "ics mt3608"})) == ["SMDICs-1.0013"]
    assert names(c.get("/api/content-labels/suche", params={"q": "ics esp32"})) == []

    # Treffer enthält das komplette Label (alle Bauteile, nicht nur getaggte)
    t = c.get("/api/content-labels/suche", params={"q": "S0250"}).json()["treffer"][0]
    assert {s["name"] for s in t["slots"] if s} == {"MT3608", "AMS1117-3.3"}


def test_nachdruck_direkt_aendert_keine_tags(tmp_path):
    _, fake, cfg = make(tmp_path)
    prn = FakePrinter()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn))
    r = c.post("/api/content-labels/nachdruck", json={"ids": [203, 250], "direkt": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert sorted(g["vorlage"] for g in d["gedruckt"]) == ["PartDB_3U-3.lbx", "PartDB_Window-Div-Bin-2.lbx"]
    assert d["uebersprungen"] == []
    assert sorted(job[1][0]["Location"] for job in prn.jobs) == ["MIC-2.0020", "SMDICs-1.0013"]
    # Tags und offener Export bleiben unberührt
    assert fake.parts[304]["tags"] == "noLabel" and fake.patches == []
    assert c.post("/api/content-labels/done").status_code == 400      # kein offener Export


def test_nachdruck_csv_und_schutz(tmp_path):
    _, fake, cfg = make(tmp_path)
    prn = FakePrinter()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn))
    r = c.post("/api/content-labels/nachdruck", json={"ids": [250]})
    assert r.status_code == 200, r.text
    datei = [d for d in r.json()["dateien"] if d["datei"].endswith("Inhalt_3U2x1_3.csv")][0]
    assert read_csv(datei["datei"])[0]["Location"] == "SMDICs-1.0013"

    assert c.post("/api/content-labels/nachdruck", json={"ids": [999]}).status_code == 404
    assert c.post("/api/content-labels/nachdruck", json={"ids": []}).status_code == 422

    # offener Export mit getaggten Teilen -> CSV-Nachdruck würde ihn überschreiben
    assert c.post("/api/content-labels/export").status_code == 200
    r = c.post("/api/content-labels/nachdruck", json={"ids": [203]})
    assert r.status_code == 409 and "offenen Export" in r.json()["detail"]
    # direkt drucken geht trotzdem, der offene Export bleibt bestehen
    assert c.post("/api/content-labels/nachdruck", json={"ids": [250], "direkt": True}).status_code == 200
    assert c.post("/api/content-labels/done").json()["entfernt"] == 1


def test_druckliste_mit_nachdrucken(tmp_path):
    _, fake, cfg = make(tmp_path)
    prn = FakePrinter()
    cfg.direct_print = True
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn))
    liste = lambda: {l["box"]["name"]: (l["getaggt"], l["nachdruck"]) for l in c.get("/api/content-labels").json()}
    assert liste() == {"SMDICs-1.0013": (True, False)}                     # nur die Box mit Tag

    # Nachdruck hinzufügen – auch die getaggte Box (doppelt erscheint sie nicht)
    r = c.post("/api/content-labels/liste", json={"ids": [203, 250, 203]}).json()
    assert r["hinzugefuegt"] == ["MIC-2.0020", "SMDICs-1.0013"]
    assert liste() == {"MIC-2.0020": (False, True), "SMDICs-1.0013": (True, True)}
    assert c.post("/api/content-labels/liste", json={"ids": [203]}).json()["hinzugefuegt"] == []
    assert c.post("/api/content-labels/liste", json={"ids": [999]}).status_code == 404

    # nur Nachdrucke lassen sich entfernen; die getaggte Box bleibt trotzdem (wegen Tag)
    assert c.delete("/api/content-labels/liste/250").status_code == 200
    assert liste() == {"MIC-2.0020": (False, True), "SMDICs-1.0013": (True, False)}
    r = c.delete("/api/content-labels/liste/250")
    assert r.status_code == 404 and "Tag" in r.json()["detail"]

    # drucken: beide; „Gedruckt“ entfernt Tag und Nachdruck-Eintrag
    r = c.post("/api/content-labels/print").json()
    assert sorted(l["name"] for l in r["labels"]) == ["MIC-2.0020", "SMDICs-1.0013"] and r["nachdruck"] == 1
    c.post("/api/content-labels/liste", json={"ids": [250]})              # nach dem Druck hinzugefügt: bleibt
    d = c.post("/api/content-labels/done").json()
    assert d["entfernt"] == 1 and d["nachdruck_erledigt"] == 1
    assert liste() == {"SMDICs-1.0013": (False, True)}


def test_nur_nachdrucke_exportieren(tmp_path):
    _, fake, cfg = make(tmp_path)
    fake.parts[304]["tags"] = ""                                            # kein Tag mehr
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=FakePrinter()))
    assert c.post("/api/content-labels/export").status_code == 400
    c.post("/api/content-labels/liste", json={"ids": [203]})
    r = c.post("/api/content-labels/export").json()
    assert r["bauteile"] == 0 and r["nachdruck"] == 1
    assert c.post("/api/content-labels/done").json()["nachdruck_erledigt"] == 1
    assert c.get("/api/content-labels").json() == []
    assert c.post("/api/content-labels/done").status_code == 400


def test_geloeschte_box_verschwindet_aus_der_liste(tmp_path):
    c, fake, cfg = make(tmp_path)
    box = c.post("/api/boxes", json={"abbr": "MIC", "compartments": 1, "box_type": "WinBin2"}).json()["created"][0]
    c.post("/api/content-labels/liste", json={"ids": [box["id"]]})
    assert c.post("/api/boxes/loeschen", json={"ids": [box["id"]]}).json()["geloescht"]
    assert box["id"] not in [l["box"]["id"] for l in c.get("/api/content-labels").json()]


def test_offener_druck_nach_neuladen(tmp_path):
    _, fake, cfg = make(tmp_path)
    cfg.direct_print = True
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=FakePrinter()))
    assert c.get("/api/content-labels/offen").json() == {"bauteile": 0, "nachdruck": 0}
    c.post("/api/content-labels/print")
    # neue App-Instanz = Seite/Server neu geladen: der offene Druck ist noch bekannt
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=FakePrinter()))
    assert c.get("/api/content-labels/offen").json() == {"bauteile": 1, "nachdruck": 0}
    assert c.post("/api/content-labels/done").json()["entfernt"] == 1
    assert fake.parts[304]["tags"] == "" and c.get("/api/content-labels/offen").json()["bauteile"] == 0
