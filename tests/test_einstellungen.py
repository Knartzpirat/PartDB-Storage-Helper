"""Einstellungen (einstellungen.yaml), Part-DB-Verbindungstest und /api/status."""
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from labeltool import routes_einstellungen
from fastapi import FastAPI
from labeltool.config import load_config
from labeltool.einstellungen import normalize_url, token_hint, SettingsError
from labeltool.partdb import check_connection

from test_smoke import FakePartDB, make

GOOD = "tcp_1a2baaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa9c8d"


class FakeInfo:
    """Part-DB mit /api/tokens/current und /api/info; zählt Anfragen, kann Fehler spielen."""

    def __init__(self, tokens=("test", GOOD), fehler=None):
        self.tokens = set(tokens)
        self.fehler = fehler      # Exception, die beim nächsten Aufruf geworfen wird
        self.calls = []
        self.legacy = FakePartDB()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        self.calls.append((req.url.host, req.url.path))
        if self.fehler:
            raise self.fehler
        if req.url.host == "kein-partdb":
            return httpx.Response(200, text="<html>Router</html>")
        auth = req.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401, json={"title": "Unauthorized", "detail": "Invalid token"})
        if req.url.path == "/api/tokens/current":
            return httpx.Response(200, json={"name": "Storage Helper", "user": {"id": 2, "name": "Lasse"},
                                             "valid_until": "2027-10-27T17:42:00+00:00", "level": 2})
        if req.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.20.4", "title": "Part-DB"})
        if auth == "Bearer test":
            return self.legacy(req)
        return httpx.Response(404, json={"detail": "nicht gefunden"})


class Hooks:
    def __init__(self):
        self.token_changed = 0

    def helper_status(self):
        return {"helfer": {"modus": "helfer", "verbunden": True, "rechner": "PC"},
                "drucker": {"name": "PT-P710BT", "angeschlossen": True, "status_text": "Bereit", "bereit": True}}

    def on_helper_token_changed(self):
        self.token_changed += 1


def setup(tmp_path, hooks=None, fake=None):
    _, _, cfg = make(tmp_path)
    fake = fake or FakeInfo()
    t = httpx.MockTransport(fake)
    app = FastAPI()   # nur die Einstellungs-Routen, mit den Test-Hooks (create_app hängt eigene ein)
    routes_einstellungen.register(app, cfg, hooks, transport=t)
    return TestClient(app), fake, cfg


# ------------------------------------------------------------ Bausteine
def test_url_und_hinweis():
    assert normalize_url(" http://192.168.0.100:4444/api/ ") == "http://192.168.0.100:4444"
    assert normalize_url("https://pdb.example.org/") == "https://pdb.example.org"
    assert normalize_url("") == ""
    for bad in ("192.168.0.100:4444", "ftp://x", "http://", "http://h:99999"):
        with pytest.raises(SettingsError):
            normalize_url(bad)
    assert token_hint(GOOD) == "tcp_1a2b…9c8d" and token_hint("") == "" and token_hint("abc") == "ab…"


def test_verbindungstest_zustaende():
    t = httpx.MockTransport(FakeInfo())
    r = check_connection("http://pdb:4444", GOOD, transport=t)
    assert r["ok"] and r["status"] == "online" and r["version"] == "2.20.4" and r["benutzer"] == "Lasse"
    assert isinstance(r["latenz_ms"], int) and "2.20.4" in r["detail"]
    assert check_connection("http://pdb:4444", "falsch", transport=t)["status"] == "token_ungueltig"
    assert check_connection("", GOOD, transport=t)["status"] == "nicht_konfiguriert"
    assert check_connection("http://pdb", "", transport=t)["status"] == "nicht_konfiguriert"
    r = check_connection("http://kein-partdb", GOOD, transport=t)
    assert r["status"] == "offline" and "kein Part-DB" in r["detail"]

    cases = [
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "DNS"),
        (httpx.ConnectError("[WinError 10061] connection refused"), "abgelehnt"),
        (httpx.ConnectTimeout("timed out"), "Zeitüberschreitung"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "SSL prüfen"),
    ]
    for exc, text in cases:
        r = check_connection("http://pdb:4444", GOOD, transport=httpx.MockTransport(FakeInfo(fehler=exc)))
        assert r["status"] == "offline" and not r["ok"] and text in r["detail"], (exc, r)


