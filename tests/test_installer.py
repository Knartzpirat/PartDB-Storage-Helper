"""Helfer-Download (ZIP), Einrichtungs-Assistent (ohne Fenster) und Brother-Modellliste."""
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from labeltool import helfer_setup as hs
from labeltool import ptouch_modelle as pm
from labeltool import routes_helfer_download as rd
from labeltool.config import load_config
from labeltool.einstellungen import load_settings

from test_smoke import make

ROOT = Path(__file__).resolve().parents[1]
ECHTE_DLL = Path(r"C:\Program Files (x86)\Brother\P-touch Editor\6\lbxcore.Win32.dll")


# ------------------------------------------------------------------ Modellliste
def test_modelle_aus_bytes_filtert_und_sortiert():
    data = (b"\x00PT-P710BT\x00QL-820NWB\x00xPT-P700\x00PT-P700W;TD-4550DNWB\x00RJ-4230B\x00"
            b"PT-0000\x00PT-12\x00XPT-P900\x00PT-P900-foo\x00VC-500W\x00MFC-J470DW\x00\x00"
            + "TP-M5000N".encode("utf-16-le") + b"\x00\x00")
    found = pm.modelle_aus_bytes(data)
    assert {"PT-P710BT", "QL-820NWB", "PT-P700W", "TD-4550DNWB", "RJ-4230B", "VC-500W", "PT-12", "TP-M5000N"} <= found
    assert "PT-0000" not in found and "XPT-P900" not in found and not any("MFC" in m for m in found)
    assert "PT-P700" not in found          # "xPT-P700" klebt an einem Buchstaben
    order = pm.sortiert(["QL-820NWB", "PT-P710BT", "PT-18R", "PT-2430PC", "PT-D410", "TD-2020", "PT-P700", "VC-500W"])
    assert order == ["PT-18R", "PT-2430PC", "PT-D410", "PT-P700", "PT-P710BT", "QL-820NWB", "TD-2020", "VC-500W"]


def test_modelle_lesen_aus_fake_installation_mit_cache(tmp_path):
    inst = tmp_path / "Brother" / "P-touch Editor" / "6"
    inst.mkdir(parents=True)
    names = [f"PT-P{700 + i}BT" for i in range(12)] + ["QL-820NWB"]
    (inst / "lbxcore.Win32.dll").write_bytes(b"MZ\x00" + b"\x00".join(n.encode() for n in names) + b"\x00")
    (inst / "andere.dll").write_bytes(b"QL-999X\x00")
    cache = tmp_path / "cache.json"
    info = pm.modelle_lesen([inst], cache_datei=cache)
    assert info["modelle"][0] == "PT-P700BT" and info["modelle"][-1] == "QL-820NWB"
    assert len(info["modelle"]) == 13 and "lbxcore.Win32.dll" in info["quelle"]
    assert "QL-999X" not in info["modelle"]           # nur lbxcore*.dll, wenn vorhanden
    assert json.loads(cache.read_text(encoding="utf-8"))["signatur"]
    # Cache wird genutzt, solange sich die Datei nicht ändert
    c = json.loads(cache.read_text(encoding="utf-8")); c["modelle"] = c["modelle"][:-1] + ["PT-E550W"]
    cache.write_text(json.dumps(c), encoding="utf-8")
    assert "PT-E550W" in pm.modelle_lesen([inst], cache_datei=cache)["modelle"]


def test_modelle_aus_version5_ordner(tmp_path):
    inst = tmp_path / "Ptedit54"
    inst.mkdir()
    (inst / "ptedit54.exe").write_bytes(b"\x00".join(m.encode() for m in pm.EINGEBAUT[:20]))
    (inst / "zufall.dll").write_bytes(b"PT-P710BT only")    # zu wenige Treffer -> ignoriert
    info = pm.modelle_lesen([inst], cache_datei=None)
    assert info["modelle"] == pm.sortiert(pm.EINGEBAUT[:20]) and "ptedit54.exe" in info["quelle"]


def test_fallback_eingebaute_liste(tmp_path):
    info = pm.modelle_lesen([tmp_path], cache_datei=None)
    assert info["quelle"] == "eingebaut" and len(info["modelle"]) == 126 == len(set(pm.EINGEBAUT))
    assert "PT-P710BT" in info["modelle"] and info["modelle"] == pm.sortiert(pm.EINGEBAUT)
    assert pm.modelle_lesen([], cache_datei=None)["quelle"] == "eingebaut"


