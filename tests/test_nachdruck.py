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
