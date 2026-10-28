"""Helfer-Update per Knopf – Server-Seite: Status-Feld "update", Setup-Download mit Helfer-Token und
Auftrag "aktualisieren" an einen Fake-Helfer (In-Process-WebSocket wie in test_helfer.py).

Es wird NIE ein echtes Setup gestartet: der Fake-Helfer lädt die Datei nur über den TestClient und
prüft die Prüfsumme.
"""
import hashlib
import threading
from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from labeltool import helfer_server
from labeltool import routes_helfer_download as rd
from test_helfer import setup as helfer_setup, status, wait_for

SETUP_BYTES = b"MZ" + b"fake-setup" * 5000


@pytest.fixture
def fake_setup(tmp_path, monkeypatch):
    d = tmp_path / "helfer-installer"
    d.mkdir()
    exe = d / rd.SETUP_NAME
    exe.write_bytes(SETUP_BYTES)
    (d / rd.VERSION_NAME).write_text("1.2.3\n", encoding="utf-8")
    monkeypatch.setenv(rd.INSTALLER_ENV, str(exe))
    return exe


@pytest.fixture
def kein_setup(tmp_path, monkeypatch):
    monkeypatch.setenv(rd.INSTALLER_ENV, str(tmp_path / "gibtsnicht.exe"))


