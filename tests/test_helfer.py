"""Druck-Helfer: Server (WebSocket, Weiterleitung, Status) und Helfer-Logik im selben Prozess.

Der Helfer läuft als Thread über die WebSocket-Verbindung des Starlette-TestClients und führt
die Aufträge mit HelferKern gegen eine b-PAC-Attrappe aus – es wird NIE echt gedruckt.
"""
import base64
import shutil
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from labeltool import bpac, helfer_server
from labeltool.app import create_app
from labeltool.bpac import BpacError
from labeltool.helfer import (HelferConfig, HelferConfigError, HelferKern, PathError, PathMapper,
                              load_helper_config, ws_url)
from test_smoke import make, read_csv

PNG = b"\x89PNG\r\n\x1a\nhelfer"
DRUCKER = "Brother PT-P710BT"


class FakeBpac:
    """b-PAC-Attrappe auf dem Helfer-PC."""
    BpacError = BpacError

    def __init__(self):
        self.rendered, self.jobs = [], []

    def status(self, printer):
        return {"ok": True, "grund": "", "drucker": [DRUCKER], "hilfe_url": ""}

    def preview_available(self):
        return True

    def render_png(self, template, row):
        self.rendered.append((Path(template), row))
        return PNG

    def print_labels(self, template, rows, printer, flags):
        self.jobs.append((Path(template).name, rows, printer, flags))
        return len(rows)


def online(name):
    return {"name": name, "angeschlossen": True, "status_text": "bereit", "bereit": True}


def offline(name):
    return {"name": name, "angeschlossen": False, "status_text": "nicht verbunden oder ausgeschaltet", "bereit": False}


class HelperThread(threading.Thread):
    """Beantwortet Server-Aufträge wie der echte Helfer (ohne Netzwerkbibliothek)."""

    def __init__(self, ws, kern, ignore=()):
        super().__init__(daemon=True)
        self.ws, self.kern, self.ignore = ws, kern, set(ignore)
        self.received, self.close_code = [], None

    def run(self):
        while True:
            try:
                msg = self.ws.receive_json()
            except WebSocketDisconnect as e:
                self.close_code = e.code
                return
            except Exception:
                return
            self.received.append(msg)
            if msg.get("typ") in self.ignore:
                continue
            answer = self.kern.handle(msg)
            if answer is not None:
                self.ws.send_json(answer)


def setup(tmp_path, mode="helfer", pfade=None, state_fn=online):
    _, fake, cfg = make(tmp_path)
    cfg.print_mode = mode
    cfg.helper_token = "geheim"
    cfg.direct_print = True
    local_opened = []
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=local_opened.append))
    c.local_opened = local_opened
    if pfade is None:   # Server und "Windows" teilen sich hier die Ordner
        pfade = {str(cfg.output_dir): str(cfg.output_dir), str(cfg.template_dir): str(cfg.template_dir)}
    hcfg = HelferConfig(server="http://testserver", token="geheim", drucker=DRUCKER, pfade=pfade,
                        cache_dir=tmp_path / "helfer-cache", csv_dir=tmp_path / "helfer-csv")
    prn, opened = FakeBpac(), []
    kern = HelferKern(hcfg, printer=prn, opener=opened.append, state_fn=state_fn, hostname="WERKSTATT-PC")
    return c, cfg, kern, prn, opened


@contextmanager
def connected(c, kern, token="geheim", header=False, ignore=()):
    headers = {"Authorization": f"Bearer {token}"} if header else {}
    with c.websocket_connect("/api/helfer/ws", headers=headers) as ws:
        hallo = dict(kern.info(), typ="hallo")
        if not header:
            hallo["token"] = token
        ws.send_json(hallo)
        welcome = ws.receive_json()
        assert welcome["typ"] == "willkommen" and welcome["protokoll"] == helfer_server.PROTOKOLL
        t = HelperThread(ws, kern, ignore)
        t.start()
        yield t


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def status(c):
    return c.app.state.hooks.helper_status()


