"""Vorlagen-Abgleich: PC-Ordner des Helfers ist die Quelle der Vorlagen (Modus helfer).

Server-App (TestClient) und Helfer-Logik (HelferKern) laufen im selben Prozess; der Helfer
beantwortet Aufträge in eigenen Threads gegen einen tmp-„PC-Ordner“ mit b-PAC- und
Opener-Attrappen. Es wird NIE gedruckt und nichts wirklich geöffnet.
"""
import base64
import hashlib
import os
import shutil
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from labeltool import helfer_server
from labeltool.app import create_app
from labeltool.desktop import relink_in_place, valid_name
from labeltool.helfer import HelferConfig, HelferKern, load_helper_config, printer_for_model
from test_helfer import DRUCKER, PNG, FakeBpac, online, wait_for
from test_smoke import VORLAGEN, make, read_csv

ALT = time.time() - 3600     # mtime „längst fertig gespeichert“


def sha(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    return hashlib.sha256(data).hexdigest()


def old(path):
    os.utime(path, (ALT, ALT))


class ThreadedHelper(threading.Thread):
    """Wie der echte Helfer: jeder Auftrag in eigenem Thread (Abgleich blockiert die Vorschau nicht)."""

    def __init__(self, ws, kern):
        super().__init__(daemon=True)
        self.ws, self.kern = ws, kern
        self.received, self.close_code = [], None
        self.lock = threading.Lock()

    def send(self, msg):
        with self.lock:
            self.ws.send_json(msg)

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
            threading.Thread(target=self._answer, args=(msg,), daemon=True).start()

    def _answer(self, msg):
        answer = self.kern.handle(msg)
        if answer is not None:
            try:
                self.send(answer)
            except Exception:
                pass

    def jobs(self, typ):
        return [m for m in self.received if m.get("typ") == typ]


def setup(tmp_path, pc="kopie"):
    """pc: "kopie" = Kopie der Testvorlagen, "leer" = leerer Ordner, None = kein Vorlagen-Ordner."""
    _, fake, cfg = make(tmp_path)
    cfg.print_mode, cfg.helper_token, cfg.direct_print = "helfer", "geheim", True
    local_opened = []
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=local_opened.append))
    c.local_opened = local_opened
    folder = None
    if pc == "kopie":
        folder = tmp_path / "PC"
        shutil.copytree(VORLAGEN, folder)
        for p in folder.glob("*.lbx"):
            old(p)
    elif pc == "leer":
        folder = tmp_path / "PC"
        folder.mkdir()
    hcfg = HelferConfig(server="http://testserver", token="geheim", drucker_modell="PT-P710BT",
                        cache_dir=tmp_path / "helfer-cache", csv_dir=tmp_path / "helfer-csv",
                        vorlagen_ordner=folder)
    opened = []
    kern = HelferKern(hcfg, printer=FakeBpac(), opener=opened.append, state_fn=online, hostname="WERKSTATT-PC")
    return c, cfg, kern, opened


@contextmanager
def connected(c, kern):
    with c.websocket_connect("/api/helfer/ws") as ws:
        ws.send_json(dict(kern.info(), typ="hallo", token="geheim"))
        assert ws.receive_json()["typ"] == "willkommen"
        t = ThreadedHelper(ws, kern)
        t.start()
        yield t


def sync(c, t, kern, manifest=None):
    """Manifest schicken und warten, bis der Server es abgeglichen hat."""
    hub = c.app.state.helper_hub
    before = hub.sync.state["runden"]
    t.send(manifest or kern.manifest())
    assert wait_for(lambda: hub.sync.state["runden"] > before, 10), "Abgleich lief nicht"
    return c.app.state.hooks.helper_status()["helfer"]["vorlagen"]


def server_files(cfg):
    return {p.name: sha(p) for p in cfg.template_dir.glob("*.lbx")}


def pc_files(folder):
    return {p.name: sha(p) for p in folder.glob("*.lbx") if valid_name(p.name)}