# ------------------------------------------------------------ Routen
def test_einstellungen_speichern(tmp_path):
    c, fake, cfg = setup(tmp_path)
    g = c.get("/api/einstellungen").json()
    assert g["partdb"] == {"url": cfg.partdb_url, "token_gesetzt": True, "token_hinweis": "te…",
                           "ssl_pruefen": True}
    assert g["druck"]["modus"] == "lokal" and "halfcut" in g["druck"]["schnitt_optionen"]
    assert g["helfer"] == {"token_gesetzt": False, "token_hinweis": ""}

    # Test speichert nichts
    r = c.post("/api/einstellungen/partdb/test", json={"url": "http://neu:4444/api", "token": GOOD,
                                                       "ssl_pruefen": False}).json()
    assert r["ok"] and r["status"] == "online" and r["benutzer"] == "Lasse" and r["version"] == "2.20.4"
    assert cfg.partdb_token == "test" and not cfg.settings_file.exists()
    # token null = gespeicherten nehmen
    r = c.post("/api/einstellungen/partdb/test", json={"url": "http://neu:4444", "token": None}).json()
    assert r["ok"] and fake.calls[-1][0] == "neu"
    assert c.post("/api/einstellungen/partdb/test", json={"url": "neu:4444"}).status_code == 400

    # PUT mit fehlgeschlagenem Test speichert nicht
    r = c.put("/api/einstellungen/partdb", json={"url": "http://neu:4444", "token": "falsch", "ssl_pruefen": True})
    assert r.status_code == 400 and "Nicht gespeichert" in r.json()["detail"]
    assert cfg.partdb_token == "test" and not cfg.settings_file.exists()

    # erfolgreich: sofort wirksam, Token nur als Hinweis
    r = c.put("/api/einstellungen/partdb", json={"url": "http://neu:4444/api/", "token": GOOD, "ssl_pruefen": False})
    assert r.status_code == 200, r.text
    assert GOOD not in r.text and r.json()["partdb"]["token_hinweis"] == "tcp_1a2b…9c8d"
    assert (cfg.partdb_url, cfg.partdb_token, cfg.verify_ssl) == ("http://neu:4444", GOOD, False)
    assert GOOD not in c.get("/api/einstellungen").text

    # Leerer Token behält den gespeicherten; ohne_test speichert auch offline
    r = c.put("/api/einstellungen/partdb", json={"url": "http://anders:1", "token": "", "ohne_test": True})
    assert r.status_code == 200 and cfg.partdb_token == GOOD and cfg.partdb_url == "http://anders:1"
    assert c.put("/api/einstellungen/partdb", json={"url": "", "ohne_test": True}).status_code == 400

    # Druck
    assert c.put("/api/einstellungen/druck", json={"modus": "fax"}).status_code == 400
    assert c.put("/api/einstellungen/druck", json={"modus": "helfer", "schnitt": ["laser"]}).status_code == 400
    r = c.put("/api/einstellungen/druck", json={"modus": "helfer", "direktdruck": True, "drucker": " PT-P710BT ",
                                               "schnitt": ["HalfCut", "cutatend"]}).json()
    assert r["druck"]["modus"] == "helfer" and r["druck"]["schnitt"] == ["halfcut", "cutatend"]
    assert (cfg.print_mode, cfg.direct_print, cfg.printer) == ("helfer", True, "PT-P710BT")

    # Neuladen: einstellungen.yaml überlagert config.yaml, config.yaml bleibt unverändert
    assert "anders" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    saved = yaml.safe_load(cfg.settings_file.read_text(encoding="utf-8"))
    assert set(saved) == {"partdb", "druck"}
    cfg2 = load_config(tmp_path / "config.yaml")
    assert (cfg2.partdb_url, cfg2.partdb_token, cfg2.verify_ssl) == ("http://anders:1", GOOD, True)
    assert (cfg2.print_mode, cfg2.printer, cfg2.cut_options) == ("helfer", "PT-P710BT", ["halfcut", "cutatend"])