# ------------------------------------------------------------------ Anmeldung
def test_auth(tmp_path):
    c, cfg, kern, _, _ = setup(tmp_path)
    for token in ("falsch", ""):
        with c.websocket_connect("/api/helfer/ws") as ws:
            ws.send_json({"typ": "hallo", "token": token})
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401
    with c.websocket_connect("/api/helfer/ws") as ws:          # erst hallo, dann alles andere
        ws.send_json({"typ": "status"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4400
    assert not c.app.state.helper_hub.connected()

    # Token im Header genügt, hallo bringt dann nur die Infos
    with connected(c, kern, header=True):
        assert c.app.state.helper_hub.connected()

    # ohne eingerichteten Token kommt niemand rein
    cfg.helper_token = ""
    with c.websocket_connect("/api/helfer/ws") as ws:
        ws.send_json({"typ": "hallo", "token": ""})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401


def test_status_hallo_und_push(tmp_path):
    c, cfg, kern, _, _ = setup(tmp_path)
    s = status(c)
    assert s["helfer"]["modus"] == "helfer" and s["helfer"]["verbunden"] is False
    assert s["drucker"]["bereit"] is False and s["drucker"]["angeschlossen"] is None
    assert c.get("/api/print-status").json()["ok"] is False
    assert "nicht verbunden" in c.get("/api/print-status").json()["grund"]
    assert c.get("/api/meta").json()["bpac_vorschau"] is False

    with connected(c, kern) as t:
        s = status(c)
        assert s["helfer"] == {"modus": "helfer", "verbunden": True, "rechner": "WERKSTATT-PC", "seit": s["helfer"]["seit"],
                               "version": s["helfer"]["version"], "bpac": True, "drucker_liste": [DRUCKER],
                               "drucker_modell": None,
                               "vorlagen": {"ordner": None, "anzahl": 7, "abgeglichen": None, "fehler": None,
                                            "hinweis": helfer_server.NO_FOLDER, "ausstehend": []}}
        assert s["helfer"]["seit"] and s["drucker"] == {"name": DRUCKER, "angeschlossen": True,
                                                        "status_text": "bereit", "bereit": True}
        ps = c.get("/api/print-status").json()
        assert ps["ok"] is True and ps["aktiv"] is True and ps["drucker"] == [DRUCKER]
        assert c.get("/api/meta").json()["bpac_vorschau"] is True

        # Status-Push: Drucker ausgeschaltet
        kern.state_fn = offline
        t.ws.send_json(dict(kern.info(), typ="status"))
        assert wait_for(lambda: status(c)["drucker"]["angeschlossen"] is False)
        s = status(c)
        assert s["drucker"]["bereit"] is False and "nicht verbunden" in s["drucker"]["status_text"]
        ps = c.get("/api/print-status").json()
        assert ps["ok"] is False and "ausgeschaltet" in ps["grund"]

        cfg.direct_print = False   # Direktdruck aus -> nicht bereit, auch wenn alles verbunden ist
        kern.state_fn = online
        t.ws.send_json(dict(kern.info(), typ="status"))
        assert wait_for(lambda: status(c)["drucker"]["angeschlossen"] is True)
        assert status(c)["drucker"]["bereit"] is False
    assert wait_for(lambda: status(c)["helfer"]["verbunden"] is False)


def test_status_lokal(tmp_path, monkeypatch):
    monkeypatch.setattr(helfer_server, "printer_state", online)
    _, fake, cfg = make(tmp_path)
    cfg.direct_print = True
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), printer=FakeBpac()))
    s = status(c)
    assert s["helfer"]["modus"] == "lokal" and s["helfer"]["verbunden"] is True and s["helfer"]["bpac"] is True
    assert s["helfer"]["drucker_liste"] == [DRUCKER] and s["helfer"]["rechner"]
    assert s["drucker"] == {"name": DRUCKER, "angeschlossen": True, "status_text": "bereit", "bereit": True}
    hooks = c.app.state.hooks
    assert callable(hooks["helper_status"]) and callable(hooks.on_helper_token_changed)


