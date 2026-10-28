"""Update per Knopf, Helfer-Seite: Auftrag „aktualisieren“ (Download mit Token, sha256, Start des Setups).

Der Download läuft gegen einen echten kleinen HTTP-Server (http.server im Thread); das Setup wird NIE
gestartet – ShellExecute ist durch eine Attrappe ersetzt (keine Administratorabfrage, keine Installation).
"""
import asyncio
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from labeltool import __version__, helfer
from labeltool.helfer import HelferConfig, HelferKern

SETUP = b"MZ" + b"\x00Labeltool-Helfer-Setup" * 5000          # ~115 KB „Setup“
SHA = hashlib.sha256(SETUP).hexdigest()
PFAD = "/api/helfer/installer/setup"


class FakeBpac:
    BpacError = Exception

    def status(self, printer):
        return {"ok": True, "grund": "", "drucker": []}

    def preview_available(self):
        return True


@pytest.fixture
def server():
    """HTTP-Server wie der Labeltool-Server: Setup nur mit Bearer-Token; /umleiten leitet um."""
    anfragen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            anfragen.append({"pfad": self.path, "auth": self.headers.get("Authorization")})
            if self.path == "/umleiten":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}{PFAD}")
                self.end_headers()
                return
            if self.path.rstrip("/").endswith(PFAD) or self.path == "/basis" + PFAD:
                if self.headers.get("Authorization") != "Bearer geheim":
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(SETUP)))
                self.send_header("X-Setup-Sha256", SHA)
                self.end_headers()
                self.wfile.write(SETUP)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.anfragen = anfragen
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def kern_fuer(tmp_path, server_url, token="geheim", installiert=True, pro_benutzer=False, starter=None):
    gestartet = []

    def fake_start(datei, parameter, erhoeht):
        gestartet.append({"datei": Path(datei), "parameter": parameter, "erhoeht": erhoeht,
                          "inhalt": Path(datei).read_bytes()})

    cfg = HelferConfig(server=server_url, token=token, cache_dir=tmp_path / "cache", csv_dir=tmp_path / "csv")
    kern = HelferKern(cfg, printer=FakeBpac(), state_fn=lambda n: {}, hostname="PC", installiert=installiert,
                      starter=starter or fake_start, update_dir=tmp_path / "update",
                      pro_benutzer=lambda: pro_benutzer)
    kern.gestartet = gestartet
    return kern


def auftrag(**kw):
    return dict({"typ": "aktualisieren", "id": "u1", "pfad": PFAD, "version": "1.2.3", "sha256": SHA}, **kw)


# ------------------------------------------------------------------ hallo/status
def test_info_meldet_installiert_und_version(tmp_path):
    info = kern_fuer(tmp_path, "http://x", installiert=True).info()
    assert info["installiert"] is True and info["version"] == __version__
    assert kern_fuer(tmp_path, "http://x", installiert=False).info()["installiert"] is False
    # ohne Angabe: gepackt (PyInstaller) = installiert; in den Tests läuft Python aus dem Projekt
    cfg = HelferConfig(server="http://x", token="t", cache_dir=tmp_path / "c")
    assert HelferKern(cfg, printer=FakeBpac(), state_fn=lambda n: {}, hostname="PC").info()["installiert"] is False


def test_server_basis():
    assert helfer.server_basis("http://unraid:8765/") == "http://unraid:8765"
    assert helfer.server_basis("unraid:8765") == "http://unraid:8765"
    assert helfer.server_basis("https://h/labeltool/") == "https://h/labeltool"
    assert helfer.server_basis("ws://h:1/api/helfer/ws") == "http://h:1"
    assert helfer.server_basis("wss://h/pre/api/helfer/ws") == "https://h/pre"
    with pytest.raises(helfer.HelferConfigError):
        helfer.server_basis("ftp://h")


# ------------------------------------------------------------------ Erfolg
def test_aktualisieren_erfolg(tmp_path, server):
    kern = kern_fuer(tmp_path, server.url + "/")
    antwort = kern.handle(auftrag())
    assert antwort["ok"] is True and antwort["id"] == "u1", antwort
    assert antwort["daten"]["version"] == "1.2.3"
    # Download über die eigene Server-Adresse + pfad, mit Token
    assert server.anfragen == [{"pfad": PFAD, "auth": "Bearer geheim"}]
    # geprüft gespeichert, Setup mit Rechteanhebung und den Update-Parametern gestartet
    (start,) = kern.gestartet
    assert start["inhalt"] == SETUP and start["datei"].parent == tmp_path / "update"
    assert start["datei"].name == "Labeltool-Helfer-Setup-1.2.3.exe"
    assert start["parameter"] == "/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE" and start["erhoeht"] is True
    assert kern.update_gestartet
    assert [p.name for p in (tmp_path / "update").iterdir()] == ["Labeltool-Helfer-Setup-1.2.3.exe"]   # kein .part
    # zweiter Auftrag: schon gestartet
    nochmal = kern.handle(auftrag(id="u2"))
    assert nochmal["ok"] is False and "bereits" in nochmal["fehler"]