@pytest.mark.skipif(not ECHTE_DLL.is_file(), reason="P-touch Editor 6 nicht installiert")
def test_echte_dll_liefert_die_eingebaute_liste():
    info = pm.modelle_lesen([ECHTE_DLL.parent], cache_datei=None)
    assert len(info["modelle"]) >= 126 and set(pm.EINGEBAUT) <= set(info["modelle"])
    assert info["modelle"][:2] == ["PT-18NR", "PT-18R"]


def test_installierte_drucker_und_zuordnung():
    win = [("Brother PT-P710BT", "Brother PT-P710BT"), ("Microsoft Print to PDF", "Microsoft Print To PDF"),
           ("Brother MFC-J470DW Printer", "Microsoft IPP Class Driver"), ("Etiketten Büro", "Brother QL-820NWB"),
           ("Brother PT-P700W", "Brother PT-P700W")]
    inst = pm.installierte_drucker(windows=win, bpac_liste=["Brother PT-P710BT", "Brother TD-4550DNWB"])
    assert [(d["name"], d["modell"], d["bpac"]) for d in inst] == [
        ("Brother PT-P700W", "PT-P700W", False), ("Brother PT-P710BT", "PT-P710BT", True),
        ("Etiketten Büro", "QL-820NWB", False), ("Brother TD-4550DNWB", "TD-4550DNWB", True)]
    assert pm.passender_drucker("PT-P710BT", inst) == "Brother PT-P710BT"
    assert pm.passender_drucker("ql-820nwb", inst) == "Etiketten Büro"       # über den Treibernamen
    assert pm.passender_drucker("PT-P700", inst) is None                      # nicht PT-P700W
    assert pm.passender_drucker("PT-P700", ["Brother PT-P700", "Brother PT-P700W"]) == "Brother PT-P700"
    assert pm.passender_drucker("", inst) is None
    assert pm.modell_aus_name("Brother PT-P710BT (Kopie 1)") == "PT-P710BT"
    assert pm.modell_aus_name("Brother MFC-J470DW Printer") is None


# ------------------------------------------------------------------ Einrichtungs-Assistent (Logik)
def werte(tmp_path, **kw):
    d = dict(server="http://unraid:8765", token="abc_DEF-123", vorlagen_ordner=str(tmp_path / "Vorlagen"),
             drucker_modell="PT-P710BT", drucker="Brother PT-P710BT", autostart=False)
    d.update(kw)
    return hs.Werte(**d)


def test_pruefen():
    f, h = hs.pruefe(hs.Werte(server="ftp://x", token="", vorlagen_ordner="relativ", drucker_modell=""), pm.EINGEBAUT)
    assert len(f) == 4 and not h
    assert hs.normalisiere_server(" unraid:8765/ ") == "http://unraid:8765"
    with pytest.raises(hs.SetupFehler):
        hs.normalisiere_server("http://host:99999")
    f, _ = hs.pruefe(hs.Werte(server="h", token="a b", vorlagen_ordner="C:\\x", drucker_modell="XY-1"), pm.EINGEBAUT)
    assert any("Leerzeichen" in t for t in f) and any("Unbekanntes Druckermodell" in t for t in f)


def test_pruefen_ordner_hinweise(tmp_path):
    f, h = hs.pruefe(werte(tmp_path), pm.EINGEBAUT)
    assert not f and any("wird angelegt" in t for t in h) and any("noch keine .lbx" in t for t in h)
    (tmp_path / "Vorlagen").mkdir()
    (tmp_path / "Vorlagen" / "a.lbx").write_bytes(b"x")
    (tmp_path / "Vorlagen" / ".versteckt.lbx").write_bytes(b"x")
    (tmp_path / "Vorlagen" / ".backup").mkdir()
    (tmp_path / "Vorlagen" / ".backup" / "b.lbx").write_bytes(b"x")
    assert hs.lbx_anzahl(tmp_path / "Vorlagen") == 1
    assert hs.ordner_text(str(tmp_path / "Vorlagen")) == "1 Vorlage(n) (.lbx) gefunden."
    assert hs.pruefe(werte(tmp_path), pm.EINGEBAUT) == ([], [])
    (tmp_path / "datei").write_text("x")
    assert hs.pruefe(werte(tmp_path, vorlagen_ordner=str(tmp_path / "datei")))[0]