# ------------------------------------------------------------------ Aufträge über den Helfer
def test_vorschau_drucken_oeffnen(tmp_path):
    c, cfg, kern, prn, opened = setup(tmp_path)
    with connected(c, kern) as t:
        # Vorschau: Vorlage wird beim ersten Mal mitgeschickt, danach aus dem Cache
        r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317", "Location": 7}})
        assert r.status_code == 200 and r.headers["X-Vorschau"] == "bpac" and r.content == PNG
        path, row = prn.rendered[-1]
        assert path.name == "PartDB_SMD15.lbx" and row == {"Section1": "LM317", "Location": "7"}
        assert path.is_relative_to(tmp_path / "helfer-cache")
        assert path.read_bytes() == (cfg.template_dir / "PartDB_SMD15.lbx").read_bytes()
        c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {}})
        previews = [m for m in t.received if m["typ"] == "vorschau"]
        assert "daten" in previews[0]["vorlage"] and "daten" not in previews[1]["vorlage"]
        # Cache weg -> Helfer meldet vorlage_fehlt, Server schickt sie erneut
        shutil.rmtree(tmp_path / "helfer-cache")
        r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {}})
        assert r.headers["X-Vorschau"] == "bpac" and len(prn.rendered) == 3
        previews = [m for m in t.received if m["typ"] == "vorschau"]
        assert "daten" not in previews[2]["vorlage"] and "daten" in previews[3]["vorlage"]

        # Direktdruck
        c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 2})
        r = c.post("/api/name-labels/print")
        assert r.status_code == 200, r.text
        assert r.json()["gedruckt"] == 2
        name, rows, printer, flags = prn.jobs[-1]
        assert name == "Layout_Lagerbox_PartDB.lbx" and rows[0]["Lagername"] == "MIC-2.0021"
        assert printer == DRUCKER and flags == 0x1 | 0x4000000

        # In P-touch öffnen: CSV-Inhalt geht mit, der Helfer legt CSV + verknüpfte Kopie lokal ab
        ex = c.post("/api/name-labels/export", json={"open_template": True}).json()
        assert "vorlage_fehler" not in ex, ex
        linked = Path(ex["vorlage"])
        local_csv = tmp_path / "helfer-csv" / Path(ex["datei"]).name
        assert linked.name == "Layout_Lagerbox_PartDB.lbx" and opened[-1] == linked
        assert linked.parent == tmp_path / "helfer-csv" / "Vorlagen"
        assert local_csv.read_bytes() == Path(ex["datei"]).read_bytes()
        xml = zipfile.ZipFile(linked).read("label.xml").decode("utf-8")
        assert f'databasePath="{local_csv.resolve()}"' in xml
        assert c.post("/api/templates/open", json={"box_type": "3U2x1_3"}).status_code == 400   # CSV fehlt noch

        # Bearbeiten: Original wird beim Helfer umgehängt und geöffnet, Backup macht der Server
        r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "worst"})
        assert r.status_code == 200, r.text
        d = r.json()
        tpl = cfg.template_dir / "PartDB_3U-3.lbx"
        assert opened[-1] == tpl and d["backup"]
        assert f'databasePath="{Path(d["csv"]).resolve()}"' in zipfile.ZipFile(tpl).read("label.xml").decode("utf-8")
        assert "-12.0123" in read_csv(d["csv"])[0]["Location"]

        # Ausgabeordner öffnen: CSVs gehen mit, der Helfer öffnet seinen lokalen CSV-Ordner
        assert c.post("/api/open-output").status_code == 200 and opened[-1] == tmp_path / "helfer-csv"
        assert {p.name for p in (tmp_path / "helfer-csv").glob("*.csv")} >= {p.name for p in cfg.output_dir.glob("*.csv")}
    assert c.local_opened == []   # im Helfer-Modus öffnet der Server nichts selbst


def test_pfad_nicht_zugeordnet(tmp_path):
    c, cfg, kern, _, opened = setup(tmp_path, pfade={"/irgendwo": r"Z:\Labeldaten"})
    with connected(c, kern):
        # Ordner öffnen braucht keine Pfade mehr (CSV-Inhalte), Bearbeiten ohne Vorlagen-Ordner schon
        assert c.post("/api/open-output").status_code == 200
        r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "worst"})
        assert r.status_code == 400 and "pfade" in r.json()["detail"]
    assert opened == [tmp_path / "helfer-csv"]


def test_drucker_aus_wird_nicht_gedruckt(tmp_path):
    c, cfg, kern, prn, _ = setup(tmp_path, state_fn=offline)
    with connected(c, kern):
        c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 1})
        r = c.post("/api/name-labels/print")
        assert r.status_code == 400 and "NICHT gesendet" in r.json()["detail"]
    assert prn.jobs == []