class FakeUpdateHelper(threading.Thread):
    """Beantwortet "aktualisieren" wie der echte Helfer (Download + Prüfsumme, aber ohne Setup-Start);
    alles andere geht an HelferKern."""

    def __init__(self, ws, kern, client, token="geheim", antwort=None, ignore=()):
        super().__init__(daemon=True)
        self.ws, self.kern, self.client, self.token = ws, kern, client, token
        self.antwort, self.ignore = antwort, set(ignore)
        self.received, self.downloads, self.close_code = [], [], None

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
            if msg.get("typ") == "aktualisieren":
                self.ws.send_json(self.update(msg))
                continue
            answer = self.kern.handle(msg)
            if answer is not None:
                self.ws.send_json(answer)

    def update(self, msg):
        if self.antwort is not None:
            return dict(self.antwort, typ="antwort", id=msg["id"])
        r = self.client.get(msg["pfad"], headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code != 200:
            return {"typ": "antwort", "id": msg["id"], "ok": False, "fehler": f"Download: HTTP {r.status_code}"}
        self.downloads.append(r)
        if hashlib.sha256(r.content).hexdigest() != msg["sha256"]:
            return {"typ": "antwort", "id": msg["id"], "ok": False, "fehler": "Prüfsumme passt nicht"}
        return {"typ": "antwort", "id": msg["id"], "ok": True}


@contextmanager
def verbunden(c, kern, version="1.0.0", installiert=True, **kw):
    with c.websocket_connect("/api/helfer/ws") as ws:
        hallo = dict(kern.info(), typ="hallo", token="geheim", version=version)
        if installiert is not None:
            hallo["installiert"] = installiert
        ws.send_json(hallo)
        assert ws.receive_json()["typ"] == "willkommen"
        t = FakeUpdateHelper(ws, kern, c, **kw)
        t.start()
        yield t


def upd(c):
    return status(c)["helfer"]["update"]


KEYS = {"verfuegbar", "installiert", "angeboten", "grund", "moeglich"}


# ------------------------------------------------------------------ Versionen
def test_versionsvergleich():
    cv = helfer_server.compare_versions
    assert cv("1.2.3", "1.2.4") == -1 and cv("1.10.0", "1.9.9") == 1 and cv("2.0.0", "2.0.0") == 0
    assert cv("0.0.0-abc1234", "0.0.0-abc1234") == 0 and cv("0.0.0-abc1234", "0.0.0-def5678") is None
    assert cv("1.2.3", "0.0.0-dev") is None and cv(None, "1.2.3") is None and cv("1.2.3", None) is None


def test_setup_version_und_sha(tmp_path, fake_setup):
    assert rd.setup_version() == "1.2.3" and rd.setup_version(fake_setup) == "1.2.3"
    sha = rd.setup_sha256(fake_setup)
    assert sha == hashlib.sha256(SETUP_BYTES).hexdigest() and rd.setup_sha256(fake_setup) == sha   # Cache
    fake_setup.write_bytes(SETUP_BYTES + b"x")                       # Größe ändert sich -> neu berechnen
    assert rd.setup_sha256(fake_setup) == hashlib.sha256(SETUP_BYTES + b"x").hexdigest()
    (fake_setup.with_name(rd.VERSION_NAME)).write_text("kaputt mit leerzeichen\n", encoding="utf-8")
    assert rd.setup_version(fake_setup) is None
    fake_setup.with_name(rd.VERSION_NAME).unlink()
    assert rd.setup_version(fake_setup) is None
    assert rd.installer_info()["installer_version"] is None


# ------------------------------------------------------------------ Status
def test_status_lokal_und_nicht_verbunden(tmp_path, fake_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path, mode="lokal")
    u = upd(c)
    assert set(u) == KEYS and u["moeglich"] is False and u["verfuegbar"] is False and u["angeboten"] == "1.2.3"
    assert c.get("/api/status").json()["helfer"]["update"] == u
    cfg.print_mode = "helfer"
    u = upd(c)
    assert set(u) == KEYS and u["moeglich"] is False and u["installiert"] is None and "nicht verbunden" in u["grund"]


def test_status_verbunden(tmp_path, fake_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    with verbunden(c, kern, version="1.0.0"):
        u = upd(c)
        assert u == {"verfuegbar": True, "installiert": "1.0.0", "angeboten": "1.2.3", "moeglich": True,
                     "grund": u["grund"]} and "1.0.0 → verfügbar 1.2.3" in u["grund"]
        assert status(c)["helfer"]["version"] == "1.0.0"
    with verbunden(c, kern, version="1.2.3"):
        u = upd(c)
        assert u["verfuegbar"] is False and u["moeglich"] is True and "aktuell" in u["grund"]
    with verbunden(c, kern, version="2.0.0"):
        assert upd(c)["verfuegbar"] is False and "neuer" in upd(c)["grund"]
    with verbunden(c, kern, version="0.0.0-abc1234"):            # Entwicklungsstand: nur gleich/anders
        assert upd(c)["verfuegbar"] is True
    # Python-/ZIP-Helfer (meldet installiert: false bzw. gar nichts)
    for inst in (False, None):
        with verbunden(c, kern, version="1.0.0", installiert=inst):
            u = upd(c)
            assert u["moeglich"] is False and u["verfuegbar"] is True and "Installer verwenden" in u["grund"]
    # installiert-Flag kommt auch per status-Nachricht
    with verbunden(c, kern, version="1.0.0", installiert=False) as t:
        t.ws.send_json(dict(kern.info(), typ="status", version="1.0.0", installiert=True))
        assert wait_for(lambda: upd(c)["moeglich"] is True)


def test_status_ohne_setup_oder_version(tmp_path, fake_setup, monkeypatch):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    with verbunden(c, kern, version="1.0.0"):
        fake_setup.with_name(rd.VERSION_NAME).unlink()
        u = upd(c)
        assert u["angeboten"] is None and u["moeglich"] is True and u["verfuegbar"] is False
        assert "unbekannt" in u["grund"]
        monkeypatch.setenv(rd.INSTALLER_ENV, str(tmp_path / "weg.exe"))
        u = upd(c)
        assert u["moeglich"] is False and u["verfuegbar"] is False and u["grund"] == helfer_server.UPD_NO_SETUP


# ------------------------------------------------------------------ Setup-Download
def test_setup_download_nur_mit_token(tmp_path, fake_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    url = "/api/helfer/installer/setup"
    assert c.get(url).status_code == 401
    assert c.get(url, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert c.get(url, headers={"Authorization": "geheim"}).status_code == 401           # ohne "Bearer"
    assert c.get(url + "?token=geheim").status_code == 401
    r = c.get(url, headers={"Authorization": "Bearer geheim"})
    assert r.status_code == 200 and r.content == SETUP_BYTES
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-setup-version"] == "1.2.3"
    assert r.headers["x-setup-sha256"] == hashlib.sha256(SETUP_BYTES).hexdigest()
    cfg.helper_token = ""                                     # ohne Server-Token kommt niemand dran
    assert c.get(url, headers={"Authorization": "Bearer "}).status_code == 401
    assert c.get("/api/helfer/installer/info").json()["installer_version"] == "1.2.3"


def test_setup_download_ohne_setup(tmp_path, kein_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    assert c.get("/api/helfer/installer/setup").status_code == 401
    assert c.get("/api/helfer/installer/setup", headers={"Authorization": "Bearer geheim"}).status_code == 404


# ------------------------------------------------------------------ Auftrag "aktualisieren"
def test_aktualisieren(tmp_path, fake_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    with verbunden(c, kern, version="1.0.0") as t:
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 200, r.text
        assert r.json() == {"gestartet": True, "version": "1.2.3", "von": "1.0.0"}
        msg = [m for m in t.received if m["typ"] == "aktualisieren"][-1]
        assert msg["pfad"] == "/api/helfer/installer/setup" and msg["version"] == "1.2.3"
        assert msg["sha256"] == hashlib.sha256(SETUP_BYTES).hexdigest() and msg["id"]
        assert len(t.downloads) == 1 and t.downloads[0].content == SETUP_BYTES     # Helfer hat mit Token geladen


def test_aktualisieren_aktuell_und_erzwingen(tmp_path, fake_setup):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    for version in ("1.2.3", "9.0.0"):
        with verbunden(c, kern, version=version) as t:
            r = c.post("/api/helfer/aktualisieren", json={})
            assert r.status_code == 409 and "erzwingen" in r.json()["detail"]
            assert not [m for m in t.received if m["typ"] == "aktualisieren"]
            r = c.post("/api/helfer/aktualisieren", json={"erzwingen": True})
            assert r.status_code == 200 and r.json()["gestartet"] is True
    # Version des Setups unbekannt: Update trotzdem möglich
    fake_setup.with_name(rd.VERSION_NAME).unlink()
    with verbunden(c, kern, version="1.2.3") as t:
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 200 and r.json()["version"] is None
        assert [m for m in t.received if m["typ"] == "aktualisieren"][-1]["version"] is None


def test_aktualisieren_fehlerfaelle(tmp_path, fake_setup, monkeypatch):
    c, cfg, kern, _, _ = helfer_setup(tmp_path)
    r = c.post("/api/helfer/aktualisieren")                    # kein Helfer
    assert r.status_code == 409 and "nicht verbunden" in r.json()["detail"]

    with verbunden(c, kern, version="1.0.0", installiert=False) as t:     # Python-/ZIP-Helfer
        r = c.post("/api/helfer/aktualisieren", json={"erzwingen": True})
        assert r.status_code == 400 and "Installer verwenden" in r.json()["detail"]
        assert not [m for m in t.received if m["typ"] == "aktualisieren"]

    # Helfer meldet Fehler (z. B. Windows-Abfrage abgelehnt) -> 400 mit seiner Meldung
    with verbunden(c, kern, version="1.0.0",
                   antwort={"ok": False, "fehler": "Administratorabfrage abgelehnt (Fehler 1223)."}):
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 400 and "1223" in r.json()["detail"]

    # falscher Token beim Helfer -> Download 401 -> Fehler
    with verbunden(c, kern, version="1.0.0", token="falsch"):
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 400 and "401" in r.json()["detail"]

    # Helfer antwortet nicht
    monkeypatch.setattr(helfer_server, "TIMEOUT_UPDATE", 0.3)
    with verbunden(c, kern, version="1.0.0", ignore={"aktualisieren"}):
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 400 and "nicht rechtzeitig" in r.json()["detail"]

    # kein Setup auf dem Server
    monkeypatch.setenv(rd.INSTALLER_ENV, str(tmp_path / "weg.exe"))
    with verbunden(c, kern, version="1.0.0"):
        r = c.post("/api/helfer/aktualisieren")
        assert r.status_code == 400 and r.json()["detail"] == helfer_server.UPD_NO_SETUP


def test_timeout_grosszuegig():
    assert helfer_server.TIMEOUT_UPDATE >= 120