def test_helfer_token(tmp_path):
    hooks = Hooks()
    c, _, cfg = setup(tmp_path, hooks)
    r = c.post("/api/einstellungen/helfer/token").json()
    tok = r["token"]
    assert len(tok) >= 40 and cfg.helper_token == tok and hooks.token_changed == 1
    assert r["helfer"] == {"token_gesetzt": True, "token_hinweis": token_hint(tok)}
    assert tok not in c.get("/api/einstellungen").text
    r2 = c.post("/api/einstellungen/helfer/token").json()
    assert r2["token"] != tok and hooks.token_changed == 2
    assert load_config(tmp_path / "config.yaml").helper_token == r2["token"]
    # Hooks als Dict gehen auch
    (tmp_path / "b").mkdir()
    c3, _, _ = setup(tmp_path / "b", {"on_helper_token_changed": hooks.on_helper_token_changed})
    c3.post("/api/einstellungen/helfer/token")
    assert hooks.token_changed == 3


def test_status_cache(tmp_path):
    hooks = Hooks()
    c, fake, cfg = setup(tmp_path, hooks)
    s = c.get("/api/status").json()
    assert s["partdb"]["status"] == "online" and s["partdb"]["benutzer"] == "Lasse"
    assert s["partdb"]["url"] == cfg.partdb_url and s["partdb"]["geprueft"]
    assert s["helfer"]["verbunden"] is True and s["drucker"]["bereit"] is True
    n = len(fake.calls)
    c.get("/api/status")
    assert len(fake.calls) == n                      # aus dem Cache
    c.get("/api/status", params={"neu": 1})
    assert len(fake.calls) > n                       # erzwungen

    # Speichern leert den Cache, Fehler werden sichtbar
    c.put("/api/einstellungen/partdb", json={"url": "http://weg:1", "ohne_test": True})
    fake.fehler = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    s = c.get("/api/status").json()["partdb"]
    assert s["status"] == "offline" and "weg" in s["detail"] and s["url"] == "http://weg:1"


def test_status_ohne_hooks(tmp_path):
    c, fake, cfg = setup(tmp_path)
    cfg.partdb_token = ""
    s = c.get("/api/status").json()
    assert s["partdb"]["status"] == "nicht_konfiguriert" and fake.calls == []
    assert s["helfer"] == {"modus": "lokal", "verbunden": False} and s["drucker"] == {"angeschlossen": None}


def test_env_startwerte(tmp_path, monkeypatch):
    f = tmp_path / "config.yaml"
    f.write_text('partdb:\n  url: ""\n  token: ""\nhelfer:\n  token: "h1"\ndruck:\n  modus: helfer\n',
                 encoding="utf-8")
    monkeypatch.setenv("PARTDB_URL", "http://env:4444/")
    monkeypatch.setenv("PARTDB_TOKEN", "envtoken")
    cfg = load_config(f)
    assert (cfg.partdb_url, cfg.partdb_token, cfg.helper_token, cfg.print_mode) == \
        ("http://env:4444", "envtoken", "h1", "helfer")
    assert cfg.settings_file == tmp_path / "einstellungen.yaml"
    # config.yaml schlägt Umgebungsvariable
    f.write_text('partdb:\n  url: "http://cfg:1"\n  token: "cfgtoken"\n', encoding="utf-8")
    cfg = load_config(f)
    assert (cfg.partdb_url, cfg.partdb_token, cfg.print_mode, cfg.helper_token) == \
        ("http://cfg:1", "cfgtoken", "lokal", "")