def test_aktualisieren_mit_server_praefix(tmp_path, server):
    kern = kern_fuer(tmp_path, server.url + "/basis")
    assert kern.handle(auftrag())["ok"] is True
    assert server.anfragen[0]["pfad"] == "/basis" + PFAD


def test_aktualisieren_pro_benutzer_ohne_admin_und_eigene_konfig(tmp_path, server, monkeypatch):
    monkeypatch.setenv(helfer.ENV_ORDNER, str(tmp_path / "daten"))
    kern = kern_fuer(tmp_path, server.url, pro_benutzer=True)
    kern.konfig_pfad = tmp_path / "anders" / "helfer.yaml"
    assert kern.handle(auftrag(version="0.0.0-abc/../x"))["ok"] is True
    (start,) = kern.gestartet
    assert start["erhoeht"] is False and " /CURRENTUSER" in start["parameter"]
    assert f'"/KONFIG={(tmp_path / "anders" / "helfer.yaml").resolve()}"' in start["parameter"]
    assert start["datei"].parent == tmp_path / "update" and "/" not in start["datei"].name   # Version bereinigt
    # Standard-Konfiguration: kein /KONFIG
    kern2 = kern_fuer(tmp_path, server.url)
    kern2.konfig_pfad = tmp_path / "daten" / "helfer.yaml"
    assert kern2.handle(auftrag())["ok"] is True
    assert "/KONFIG" not in kern2.gestartet[0]["parameter"]


# ------------------------------------------------------------------ Fehler
def test_falsche_pruefsumme(tmp_path, server):
    kern = kern_fuer(tmp_path, server.url)
    antwort = kern.handle(auftrag(sha256="0" * 64))
    assert antwort["ok"] is False and "Prüfsumme" in antwort["fehler"]
    assert not kern.gestartet and not kern.update_gestartet
    assert list((tmp_path / "update").iterdir()) == []                       # nichts liegen gelassen


def test_ungueltige_pruefsumme_im_auftrag(tmp_path, server):
    kern = kern_fuer(tmp_path, server.url)
    antwort = kern.handle(auftrag(sha256="kaputt"))
    assert antwort["ok"] is False and "sha256" in antwort["fehler"] and not server.anfragen


@pytest.mark.parametrize("token, pfad, text", [
    ("falsch", PFAD, "Token abgelehnt"),
    ("geheim", "/api/helfer/installer/gibtsnicht", "nicht gefunden"),
    ("geheim", "/umleiten", "leitet um"),
])
def test_download_fehler(tmp_path, server, token, pfad, text):
    kern = kern_fuer(tmp_path, server.url, token=token)
    antwort = kern.handle(auftrag(pfad=pfad))
    assert antwort["ok"] is False and "Download" in antwort["fehler"] and text in antwort["fehler"], antwort
    assert not kern.gestartet and not kern.update_gestartet
    assert len(server.anfragen) == 1                                          # Umleitung nicht gefolgt