def test_helfer_weg_und_timeout(tmp_path, monkeypatch):
    c, cfg, kern, prn, _ = setup(tmp_path)
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 1})

    # ohne Helfer: Skizze statt Fehler, Druck/Öffnen mit klarer Meldung, CSV-Export klappt trotzdem
    r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317"}})
    assert r.status_code == 200 and r.headers["X-Vorschau"] == "skizze"
    r = c.post("/api/name-labels/print")
    assert r.status_code == 400 and "Druck-Helfer ist nicht verbunden" in r.json()["detail"]
    ex = c.post("/api/name-labels/export", json={"open_template": True})
    assert ex.status_code == 200 and "nicht verbunden" in ex.json()["vorlage_fehler"]
    assert c.post("/api/open-output").status_code == 400

    # Helfer verbunden, antwortet aber nicht
    monkeypatch.setattr(helfer_server, "TIMEOUT_PREVIEW", 0.3)
    monkeypatch.setattr(helfer_server, "TIMEOUT_PRINT", 0.3)
    with connected(c, kern, ignore={"vorschau", "drucken"}):
        r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317"}})
        assert r.status_code == 200 and r.headers["X-Vorschau"] == "skizze"
        r = c.post("/api/name-labels/print")
        assert r.status_code == 400 and "nicht rechtzeitig" in r.json()["detail"]
    assert prn.jobs == [] and prn.rendered == []