def test_helfer_yaml_schreiben_und_vom_helfer_lesbar(tmp_path):
    w = werte(tmp_path, vorlagen_ordner="C:\\Users\\lasse\\Documents\\Ä Vorlagen", token='t"ok\\en',
              drucker_modell="pt-p710bt", extra={"pfade": {"/ausgabe": "Z:\\Labeldaten"}, "status_intervall": 5})
    path = hs.schreibe_helfer_yaml(tmp_path / "helfer.yaml", w)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"server": "http://unraid:8765", "token": 't"ok\\en',
                    "vorlagen_ordner": "C:\\Users\\lasse\\Documents\\Ä Vorlagen", "drucker_modell": "PT-P710BT",
                    "drucker": "Brother PT-P710BT", "autostart": False,
                    "pfade": {"/ausgabe": "Z:\\Labeldaten"}, "status_intervall": 5}
    assert not (tmp_path / "helfer.yaml.tmp").exists()
    from labeltool.helfer import load_helper_config
    cfg = load_helper_config(path)
    assert cfg.server == "http://unraid:8765" and cfg.token == 't"ok\\en' and cfg.drucker == "Brother PT-P710BT"
    with pytest.raises(hs.SetupFehler):
        hs.schreibe_helfer_yaml(tmp_path / "x.yaml", werte(tmp_path, token=""))
    assert not (tmp_path / "x.yaml").exists()


def test_vorbelegung_aus_vorgabe_und_bisheriger_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "dokumente_ordner", lambda: tmp_path / "Dokumente")
    w = hs.lade_werte(tmp_path)
    assert w.server == "" and w.vorlagen_ordner == str(tmp_path / "Dokumente" / "Labeltool-Vorlagen") and w.autostart
    (tmp_path / "helfer.vorgabe.yaml").write_text('server: "http://neu:8765"\ntoken: "NEU"\n', encoding="utf-8")
    w = hs.lade_werte(tmp_path)
    assert (w.server, w.token) == ("http://neu:8765", "NEU")
    # bestehende helfer.yaml ist neuer -> ihre Werte gelten, Vorgabe nur als Lückenfüller
    (tmp_path / "helfer.yaml").write_text('server: "http://alt:1"\ntoken: ""\nvorlagen_ordner: "D:\\\\V"\n'
                                          'drucker_modell: "ql-820nwb"\nautostart: false\npfade: {"/a": "B:"}\n',
                                          encoding="utf-8")
    os.utime(tmp_path / "helfer.vorgabe.yaml", (1, 1))
    w = hs.lade_werte(tmp_path)
    assert (w.server, w.token, w.vorlagen_ordner, w.drucker_modell, w.autostart) == \
        ("http://alt:1", "NEU", "D:\\V", "QL-820NWB", False)
    assert w.extra == {"pfade": {"/a": "B:"}}
    # frisch heruntergeladenes Paket (Vorgabe neuer) bringt neuen Token/Server, Ordner bleibt
    os.utime(tmp_path / "helfer.vorgabe.yaml", None)
    os.utime(tmp_path / "helfer.yaml", (2, 2))
    w = hs.lade_werte(tmp_path)
    assert (w.server, w.token, w.vorlagen_ordner) == ("http://neu:8765", "NEU", "D:\\V")


def test_modellauswahl():
    inst = [{"name": "Brother PT-P710BT", "modell": "PT-P710BT"}, {"name": "QL", "modell": "QL-820NWB"}]
    e = hs.modell_eintraege(list(pm.EINGEBAUT), inst)
    assert e[:2] == [("PT-P710BT (installiert)", "PT-P710BT"), ("QL-820NWB (installiert)", "QL-820NWB")]
    assert len(e) == 126 and [m for _, m in e].count("PT-P710BT") == 1
    assert hs.modell_vorauswahl("", inst) == "PT-P710BT"
    assert hs.modell_vorauswahl("td-4550dnwb", inst) == "TD-4550DNWB"
    assert hs.modell_vorauswahl("", []) == ""
    assert hs.modell_hinweis("PT-P710BT", inst) == (True, "✓ Installiert als Windows-Drucker „Brother PT-P710BT“.")
    ok, text = hs.modell_hinweis("PT-P900W", inst)
    assert not ok and "Treiber" in text