# ------------------------------------------------------------------ Abgleich PC -> Server
def test_erstabgleich_aenderung_loeschung(tmp_path):
    c, cfg, kern, _ = setup(tmp_path)
    pc = kern.folder
    # PC weicht ab: eine geändert, eine gelöscht, eine neu; Beiwerk wird ignoriert
    relink_in_place(pc / "PartDB_3U-3.lbx", tmp_path / "pc.csv")
    old(pc / "PartDB_3U-3.lbx")
    (pc / "PartDB_SMD15.lbx").unlink()
    shutil.copy2(pc / "PartDB_Window-Div-Bin-1.lbx", pc / "Neu.lbx")
    old(pc / "Neu.lbx")
    (pc / "notiz.txt").write_text("x")
    (pc / ".backup").mkdir()
    shutil.copy2(pc / "Neu.lbx", pc / ".backup" / "Neu.20250101-000000.lbx")
    shutil.copy2(pc / "Neu.lbx", pc / ".versteckt.lbx")
    (pc / "Unterordner").mkdir()
    shutil.copy2(pc / "Neu.lbx", pc / "Unterordner" / "tief.lbx")

    with connected(c, kern) as t:
        v = sync(c, t, kern)
        assert v == {"ordner": str(pc), "anzahl": 7, "abgeglichen": v["abgeglichen"], "fehler": None, "hinweis": None,
                     "ausstehend": []}
        assert v["abgeglichen"]
        assert server_files(cfg) == pc_files(pc)
        assert {m["name"] for m in t.jobs("datei")} == {"PartDB_3U-3.lbx", "Neu.lbx"}   # nur was fehlt/abweicht
        gone = cfg.template_dir / ".geloescht" / "PartDB_SMD15.lbx"
        assert gone.is_file() and sha(gone) == sha(VORLAGEN / "PartDB_SMD15.lbx")
        assert not (cfg.template_dir / "tief.lbx").exists() and not (cfg.template_dir / ".versteckt.lbx").exists()
        assert not list(cfg.template_dir.glob(".*.tmp"))

        # gleiches Manifest noch mal: nichts zu tun (keine Schleife Server -> Helfer -> Server)
        n = len(t.received)
        sync(c, t, kern)
        sync(c, t, kern)
        assert len(t.received) == n and server_files(cfg) == pc_files(pc)

        # Vorlagen-Übersicht nennt die Quelle, Status das Druckermodell
        q = c.get("/api/vorlagen").json()["quelle"]
        assert q["modus"] == "helfer" and q["ordner"] == str(pc) and q["rechner"] == "WERKSTATT-PC"
        assert c.app.state.hooks.helper_status()["helfer"]["drucker_modell"] == "PT-P710BT"

        # erneut gelöscht und wieder angelegt: zweite Fassung in .geloescht bekommt einen Zeitstempel
        shutil.copy2(VORLAGEN / "PartDB_SMD15.lbx", pc / "PartDB_SMD15.lbx")
        old(pc / "PartDB_SMD15.lbx")
        sync(c, t, kern)
        assert (cfg.template_dir / "PartDB_SMD15.lbx").is_file()
        (pc / "PartDB_SMD15.lbx").unlink()
        sync(c, t, kern)
        assert len(list((cfg.template_dir / ".geloescht").glob("PartDB_SMD15*.lbx"))) == 2

    # getrennt: letzter Abgleich bleibt im Status sichtbar
    assert wait_for(lambda: not c.app.state.helper_hub.connected())
    v = c.app.state.hooks.helper_status()["helfer"]["vorlagen"]
    assert v["ordner"] == str(pc) and v["anzahl"] == 7


