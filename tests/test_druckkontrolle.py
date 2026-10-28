"""Druckkontrolle: fehlende Labels gezielt nachdrucken und die Windows-Warteschlange nach dem Senden abfragen."""
import sys
import types

import httpx
from fastapi.testclient import TestClient

from labeltool import desktop, helfer_server
from labeltool.app import create_app
from test_helfer import DRUCKER, connected, setup
from test_smoke import FakePrinter, make


class QueuePrinter(FakePrinter):
    def __init__(self, answer):
        super().__init__()
        self.answer, self.asked = answer, []

    def queue(self, printer):
        self.asked.append(printer)
        return self.answer


def client(tmp_path, prn):
    _, fake, cfg = make(tmp_path)
    cfg.direct_print = True
    return TestClient(create_app(cfg, transport=httpx.MockTransport(fake), opener=lambda p: None, printer=prn)), fake


def test_box_labels_nur_fehlende_nachdrucken(tmp_path):
    prn = FakePrinter()
    c, _ = client(tmp_path, prn)
    c.post("/api/boxes", json={"abbr": "MIC", "compartments": 2, "box_type": "WinBin2", "count": 3})
    r = c.post("/api/name-labels/print").json()
    labels = r["labels"]
    assert [l["name"] for l in labels] == r["boxen"] and len(labels) == 3

    # nur die zweite Box fehlt
    missing = labels[1]
    assert c.post("/api/druck/pruefen", json={"art": "box", "ids": [missing["id"]]}).json()["labels"] == 1
    r = c.post("/api/name-labels/print", json={"ids": [missing["id"]], "kopien": 2})
    assert r.status_code == 200, r.text
    assert r.json()["labels"] == [missing]
    rows = prn.jobs[-1][1]
    assert [row["Lagername"] for row in rows] == [missing["name"]] * 2

    # „Gedruckt ✓“ gilt weiter für alle drei Boxen des ersten Drucks
    assert c.post("/api/name-labels/done").json()["erledigt"] == 3
    assert c.get("/api/name-labels").json()["boxes"] == []

    # Box nicht (mehr) in der Warteschlange -> klare Meldung, nichts gedruckt
    n = len(prn.jobs)
    r = c.post("/api/name-labels/print", json={"ids": [missing["id"]]})
    assert r.status_code == 400 and "nicht (mehr) in der Warteschlange" in r.json()["detail"]
    assert len(prn.jobs) == n


def test_bauteil_labels_liefern_box_ids(tmp_path):
    prn = FakePrinter()
    c, _ = client(tmp_path, prn)
    r = c.post("/api/content-labels/print").json()
    ids = {l["id"] for l in r["labels"]}
    assert ids and all(isinstance(i, int) for i in ids)

    # Nachdruck einer fehlenden Box über ihre ID
    one = r["labels"][0]
    r = c.post("/api/content-labels/nachdruck", json={"ids": [one["id"]], "direkt": True}).json()
    assert r["labels"] == [one]


def test_warteschlange_endpunkt(tmp_path):
    c, _ = client(tmp_path, FakePrinter())
    assert c.get("/api/druck/warteschlange").json() == {"unterstuetzt": False}   # Drucker ohne Abfrage

    prn = QueuePrinter({"auftraege": 1, "fehler": "Band leer oder fehlt", "seiten": 2, "seiten_gesamt": 5})
    (tmp_path / "b").mkdir()
    c, _ = client(tmp_path / "b", prn)
    d = c.get("/api/druck/warteschlange").json()
    assert d["unterstuetzt"] is True and d["fehler"] == "Band leer oder fehlt" and d["seiten"] == 2

    prn.answer = None                                                             # kein Windows
    assert c.get("/api/druck/warteschlange").json() == {"unterstuetzt": False}


def test_local_printer_queue_nimmt_ptouch():
    asked = []
    module = types.SimpleNamespace(status=lambda p: {"drucker": ["Microsoft Print to PDF", DRUCKER]})
    lp = helfer_server.LocalPrinter(module=module, state_fn=lambda n: {}, queue_fn=lambda n: asked.append(n) or {"auftraege": 0})
    assert lp.queue("") == {"auftraege": 0} and asked == [DRUCKER]
    lp.queue("Anderer")
    assert asked[-1] == "Anderer"


def test_warteschlange_ueber_helfer(tmp_path):
    c, cfg, kern, _, _ = setup(tmp_path)
    asked = []
    kern.queue_fn = lambda name: asked.append(name) or {"auftraege": 1, "fehler": "", "seiten": 1, "seiten_gesamt": 3}
    assert kern.info()["warteschlange"] is True
    with connected(c, kern):
        d = c.get("/api/druck/warteschlange").json()
    assert d == {"auftraege": 1, "fehler": "", "seiten": 1, "seiten_gesamt": 3, "unterstuetzt": True}
    assert asked == [DRUCKER]


def test_alter_helfer_ohne_warteschlange(tmp_path, monkeypatch):
    c, cfg, kern, _, _ = setup(tmp_path)
    info = kern.info
    monkeypatch.setattr(kern, "info", lambda: {k: v for k, v in info().items() if k != "warteschlange"})
    with connected(c, kern):
        assert c.get("/api/druck/warteschlange").json() == {"unterstuetzt": False}


def test_print_queue_liest_auftraege(monkeypatch):
    jobs = [{"Status": 0x40 | 0x10, "pStatus": "", "PagesPrinted": 3, "TotalPages": 7},
            {"Status": 0, "pStatus": "Bandende", "PagesPrinted": 0, "TotalPages": 2}]
    fake = types.SimpleNamespace(OpenPrinter=lambda n: "h", ClosePrinter=lambda h: None,
                                 EnumJobs=lambda h, a, b, lvl: jobs,
                                 GetPrinter=lambda h, lvl: {"Status": 0x10, "Attributes": 0, "cJobs": 2})
    monkeypatch.setitem(sys.modules, "win32print", fake)
    q = desktop.print_queue(DRUCKER)
    assert q["auftraege"] == 2 and q["seiten"] == 3 and q["seiten_gesamt"] == 9
    assert q["fehler"].split(", ") == ["Band leer oder fehlt", "Bandende", "kein Band"]

    jobs.clear()                              # leer: Druckerstatus allein ist kein Auftragsfehler
    assert desktop.print_queue(DRUCKER) == {"auftraege": 0, "fehler": "", "seiten": 0, "seiten_gesamt": 0}
    assert desktop.print_queue("") is None