class FakeRun:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        Path(kw["env"]["LT_LNK"]).write_bytes(b"lnk")
        return type("R", (), {"returncode": self.rc, "stderr": "kaputt"})()


def test_speichern_autostart_und_start_mit_fakes(tmp_path):
    (tmp_path / "helfer.bat").write_text("@echo off\r\n")
    start, run, popen = tmp_path / "Startup", FakeRun(), []
    w = werte(tmp_path, autostart=True, server="unraid:8765")
    out = hs.speichern(tmp_path, w, aus_bat=False, autostart_dir=start, runner=run,
                       popen=lambda *a, **k: popen.append((a, k)))
    assert (tmp_path / "Vorlagen").is_dir() and (start / hs.AUTOSTART_NAME).is_file()
    assert yaml.safe_load((tmp_path / "helfer.yaml").read_text(encoding="utf-8"))["server"] == "http://unraid:8765"
    env = run.calls[0][1]["env"]
    assert env["LT_ZIEL"] == str((tmp_path / "helfer.bat").resolve()) and env["LT_ORDNER"] == str(tmp_path.resolve())
    assert popen and popen[0][0][0][-1] == str(tmp_path / "helfer.bat") and len(out) == 3
    # aus helfer.bat: kein eigener Start; Autostart aus -> Verknüpfung weg
    popen.clear()
    hs.speichern(tmp_path, werte(tmp_path, autostart=False), aus_bat=True, autostart_dir=start, runner=run,
                 popen=lambda *a, **k: popen.append(a))
    assert not popen and not (start / hs.AUTOSTART_NAME).exists()
    with pytest.raises(hs.SetupFehler, match="kaputt"):
        hs.autostart_setzen(True, tmp_path / "helfer.bat", start, runner=FakeRun(rc=1))


@pytest.mark.skipif(sys.platform != "win32", reason="Verknüpfung nur unter Windows")
def test_autostart_verknuepfung_echt_in_tmp(tmp_path):
    (tmp_path / "helfer.bat").write_text("@echo off\r\n")
    lnk = hs.autostart_setzen(True, tmp_path / "helfer.bat", tmp_path / "Startup")
    assert lnk.is_file() and lnk.read_bytes()[:4] == b"L\x00\x00\x00"      # Shell-Link-Kopf
    hs.autostart_setzen(False, tmp_path / "helfer.bat", tmp_path / "Startup")
    assert not lnk.exists()


def test_verbindung_testen():
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []
    ok, text = hs.verbindung_testen("unraid:8765/", opener=lambda req, timeout: (
        seen.append(req.full_url), Resp(json.dumps({"kategorien": []}).encode()))[1])
    assert ok and seen == ["http://unraid:8765/api/meta"]
    assert not hs.verbindung_testen("x", opener=lambda req, timeout: Resp(b"<html>"))[0]
    import urllib.error
    def boom(req, timeout): raise urllib.error.URLError("Verbindung verweigert")
    ok, text = hs.verbindung_testen("x", opener=boom)
    assert not ok and "Verbindung verweigert" in text

    # mit Token: zweiter Aufruf gegen /api/helfer/token-pruefen
    def antworten(gueltig):
        def opener(req, timeout):
            if req.full_url.endswith("/api/meta"):
                return Resp(json.dumps({"kategorien": []}).encode())
            assert req.full_url.endswith("/api/helfer/token-pruefen") and json.loads(req.data)["token"] == "T"
            return Resp(json.dumps({"ok": gueltig, "grund": "" if gueltig else "falsch"}).encode())
        return opener
    assert hs.verbindung_testen("x", "T", opener=antworten(True)) == (True, "✓ Labeltool-Server erreichbar, Token gültig.")
    ok, text = hs.verbindung_testen("x", "T", opener=antworten(False))
    assert not ok and "Token passt nicht" in text