def test_halb_geschrieben_gesperrt_zu_gross(tmp_path, monkeypatch):
    c, cfg, kern, _ = setup(tmp_path)
    pc = kern.folder
    orig = sha(cfg.template_dir / "PartDB_3U-3.lbx")
    with connected(c, kern) as t:
        sync(c, t, kern)
        # gerade gespeichert (mtime frisch): nicht abgleichen, aber auch NICHT als gelöscht werten
        relink_in_place(pc / "PartDB_3U-3.lbx", tmp_path / "neu.csv")
        m = kern.manifest()
        assert "PartDB_3U-3.lbx" not in m["dateien"] and "gerade gespeichert" in m["uebersprungen"]["PartDB_3U-3.lbx"]
        v = sync(c, t, kern, m)
        assert sha(cfg.template_dir / "PartDB_3U-3.lbx") == orig and v["hinweis"] is None
        assert kern.handle({"typ": "datei", "id": "x", "name": "PartDB_3U-3.lbx"})["code"] == "unfertig"

        # halb geschrieben (kein vollständiges ZIP), aber schon alt -> übersprungen mit Grund
        data = (pc / "PartDB_3U-3.lbx").read_bytes()
        (pc / "PartDB_3U-3.lbx").write_bytes(data[: len(data) // 2])
        old(pc / "PartDB_3U-3.lbx")
        v = sync(c, t, kern)
        assert sha(cfg.template_dir / "PartDB_3U-3.lbx") == orig
        assert "PartDB_3U-3.lbx" in v["hinweis"] and "keine gültige" in v["hinweis"]

        # fertig gespeichert -> kommt an
        (pc / "PartDB_3U-3.lbx").write_bytes(data)
        old(pc / "PartDB_3U-3.lbx")
        v = sync(c, t, kern)
        assert sha(cfg.template_dir / "PartDB_3U-3.lbx") == sha(data) != orig and v["hinweis"] is None

        # zu groß: bleibt beim alten Stand, Hinweis
        big = pc / "Riesig.lbx"
        with open(big, "wb") as f:
            f.truncate(10 * 1024 * 1024 + 1)
        old(big)
        v = sync(c, t, kern)
        assert not (cfg.template_dir / "Riesig.lbx").exists() and "zu groß" in v["hinweis"]
        big.unlink()

        # Server-Datei gesperrt (Windows: im Editor offen) -> Fehler im Status, nächster Abgleich klappt
        relink_in_place(pc / "PartDB_SMD15.lbx", tmp_path / "x.csv")
        old(pc / "PartDB_SMD15.lbx")
        real_replace = Path.replace

        def locked(self, target):
            if Path(target).name == "PartDB_SMD15.lbx":
                raise PermissionError("gesperrt")
            return real_replace(self, target)
        monkeypatch.setattr(Path, "replace", locked)
        v = sync(c, t, kern)
        assert "PartDB_SMD15.lbx" in v["fehler"] and "geöffnet" in v["fehler"]
        monkeypatch.setattr(Path, "replace", real_replace)
        v = sync(c, t, kern)
        assert v["fehler"] is None and server_files(cfg) == pc_files(pc)

        # Ordner weg (z. B. Netzlaufwerk getrennt): Fehler melden, NICHTS verschieben
        shutil.move(pc, tmp_path / "PC-weg")
        v = sync(c, t, kern)
        assert "nicht lesbar" in v["fehler"] and len(server_files(cfg)) == 7
        assert not (cfg.template_dir / ".geloescht").exists()


def test_abgleich_blockiert_vorschau_nicht(tmp_path):
    c, cfg, kern, _ = setup(tmp_path)
    slow = threading.Event()
    real = kern._job_datei

    def langsam(msg):
        slow.set()
        time.sleep(2.0)
        return real(msg)
    kern._job_datei = langsam
    relink_in_place(kern.folder / "PartDB_3U-3.lbx", tmp_path / "x.csv")
    old(kern.folder / "PartDB_3U-3.lbx")
    with connected(c, kern) as t:
        before = c.app.state.helper_hub.sync.state["runden"]
        t.send(kern.manifest())
        assert slow.wait(5)
        start = time.monotonic()
        r = c.post("/api/vorlagen/PartDB_SMD15.lbx/vorschau", json={"zeile": {"Section1": "LM317"}})
        assert r.headers["X-Vorschau"] == "bpac" and r.content == PNG
        assert time.monotonic() - start < 1.5
        assert wait_for(lambda: c.app.state.helper_hub.sync.state["runden"] > before, 10)


def test_leerer_pc_ordner_wird_befuellt(tmp_path):
    c, cfg, kern, _ = setup(tmp_path, pc="leer")
    with connected(c, kern) as t:
        sync(c, t, kern)
        assert pc_files(kern.folder) == server_files(cfg) and len(server_files(cfg)) == 7
        assert not (cfg.template_dir / ".geloescht").exists()
        v = sync(c, t, kern)       # danach normaler Abgleich ohne Übertragungen
        assert v["anzahl"] == 7 and v["fehler"] is None and len(t.jobs("datei")) == 0
    # Installation ist jetzt bekannt: wird der Ordner später leer, wird NICHT neu befüllt,
    # sondern greift der Massen-Löschschutz
    for p in kern.folder.glob("*.lbx"):
        p.unlink()
    with connected(c, kern) as t:
        v = sync(c, t, kern)
        assert "7 von 7 Vorlagen fehlen" in v["fehler"] and len(v["ausstehend"]) == 7
        assert len(server_files(cfg)) == 7 and t.jobs("vorlage_speichern") == []


def test_massen_loeschschutz_und_bestaetigung(tmp_path):
    c, cfg, kern, _ = setup(tmp_path)
    pc = kern.folder
    with connected(c, kern) as t:
        sync(c, t, kern)
        assert c.post("/api/helfer/vorlagen/loeschen-bestaetigen").status_code == 400   # nichts offen
        for name in ("PartDB_SMD15.lbx", "PartDB_3U-3.lbx", "PartDB_Window-Div-Bin-1.lbx", "PartDB_Window-Div-Bin-2.lbx"):
            (pc / name).unlink()
        v = sync(c, t, kern)            # 4 von 7 fehlen -> nichts verschieben, melden
        assert "4 von 7 Vorlagen fehlen" in v["fehler"] and "Ordner richtig gewählt" in v["fehler"]
        assert len(v["ausstehend"]) == 4 and len(server_files(cfg)) == 7
        assert not (cfg.template_dir / ".geloescht").exists()
        v = sync(c, t, kern)            # bleibt so, bis bestätigt
        assert len(server_files(cfg)) == 7
        hub = c.app.state.helper_hub
        before = hub.sync.state["runden"]
        r = c.post("/api/helfer/vorlagen/loeschen-bestaetigen")
        assert r.status_code == 200 and len(r.json()["bestaetigt"]) == 4 and r.json()["abgleich_gestartet"]
        assert wait_for(lambda: hub.sync.state["runden"] > before, 10)
        v = c.app.state.hooks.helper_status()["helfer"]["vorlagen"]
        assert v["fehler"] is None and v["ausstehend"] == [] and len(server_files(cfg)) == 3
        assert len(list((cfg.template_dir / ".geloescht").glob("*.lbx"))) == 4
        # einzelne Löschung (1 von 3) ist plausibel -> sofort
        (pc / "PartDB_Window-Div-Bin-3.lbx").unlink()
        v = sync(c, t, kern)
        assert v["fehler"] is None and len(server_files(cfg)) == 2


def test_token_pruefen(tmp_path):
    c, cfg, kern, _ = setup(tmp_path)
    with connected(c, kern):
        assert c.post("/api/helfer/token-pruefen", json={"token": "geheim"}).json() == {"ok": True, "grund": ""}
        r = c.post("/api/helfer/token-pruefen", json={"token": "falsch"}).json()
        assert r["ok"] is False and "ungültig" in r["grund"]
        assert c.post("/api/helfer/token-pruefen", json={}).json()["ok"] is False
        assert c.app.state.helper_hub.connected()          # laufender Helfer bleibt verbunden
    cfg.helper_token = ""
    r = c.post("/api/helfer/token-pruefen", json={"token": ""}).json()
    assert r["ok"] is False and "kein Helfer-Token" in r["grund"]


# ------------------------------------------------------------------ Server -> PC
def test_kopie_geht_zum_pc(tmp_path):
    c, cfg, kern, opened = setup(tmp_path)
    pc = kern.folder
    with connected(c, kern) as t:
        sync(c, t, kern)
        stale = kern.manifest()      # Manifest von VOR der Kopie (kommt evtl. verspätet)
        r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_Window-Div-Bin-3.lbx",
                                                "name": "PartDB_Window-Div-Bin-4.lbx", "lagerart": "WinBin4"})
        assert r.status_code == 200, r.text
        d = r.json()
        new = pc / "PartDB_Window-Div-Bin-4.lbx"
        assert new.is_file() and opened == [new] and d["vorlage"] == str(new)
        assert d["csv"] == str(tmp_path / "helfer-csv" / "PartDB_Window-Div-Bin-4.csv")
        assert read_csv(d["csv"])[0]["Section4"]
        assert f'databasePath="{Path(d["csv"]).resolve()}"' in zipfile.ZipFile(new).read("label.xml").decode("utf-8")
        assert cfg.box_types["WinBin4"].lbx == new.name
        # verspätetes altes Manifest: neue Datei wird NICHT als gelöscht gewertet
        sync(c, t, kern, stale)
        assert (cfg.template_dir / new.name).is_file()
        # das Bearbeiten (Umhängen) im PC-Ordner kommt per Abgleich zum Server – sofort, eigene Änderung
        assert "PartDB_Window-Div-Bin-4.lbx" in kern.manifest()["dateien"]
        sync(c, t, kern)
        assert sha(cfg.template_dir / new.name) == sha(new)
        # Name gibt es auf dem PC schon (Server weiß es noch nicht) -> 409/400, Server-Kopie wieder weg
        shutil.copy2(VORLAGEN / "PartDB_SMD15.lbx", pc / "Konflikt.lbx")
        r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_3U-3.lbx", "name": "Konflikt.lbx"})
        assert r.status_code == 400 and "gibt es im Vorlagen-Ordner schon" in r.json()["detail"]
        assert not (cfg.template_dir / "Konflikt.lbx").exists()
    # ohne Helfer: Kopie scheitert verständlich, nichts bleibt liegen
    assert wait_for(lambda: not c.app.state.helper_hub.connected())
    r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "Offline.lbx"})
    assert r.status_code == 400 and "nicht verbunden" in r.json()["detail"]
    assert not (cfg.template_dir / "Offline.lbx").exists()