def test_tokenwechsel_und_verdraengen(tmp_path):
    c, cfg, kern, _, _ = setup(tmp_path)
    hub = c.app.state.helper_hub
    with connected(c, kern) as first:
        with connected(c, kern) as second:      # neuer Helfer verdrängt den alten
            assert wait_for(lambda: first.close_code is not None)
            assert first.close_code == helfer_server.CLOSE_REPLACED and hub.connected()
            cfg.helper_token = "neu"
            c.app.state.hooks.on_helper_token_changed()
            assert wait_for(lambda: second.close_code is not None)
            assert second.close_code == 4401 and not hub.connected()
            assert status(c)["helfer"]["verbunden"] is False
    # alter Token gilt nicht mehr
    with c.websocket_connect("/api/helfer/ws") as ws:
        ws.send_json({"typ": "hallo", "token": "geheim"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with connected(c, kern, token="neu"):
        assert hub.connected()


def test_modus_wechsel_wirkt_sofort(tmp_path):
    c, cfg, kern, prn, opened = setup(tmp_path)
    cfg.print_mode = "lokal"
    assert c.post("/api/open-output").status_code == 200 and c.local_opened == [cfg.output_dir]
    cfg.print_mode = "helfer"
    with connected(c, kern):
        assert c.post("/api/open-output").status_code == 200 and opened == [tmp_path / "helfer-csv"]
    assert c.local_opened == [cfg.output_dir]


# ------------------------------------------------------------------ Helfer-Bausteine
def test_pfad_uebersetzung():
    m = PathMapper({"/ausgabe": r"Z:\Labeldaten", "/ausgabe/Vorlagen/": r"Y:\Kopien", "/vorlagen": r"Z:\Etiketten",
                    r"C:\Server\Daten": r"D:\Daten"})
    win = lambda p: PureWindowsPath(str(p))
    assert win(m.translate("/ausgabe/Beispiele/PartDB_3U-3.csv")) == PureWindowsPath(r"Z:\Labeldaten\Beispiele\PartDB_3U-3.csv")
    assert win(m.translate("/ausgabe/Vorlagen/a.lbx")) == PureWindowsPath(r"Y:\Kopien\a.lbx")     # längster Präfix
    assert win(m.translate("/ausgabe")) == PureWindowsPath(r"Z:\Labeldaten")
    assert win(m.translate(r"c:\server\daten\x.csv")) == PureWindowsPath(r"D:\Daten\x.csv")     # Windows: Groß/klein egal
    for bad in ("/ausgabefoo/x.csv", "/Ausgabe/x.csv", "/etc/passwd"):
        with pytest.raises(PathError, match="nicht zugeordnet"):
            m.translate(bad)
    with pytest.raises(PathError, match=r"'\.\.'"):
        m.translate("/ausgabe/../../Windows/x")
    assert PathMapper({}).translate(r"C:\x\y.csv") == Path(r"C:\x\y.csv")     # ohne Zuordnung: unverändert


def test_helfer_konfig(tmp_path):
    assert ws_url("http://unraid:8765") == "ws://unraid:8765/api/helfer/ws"
    assert ws_url("https://label.example.de/tool/") == "wss://label.example.de/tool/api/helfer/ws"
    assert ws_url("unraid:8765") == "ws://unraid:8765/api/helfer/ws"
    assert ws_url("ws://unraid:8765/api/helfer/ws") == "ws://unraid:8765/api/helfer/ws"
    f = tmp_path / "helfer.yaml"
    f.write_text('server: "http://unraid:8765"\ntoken: "abc"\npfade:\n  /ausgabe: \'Z:\\Labeldaten\'\n', encoding="utf-8")
    cfg = load_helper_config(f)
    assert cfg.token == "abc" and cfg.pfade == {"/ausgabe": r"Z:\Labeldaten"} and cfg.cache_dir == tmp_path / ".helfer-cache"
    f.write_text('server: "http://unraid:8765"\n', encoding="utf-8")
    with pytest.raises(HelferConfigError, match="token"):
        load_helper_config(f)
    with pytest.raises(HelferConfigError, match="nicht gefunden"):
        load_helper_config(tmp_path / "fehlt.yaml")
    shutil.copy(Path(__file__).resolve().parents[1] / "helfer.example.yaml", f)
    with pytest.raises(HelferConfigError, match="token"):    # Beispiel hat noch keinen Token
        load_helper_config(f)


def test_kern_fehlerfaelle(tmp_path):
    hcfg = HelferConfig(server="x", token="t", cache_dir=tmp_path / "cache")
    prn = FakeBpac()
    kern = HelferKern(hcfg, printer=prn, opener=lambda p: None, state_fn=offline, hostname="PC")
    data = b"keine echte Vorlage"
    import hashlib
    tpl = {"name": "a.lbx", "sha256": hashlib.sha256(data).hexdigest()}
    assert kern.handle({"typ": "ping"}) == {"typ": "pong"}
    r = kern.handle({"typ": "vorschau", "id": "1", "vorlage": tpl, "zeile": {}})
    assert r["ok"] is False and r["code"] == "vorlage_fehlt"
    r = kern.handle({"typ": "vorschau", "id": "2", "vorlage": dict(tpl, daten=base64.b64encode(b"anders").decode()), "zeile": {}})
    assert r["ok"] is False and "Prüfsumme" in r["fehler"]
    r = kern.handle({"typ": "vorschau", "id": "3", "vorlage": dict(tpl, name="../a.lbx", daten=""), "zeile": {}})
    assert r["ok"] is False and "Ungültiger Vorlagenname" in r["fehler"]
    r = kern.handle({"typ": "drucken", "id": "4", "vorlage": dict(tpl, daten=base64.b64encode(data).decode()),
                     "zeilen": [{}], "drucker": "", "flags": 0})
    assert r["ok"] is False and "NICHT gesendet" in r["fehler"] and prn.jobs == []
    assert kern.handle({"typ": "gibtsnicht", "id": "5"})["ok"] is False
    info = kern.info()
    assert info["drucker_status"] == {DRUCKER: {"angeschlossen": False, "status_text": "nicht verbunden oder ausgeschaltet"}}


def test_lokaler_drucker_offline_druckt_nicht():
    prn = FakeBpac()
    local = helfer_server.LocalPrinter(module=prn, state_fn=offline)
    with pytest.raises(BpacError, match="NICHT gesendet"):
        local.print_labels(Path("x.lbx"), [{}], DRUCKER, 0)
    with pytest.raises(BpacError, match="NICHT gesendet"):      # auch mit "Drucker aus der Vorlage"
        local.print_labels(Path("x.lbx"), [{}], "", 0)
    st = local.status(DRUCKER)
    assert st["ok"] is False and st["angeschlossen"] is False and prn.jobs == []
    local = helfer_server.LocalPrinter(module=prn, state_fn=online)
    assert local.print_labels(Path("x.lbx"), [{}], DRUCKER, 0) == 1 and local.status("")["ok"] is True
    assert isinstance(helfer_server.HelperError("x"), bpac.BpacError)