def test_konsole_assistent(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "verbindung_testen", lambda s, t="": (True, "ok"))
    antworten = iter(["", "", str(tmp_path / "V"), "", "n",            # erster Durchlauf: Token fehlt
                      "", "TOKEN1", "", "", ""])
    out, saved = [], []
    ok = hs.konsole_assistent(tmp_path, hs.Werte(server="http://s:1", vorlagen_ordner="", autostart=False),
                              aus_bat=True, eingabe=lambda p: next(antworten), ausgabe=out.append,
                              modelle_fn=lambda: list(pm.EINGEBAUT),
                              drucker_fn=lambda: [{"name": "Brother PT-P710BT", "modell": "PT-P710BT"}],
                              speichern_fn=lambda o, w, aus_bat: saved.append(w) or ["gespeichert"])
    assert ok and any("FEHLER" in t and "Token" in t for t in out)
    w = saved[0]
    assert (w.token, w.drucker_modell, w.drucker, w.vorlagen_ordner, w.autostart) == \
        ("TOKEN1", "PT-P710BT", "Brother PT-P710BT", str(tmp_path / "V"), False)


def _anzeige():
    try:
        import tkinter
        r = tkinter.Tk(); r.destroy()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _anzeige(), reason="tkinter/Anzeige nicht verfügbar")
def test_fenster_smoke(tmp_path):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:   # z. B. "Can't find a usable tk.tcl" (Store-Python, ohne Anzeige)
        pytest.skip(f"Tk nicht verfügbar: {e}")
    root.withdraw()
    a = hs.Assistent(root, tmp_path, werte(tmp_path, drucker_modell=""),
                     modelle_fn=lambda: list(pm.EINGEBAUT),
                     drucker_fn=lambda: [{"name": "Brother PT-P710BT", "modell": "PT-P710BT"}],
                     speichern_fn=lambda *a, **k: [])
    root.after(1500, root.destroy)
    root.mainloop()
    assert a.v_modell.get() == "PT-P710BT (installiert)"     # Hintergrund-Suche hat vorausgewählt
    w = a.werte_lesen()
    assert (w.drucker_modell, w.drucker, w.token) == ("PT-P710BT", "Brother PT-P710BT", "abc_DEF-123")


# ------------------------------------------------------------------ Download
def app_mit_download(tmp_path, token=None):
    _, _, cfg = make(tmp_path)
    if token is not None:
        cfg.helper_token = token
    app = FastAPI()
    rd.register(app, cfg, None)
    return TestClient(app), cfg


def test_download_zip_inhalt(tmp_path):
    c, cfg = app_mit_download(tmp_path, token="GEHEIM_123")
    r = c.get("/api/helfer/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="Labeltool-Helfer.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"helfer.bat", "helfer-autostart.bat", "LIESMICH.txt", "requirements-helfer.txt", "helfer.vorgabe.yaml",
            "labeltool/__init__.py", "labeltool/helfer.py", "labeltool/helfer_setup.py",
            "labeltool/ptouch_modelle.py", "labeltool/bpac.py", "labeltool/desktop.py", "labeltool/ptouch.py"} <= names
    for server_mod in ("app", "routes_einstellungen", "routes_helfer_download", "helfer_server", "partdb", "__main__"):
        assert f"labeltool/{server_mod}.py" not in names
    assert not any(n.endswith((".yaml",)) and n not in ("helfer.vorgabe.yaml",) for n in names)
    v = yaml.safe_load(z.read("helfer.vorgabe.yaml"))
    assert v == {"server": "http://testserver", "token": "GEHEIM_123"}
    req = z.read("requirements-helfer.txt").decode()
    assert "websockets" in req and "pyyaml" in req and "pywin32" in req and "fastapi" not in req
    bat = z.read("helfer.bat")
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")      # CRLF für cmd.exe
    assert b"labeltool.helfer_setup" in bat and b"requirements-helfer.txt" in bat
    assert z.read("labeltool/helfer.py") == (ROOT / "labeltool" / "helfer.py").read_bytes()
    assert b"http://testserver" in z.read("LIESMICH.txt")
    # keine fremden Server-Pakete in den Helfer-Modulen
    assert not rd.fremde_pakete() & set(rd.VERBOTEN)