def test_bearbeiten_im_pc_ordner(tmp_path):
    c, cfg, kern, opened = setup(tmp_path)
    pc = kern.folder
    server_tpl = cfg.template_dir / "PartDB_3U-3.lbx"
    before = sha(server_tpl)
    with connected(c, kern) as t:
        sync(c, t, kern)
        r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "box", "box_id": 250})
        assert r.status_code == 200, r.text
        d = r.json()
        tpl = pc / "PartDB_3U-3.lbx"
        local_csv = tmp_path / "helfer-csv" / "PartDB_3U-3.csv"
        assert d == {"vorlage": str(tpl), "csv": str(local_csv), "backup": d["backup"]}
        assert opened == [tpl]
        # Backup im PC-Ordner, keins auf dem Server; Server-Datei unverändert bis zum Abgleich
        assert Path(d["backup"]).parent == pc / ".backup" and sha(d["backup"]) == sha(VORLAGEN / "PartDB_3U-3.lbx")
        assert not (cfg.template_dir / ".backup").exists() and sha(server_tpl) == before
        # CSV-Inhalt kam mit, verknüpft ist der lokale Windows-Pfad
        assert read_csv(local_csv)[0]["Section3"] == "MT3608"
        assert f'databasePath="{local_csv.resolve()}"' in zipfile.ZipFile(tpl).read("label.xml").decode("utf-8")
        job = t.jobs("bearbeiten")[-1]
        assert job["name"] == "PartDB_3U-3.lbx" and job["csv"]["name"] == "PartDB_3U-3.csv"
        assert base64.b64decode(job["csv"]["inhalt"]) == (cfg.output_dir / "Beispiele" / "PartDB_3U-3.csv").read_bytes()
        # Abgleich bringt die Änderung zum Server
        sync(c, t, kern)
        assert sha(server_tpl) == sha(tpl) != before
        # zweites Bearbeiten: kein weiteres Backup
        r = c.post("/api/vorlagen/PartDB_3U-3.lbx/bearbeiten", json={"quelle": "worst"}).json()
        assert r["backup"] is None and len(list((pc / ".backup").iterdir())) == 1
    assert c.local_opened == []