def test_server_nicht_erreichbar(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                                 # Port sicher frei
    kern = kern_fuer(tmp_path, f"http://127.0.0.1:{port}")
    antwort = kern.handle(auftrag())
    assert antwort["ok"] is False and "nicht erreichbar" in antwort["fehler"] and not kern.gestartet


@pytest.mark.parametrize("pfad", ["http://boese.example/setup", "//boese.example/x", "api/ohne/schraegstrich",
                                  "", "/a\\b"])
def test_fremde_adresse_bekommt_keinen_token(tmp_path, server, pfad):
    kern = kern_fuer(tmp_path, server.url)
    antwort = kern.handle(auftrag(pfad=pfad))
    assert antwort["ok"] is False and "Download-Pfad" in antwort["fehler"] and not server.anfragen


def test_uac_abgelehnt(tmp_path, server):
    def abgelehnt(datei, parameter, erhoeht):
        e = OSError("Der Vorgang wurde vom Benutzer abgebrochen.")
        e.winerror = helfer.ERROR_CANCELLED
        raise e

    kern = kern_fuer(tmp_path, server.url, starter=abgelehnt)
    antwort = kern.handle(auftrag())
    assert antwort["ok"] is False and "Administratorabfrage wurde abgelehnt" in antwort["fehler"]
    assert not kern.update_gestartet                                          # Helfer läuft weiter
    # danach ist ein neuer Versuch möglich
    kern.starter = lambda d, p, e: None
    assert kern.handle(auftrag(id="u2"))["ok"] is True and kern.update_gestartet


def test_setup_start_fehlgeschlagen(tmp_path, server):
    def kaputt(datei, parameter, erhoeht):
        e = OSError(2, "Das System kann die angegebene Datei nicht finden")
        e.winerror = 2
        raise e

    antwort = kern_fuer(tmp_path, server.url, starter=kaputt).handle(auftrag())
    assert antwort["ok"] is False and "nicht gestartet" in antwort["fehler"]


def test_nicht_installiert(tmp_path, server):
    kern = kern_fuer(tmp_path, server.url, installiert=False)
    antwort = kern.handle(auftrag())
    assert antwort["ok"] is False and "Installer" in antwort["fehler"] and "ZIP" in antwort["fehler"]
    assert not server.anfragen and not kern.gestartet


@pytest.mark.skipif(sys.platform != "win32", reason="ShellExecuteEx nur unter Windows")
def test_shellexecute_fehlende_datei(tmp_path):
    """Echter ShellExecuteEx-Aufruf ohne Rechteanhebung auf eine fehlende Datei: startet nichts."""
    with pytest.raises(OSError) as e:
        helfer.setup_ausfuehren(tmp_path / "gibt-es-nicht.exe", "/UPDATE", erhoeht=False)
    assert e.value.winerror in (2, 3)


def test_pro_benutzer_erkennung_ohne_eintrag(tmp_path):
    assert helfer.installation_pro_benutzer(tmp_path / "nirgends") is False


# ------------------------------------------------------------------ Verbindung: Antwort, dann beenden
class FakeWS:
    """Minimale WebSocket-Attrappe: liefert Nachrichten und endet, wenn close() gerufen wird."""

    def __init__(self, nachrichten):
        self.eingang: asyncio.Queue = asyncio.Queue()
        for n in nachrichten:
            self.eingang.put_nowait(json.dumps(n))
        self.gesendet, self.geschlossen = [], None

    async def send(self, text):
        self.gesendet.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.geschlossen = (code, reason)
        self.eingang.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.eingang.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


def test_session_beendet_sich_nach_update(tmp_path, server, monkeypatch):
    monkeypatch.setattr(helfer, "UPDATE_PAUSE", 0.01)
    kern = kern_fuer(tmp_path, server.url)
    ws = FakeWS([auftrag()])
    cfg = kern.cfg

    async def lauf():
        await asyncio.wait_for(helfer._session(ws, kern, cfg), 10)

    asyncio.run(lauf())
    assert ws.gesendet[0]["typ"] == "hallo" and ws.gesendet[0]["installiert"] is True
    antwort = next(m for m in ws.gesendet if m.get("typ") == "antwort")
    assert antwort["ok"] is True and ws.geschlossen == (1000, "Helfer wird aktualisiert")
    assert kern.update_gestartet


def test_session_laeuft_nach_fehler_weiter(tmp_path, server, monkeypatch):
    monkeypatch.setattr(helfer, "UPDATE_PAUSE", 0.01)
    kern = kern_fuer(tmp_path, server.url)
    ws = FakeWS([auftrag(sha256="1" * 64)])

    async def lauf():
        task = asyncio.create_task(helfer._session(ws, kern, kern.cfg))
        for _ in range(500):
            if any(m.get("typ") == "antwort" for m in ws.gesendet):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert ws.geschlossen is None and not task.done()                     # Verbindung bleibt
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(lauf())
    antwort = next(m for m in ws.gesendet if m.get("typ") == "antwort")
    assert antwort["ok"] is False and "Prüfsumme" in antwort["fehler"]


def test_run_forever_endet_nach_update(tmp_path, monkeypatch):
    """run_forever kehrt nach gestartetem Update mit True zurück (main beendet dann den Prozess)."""
    cfg_path = tmp_path / "helfer.yaml"
    cfg_path.write_text('server: "http://127.0.0.1:9"\ntoken: "t"\n', encoding="utf-8")

    class Kern:
        update_gestartet = False
        konfig_pfad = None

        def __init__(self, cfg):
            self.cfg = cfg

    class Verbindung:
        close_code, close_reason = 1000, ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    erzeugt = []

    def fabrik(cfg):
        k = Kern(cfg)
        erzeugt.append(k)
        return k

    async def fake_session(ws, kern, cfg):
        kern.update_gestartet = True

    import websockets.asyncio.client as wc
    monkeypatch.setattr(wc, "connect", lambda *a, **k: Verbindung())
    monkeypatch.setattr(helfer, "_session", fake_session)
    assert asyncio.run(asyncio.wait_for(helfer.run_forever(cfg_path, kern_factory=fabrik), 10)) is True
    assert len(erzeugt) == 1 and erzeugt[0].konfig_pfad == cfg_path