def test_download_server_aus_x_forwarded(tmp_path):
    c, _ = app_mit_download(tmp_path, token="T")
    def server(headers):
        r = c.get("/api/helfer/download", headers=headers)
        return yaml.safe_load(zipfile.ZipFile(io.BytesIO(r.content)).read("helfer.vorgabe.yaml"))["server"]
    assert server({"Host": "unraid:8765"}) == "http://unraid:8765"
    assert server({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "labels.example.org, proxy"}) == \
        "https://labels.example.org"
    assert server({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "nas", "X-Forwarded-Port": "8443",
                   "X-Forwarded-Prefix": "/labeltool/"}) == "https://nas:8443/labeltool"
    assert server({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "nas", "X-Forwarded-Port": "443"}) == "https://nas"


def test_download_erzeugt_token_wenn_keiner_da(tmp_path):
    calls = []
    _, _, cfg = make(tmp_path)
    cfg.helper_token = ""
    app = FastAPI()
    rd.register(app, cfg, {"on_helper_token_changed": lambda: calls.append(1)})
    c = TestClient(app)
    tok = yaml.safe_load(zipfile.ZipFile(io.BytesIO(c.get("/api/helfer/download").content))
                         .read("helfer.vorgabe.yaml"))["token"]
    assert len(tok) >= 40 and cfg.helper_token == tok
    assert load_settings(cfg.settings_file)["helfer"]["token"] == tok
    assert load_config(tmp_path / "config.yaml").helper_token == tok      # übersteht Neustart
    assert not calls                                                       # niemand getrennt
    tok2 = yaml.safe_load(zipfile.ZipFile(io.BytesIO(c.get("/api/helfer/download").content))
                          .read("helfer.vorgabe.yaml"))["token"]
    assert tok2 == tok                                                     # bestehender bleibt