def test_oeffnen_und_ordner_mit_csv_inhalt(tmp_path):
    c, cfg, kern, opened = setup(tmp_path)
    with connected(c, kern) as t:
        c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 1})
        ex = c.post("/api/name-labels/export", json={"open_template": True}).json()
        assert "vorlage_fehler" not in ex, ex
        job = t.jobs("oeffnen")[-1]
        assert isinstance(job["csv"], dict) and set(job["csv"]) == {"name", "inhalt"}
        assert "pfad" not in job and str(cfg.output_dir) not in str(job)   # keine Server-Pfade
        local_csv = tmp_path / "helfer-csv" / Path(ex["datei"]).name
        linked = Path(ex["vorlage"])
        assert local_csv.read_bytes() == Path(ex["datei"]).read_bytes() and opened == [linked]
        assert f'databasePath="{local_csv.resolve()}"' in zipfile.ZipFile(linked).read("label.xml").decode("utf-8")

        assert c.post("/api/open-output").status_code == 200
        job = t.jobs("ordner")[-1]
        assert [f["name"] for f in job["dateien"]] == [Path(ex["datei"]).name]
        assert opened[-1] == tmp_path / "helfer-csv"


# ------------------------------------------------------------------ Namen/Pfade, beidseitig
def test_ungueltige_namen(tmp_path):
    c, cfg, kern, _ = setup(tmp_path)
    data = (VORLAGEN / "PartDB_SMD15.lbx").read_bytes()
    b64 = base64.b64encode(data).decode()
    bad_names = ["../x.lbx", "..\\x.lbx", "sub/x.lbx", "C:\\x.lbx", "x.txt", ".x.lbx", "", None, "x.lbx ", "a:b.lbx"]
    for name in bad_names:
        assert not valid_name(name)
        r = kern.handle({"typ": "datei", "id": "1", "name": name})
        assert r["ok"] is False and "Ungültiger Vorlagenname" in r["fehler"], name
        r = kern.handle({"typ": "vorlage_speichern", "id": "2", "name": name, "sha256": sha(data), "daten": b64})
        assert r["ok"] is False, name
        r = kern.handle({"typ": "bearbeiten", "id": "3", "name": name,
                         "csv": {"name": "a.csv", "inhalt": base64.b64encode(b"x").decode()}})
        assert r["ok"] is False, name
    for name in ("../a.csv", "a.lbx", "sub\\a.csv", ".a.csv"):
        r = kern.handle({"typ": "ordner", "id": "4", "dateien": [{"name": name, "inhalt": "eA=="}]})
        assert r["ok"] is False and "CSV-Name" in r["fehler"], name
    assert not (tmp_path / "x.lbx").exists() and not (tmp_path / "a.csv").exists()
    # Prüfsumme und Inhalt werden geprüft
    r = kern.handle({"typ": "vorlage_speichern", "id": "5", "name": "y.lbx", "sha256": "0" * 64, "daten": b64})
    assert r["ok"] is False and "Prüfsumme" in r["fehler"]
    junk = base64.b64encode(b"kein zip").decode()
    r = kern.handle({"typ": "vorlage_speichern", "id": "6", "name": "y.lbx", "sha256": sha(b"kein zip"), "daten": junk})
    assert r["ok"] is False and not (kern.folder / "y.lbx").exists()

    # Server lässt sich vom Helfer keine Pfade unterschieben
    with connected(c, kern) as t:
        m = kern.manifest()
        m["dateien"].update({"../boese.lbx": sha(data), "sub/x.lbx": sha(data), "C:\\x.lbx": sha(data),
                             "kaputt.lbx": "keine-prüfsumme"})
        v = sync(c, t, kern, m)
        assert "ungültiger Name" in v["fehler"] and len(t.jobs("datei")) == 0
        assert not (tmp_path / "boese.lbx").exists() and server_files(cfg) == pc_files(kern.folder)

        # Helfer liefert eine andere Datei als angefragt -> verworfen
        shutil.copy2(VORLAGEN / "PartDB_SMD15.lbx", kern.folder / "Neu.lbx")
        old(kern.folder / "Neu.lbx")
        real = kern._job_datei
        kern._job_datei = lambda msg: dict(real(msg), name="../boese.lbx")
        v = sync(c, t, kern)
        assert "andere Datei" in v["fehler"] and not (cfg.template_dir / "Neu.lbx").exists()
        assert not (tmp_path / "boese.lbx").exists()
        kern._job_datei = real
        v = sync(c, t, kern)
        assert v["fehler"] is None and (cfg.template_dir / "Neu.lbx").is_file()


