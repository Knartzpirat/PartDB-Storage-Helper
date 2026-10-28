"""Geschriebene CSVs im Browser herunterladen: eine Datei als CSV, mehrere als ZIP."""
import io
import zipfile

from test_smoke import make


def test_csv_download(tmp_path):
    c, fake, cfg = make(tmp_path)
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 1})
    ex = c.post("/api/name-labels/export").json()
    name = ex["datei"].replace("\\", "/").split("/")[-1]

    r = c.get("/api/ausgabe/download", params={"datei": name})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'filename="{name}"' in r.headers["content-disposition"] and b"Lagername" in r.content

    files = [d["datei"].replace("\\", "/").split("/")[-1] for d in c.post("/api/content-labels/export").json()["dateien"]]
    r = c.get("/api/ausgabe/download", params={"datei": [name, *files]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == sorted({name, *files})


def test_csv_download_nur_ausgabeordner(tmp_path):
    c, fake, cfg = make(tmp_path)
    (tmp_path / "geheim.csv").write_text("x", encoding="utf-8")       # neben dem Ausgabeordner
    for bad in ("../geheim.csv", "..\geheim.csv", "config.yaml", "/etc/passwd", ".versteckt.csv", "a/b.csv"):
        assert c.get("/api/ausgabe/download", params={"datei": bad}).status_code == 400, bad
    assert c.get("/api/ausgabe/download", params={"datei": "gibtsnicht.csv"}).status_code == 404
    assert c.get("/api/ausgabe/download").status_code == 422