def test_download_ohne_bat_dateien_verstaendlicher_fehler(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PROJEKT_DIR", tmp_path)
    (tmp_path / "x").mkdir()
    c, _ = app_mit_download(tmp_path / "x", token="T")
    r = c.get("/api/helfer/download")
    assert r.status_code == 500 and "helfer.bat" in r.json()["detail"]


def test_modulgraph_folgt_importen(tmp_path):
    pkg = tmp_path / "labeltool"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '1'")
    (pkg / "helfer.py").write_text("from . import __version__, a\nfrom .b import x\ndef f():\n    from labeltool import c\n")
    (pkg / "helfer_setup.py").write_text("import yaml\n")
    (pkg / "ptouch_modelle.py").write_text("")
    (pkg / "a.py").write_text("import labeltool.d\n")
    (pkg / "b.py").write_text("x = 1\n")
    (pkg / "c.py").write_text("")
    (pkg / "d.py").write_text("import fastapi\n")
    (pkg / "server.py").write_text("")
    mods = rd.helfer_module(pkg)
    assert mods == ["__init__", "a", "b", "c", "d", "helfer", "helfer_setup", "ptouch_modelle"]
    assert "fastapi" in rd.fremde_pakete(pkg, mods)
    (tmp_path / "helfer.bat").write_text("a\nb\n"); (tmp_path / "helfer-autostart.bat").write_text("c")
    with pytest.raises(RuntimeError, match="fastapi"):
        rd.baue_zip("http://x", "t", pkg, tmp_path)


# ------------------------------------------------------------------ Installer-Download (Setup-Programm)
@pytest.fixture
def fake_setup(tmp_path, monkeypatch):
    """Fake-Setup über LABELTOOL_INSTALLER; Projekt-Fallback auf einen leeren Ordner umgelenkt."""
    monkeypatch.setattr(rd, "PROJEKT_DIR", tmp_path / "projekt")
    setup = tmp_path / "dist" / "Labeltool-Helfer-Setup.exe"
    setup.parent.mkdir()
    setup.write_bytes(b"MZ" + os.urandom(300_000))                 # nicht komprimierbar, > 1 Block
    monkeypatch.setenv(rd.INSTALLER_ENV, str(setup))
    return setup


def test_installer_zip_inhalt(tmp_path, fake_setup):
    c, _ = app_mit_download(tmp_path, token="GEHEIM_123")
    r = c.get("/api/helfer/installer")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="Labeltool-Helfer-Installer.zip"' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store" and int(r.headers["content-length"]) == len(r.content)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["LIESMICH.txt", "Labeltool-Helfer-Setup.exe", "helfer.vorgabe.yaml"]
    assert z.read("Labeltool-Helfer-Setup.exe") == fake_setup.read_bytes()
    assert z.getinfo("Labeltool-Helfer-Setup.exe").compress_type == zipfile.ZIP_STORED
    assert yaml.safe_load(z.read("helfer.vorgabe.yaml")) == {"server": "http://testserver", "token": "GEHEIM_123"}
    liesmich = z.read("LIESMICH.txt").decode("utf-8")
    assert "\r\n" in liesmich and "\n" not in liesmich.replace("\r\n", "")
    for text in ("http://testserver", "Weitere Informationen", "Trotzdem ausführen", "%LOCALAPPDATA%\Labeltool-Helfer\\",
                 "Deinstallieren", "Update"):
        assert text in liesmich
    # temporäre ZIP-Datei wird nach dem Senden gelöscht
    import tempfile
    assert not list(Path(tempfile.gettempdir()).glob("labeltool-installer-*.zip"))


def test_installer_vorgabe_aus_x_forwarded_und_token_erzeugt(tmp_path, fake_setup):
    c, cfg = app_mit_download(tmp_path, token="")
    r = c.get("/api/helfer/installer", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "nas",
                                                 "X-Forwarded-Port": "8443", "X-Forwarded-Prefix": "/labeltool"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    v = yaml.safe_load(z.read("helfer.vorgabe.yaml"))
    assert v["server"] == "https://nas:8443/labeltool"
    assert len(v["token"]) >= 40 and v["token"] == cfg.helper_token                # gleicher Weg wie /download
    assert load_settings(cfg.settings_file)["helfer"]["token"] == v["token"]
    assert "https://nas:8443/labeltool" in z.read("LIESMICH.txt").decode("utf-8")


def test_installer_fehlt_404_und_info(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PROJEKT_DIR", tmp_path / "projekt")
    monkeypatch.setattr(rd, "INSTALLER_STANDARD", tmp_path / "gibtsnicht" / rd.SETUP_NAME)
    monkeypatch.delenv(rd.INSTALLER_ENV, raising=False)
    c, cfg = app_mit_download(tmp_path, token="")
    r = c.get("/api/helfer/installer")
    assert r.status_code == 404 and "Installer" in r.json()["detail"] and "ohne Installation" in r.json()["detail"]
    assert c.get("/api/helfer/installer/info").json() == {"installer_verfuegbar": False}
    assert cfg.helper_token == ""                                                 # 404/Info erzeugen keinen Token
    # Env zeigt auf fehlende Datei -> kein Rückgriff auf andere Orte
    lokal = tmp_path / "projekt" / "installer" / "dist" / rd.SETUP_NAME
    lokal.parent.mkdir(parents=True)
    lokal.write_bytes(b"MZ")
    assert rd.installer_info()["installer_verfuegbar"]                            # lokaler Fallback ohne Env
    monkeypatch.setenv(rd.INSTALLER_ENV, str(tmp_path / "fehlt.exe"))
    assert c.get("/api/helfer/installer").status_code == 404
    assert not rd.installer_info()["installer_verfuegbar"]
    (tmp_path / "leer.exe").write_bytes(b"")
    monkeypatch.setenv(rd.INSTALLER_ENV, str(tmp_path / "leer.exe"))              # leere Datei zählt nicht
    assert not rd.installer_info()["installer_verfuegbar"]


def test_installer_info_verfuegbar(tmp_path, fake_setup):
    c, cfg = app_mit_download(tmp_path, token="")
    info = c.get("/api/helfer/installer/info").json()
    assert info["installer_verfuegbar"] is True and info["installer_groesse"] == fake_setup.stat().st_size
    assert info["installer_stand"][:4].isdigit() and str(fake_setup) not in json.dumps(info)
    assert rd.installer_info(cfg) == info and cfg.helper_token == ""


def test_installer_standardort_im_image(tmp_path, monkeypatch):
    monkeypatch.delenv(rd.INSTALLER_ENV, raising=False)
    monkeypatch.setattr(rd, "PROJEKT_DIR", tmp_path / "projekt")
    assert rd.INSTALLER_STANDARD.as_posix() == "/app/helfer-installer/Labeltool-Helfer-Setup.exe"
    std = tmp_path / "app" / rd.SETUP_NAME
    std.parent.mkdir()
    std.write_bytes(b"MZ123")
    monkeypatch.setattr(rd, "INSTALLER_STANDARD", std)
    assert rd.installer_pfad() == std