# ------------------------------------------------------------------ ohne Vorlagen-Ordner
def test_kein_vorlagen_ordner(tmp_path):
    c, cfg, kern, opened = setup(tmp_path, pc=None)
    m = kern.manifest()
    assert m["ordner"] is None and m["dateien"] is None
    before = server_files(cfg)
    with connected(c, kern) as t:
        v = sync(c, t, kern, m)
        assert v == {"ordner": None, "anzahl": 7, "abgeglichen": None, "fehler": None,
                     "hinweis": helfer_server.NO_FOLDER, "ausstehend": []}
        assert server_files(cfg) == before
        # Kopie bleibt im Server-Ordner (bisheriges Verhalten); Bearbeiten über den Server-Pfad
        r = c.post("/api/vorlagen/kopie", json={"quelle": "PartDB_SMD15.lbx", "name": "Neu.lbx"})
        assert r.status_code == 200, r.text
        assert (cfg.template_dir / "Neu.lbx").is_file() and opened == [cfg.template_dir / "Neu.lbx"]
        assert t.jobs("vorlage_speichern") == []
        assert (cfg.template_dir / ".backup").is_dir()          # Sicherung macht dann der Server
    q = c.get("/api/vorlagen").json()["quelle"]
    assert q["ordner"] == str(cfg.template_dir)


# ------------------------------------------------------------------ Konfiguration, Druckerwahl
def test_konfig_und_druckermodell(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    f = tmp_path / "helfer.yaml"
    f.write_text('server: "http://unraid:8765"\ntoken: "abc"\nvorlagen_ordner: "Etiketten"\n'
                 'drucker_modell: "PT-P710BT"\ndrucker: ""\nautostart: true\npfade: {}\n', encoding="utf-8")
    hcfg = load_helper_config(f)
    assert hcfg.vorlagen_ordner == tmp_path / "Etiketten" and hcfg.drucker_modell == "PT-P710BT"
    assert hcfg.csv_dir == tmp_path / "appdata" / "Labeltool-Helfer" / "csv" and hcfg.pfade == {}
    f.write_text('server: "x"\ntoken: "abc"\n', encoding="utf-8")       # alte Konfiguration
    assert load_helper_config(f).vorlagen_ordner is None

    names = ["Microsoft Print to PDF", "Brother PT-P710BT (Kopie 1)", "Brother PT-P710BT", "Brother QL-820NWB"]
    assert printer_for_model("PT-P710BT", names) == "Brother PT-P710BT"
    assert printer_for_model("pt-p710bt", names) == "Brother PT-P710BT"
    assert printer_for_model("QL-820NWB", names) == "Brother QL-820NWB"
    assert printer_for_model("PT-P710", names) is None and printer_for_model("", names) is None

    class Prn(FakeBpac):
        def status(self, printer):
            return {"ok": True, "grund": "", "drucker": names}
    hcfg = HelferConfig(server="x", token="t", drucker_modell="QL-820NWB", cache_dir=tmp_path / "c")
    prn = Prn()
    kern = HelferKern(hcfg, printer=prn, opener=lambda p: None, state_fn=online, hostname="PC")
    info = kern.info()
    assert info["drucker"] == "Brother QL-820NWB" and info["drucker_modell"] == "QL-820NWB" and info["protokoll"] == 2
    data = (VORLAGEN / "PartDB_SMD15.lbx").read_bytes()
    tpl = {"name": "a.lbx", "sha256": sha(data), "daten": base64.b64encode(data).decode()}
    r = kern.handle({"typ": "drucken", "id": "1", "vorlage": tpl, "zeilen": [{}], "drucker": "", "flags": 0})
    assert r["ok"] and prn.jobs[-1][2] == "Brother QL-820NWB"     # Attrappe – nie echt gedruckt
    hcfg.drucker = DRUCKER                                         # ausdrücklicher Drucker gewinnt
    assert kern.info()["drucker"] == DRUCKER
