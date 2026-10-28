"""Einrichtungs-Assistent für den Windows-Druck-Helfer (ein Fenster, tkinter).

Start: python -m labeltool.helfer_setup [--aus-bat] [--konsole] [helfer.yaml]
       (helfer.bat ruft ihn auf, wenn helfer.yaml fehlt, oder mit "helfer.bat einrichten")

Fragt Server-Adresse, Helfer-Token, Vorlagen-Ordner auf diesem PC, Druckermodell und Autostart
ab und schreibt helfer.yaml. Vorbelegt aus helfer.vorgabe.yaml (liegt im Download-Paket, enthält
Server und Token) bzw. einer vorhandenen helfer.yaml. "Speichern & starten" schreibt die Datei,
legt auf Wunsch die Autostart-Verknüpfung an und startet den Helfer (beim Aufruf aus helfer.bat
startet helfer.bat ihn danach selbst – Rückgabewert 0 = gespeichert, 1 = abgebrochen).

Die Logik (Vorbelegung, Prüfung, YAML, Modellauswahl, Autostart) steht in Funktionen ohne
Fenster und ist so testbar. Ohne tkinter (oder ohne Anzeige) fragt der Assistent in der Konsole.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import yaml

from . import ptouch_modelle

DATEI = "helfer.yaml"
VORGABE = "helfer.vorgabe.yaml"
AUTOSTART_NAME = "Labeltool Druck-Helfer.lnk"
ORDNER_NAME = "Labeltool-Vorlagen"
BEKANNT = ("server", "token", "vorlagen_ordner", "drucker_modell", "drucker", "autostart")


class SetupFehler(Exception):
    pass


@dataclass
class Werte:
    server: str = ""
    token: str = ""
    vorlagen_ordner: str = ""
    drucker_modell: str = ""
    drucker: str = ""             # Windows-Druckername, leer = passend zum Modell automatisch
    autostart: bool = True
    extra: dict = field(default_factory=dict)   # weitere Schlüssel (pfade, cache_ordner …) bleiben erhalten


# ------------------------------------------------------------------ Vorbelegung
def _yaml_laden(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _mtime(path: Path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def dokumente_ordner() -> Path:
    """Ordner "Dokumente" des Benutzers (auch wenn er z. B. nach OneDrive umgeleitet ist)."""
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:   # CSIDL_PERSONAL
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def ordner_vorschlag() -> Path:
    return dokumente_ordner() / ORDNER_NAME


def lade_werte(ordner: Path) -> Werte:
    """Vorbelegung aus helfer.yaml und helfer.vorgabe.yaml.

    Server/Token: aus der neueren der beiden Dateien (ein frisch heruntergeladenes Paket bringt
    den aktuellen Token mit), alles andere aus helfer.yaml, sonst Vorschläge.
    """
    ordner = Path(ordner)
    vorgabe, bisher = _yaml_laden(ordner / VORGABE), _yaml_laden(ordner / DATEI)
    vorne, hinten = (vorgabe, bisher) if vorgabe and _mtime(ordner / VORGABE) > _mtime(ordner / DATEI) \
        else (bisher, vorgabe)

    def wert(key: str, *quellen: dict) -> str:
        return next((str(q[key]).strip() for q in quellen if q.get(key) not in (None, "")), "")

    return Werte(
        server=wert("server", vorne, hinten),
        token=wert("token", vorne, hinten),
        vorlagen_ordner=wert("vorlagen_ordner", bisher, vorgabe) or str(ordner_vorschlag()),
        drucker_modell=wert("drucker_modell", bisher, vorgabe).upper(),
        drucker=wert("drucker", bisher),
        autostart=bool(bisher.get("autostart", vorgabe.get("autostart", True))),
        extra={k: v for k, v in bisher.items() if k not in BEKANNT},
    )


# ------------------------------------------------------------------ Prüfen
def normalisiere_server(url: str) -> str:
    """'unraid:8765/' -> 'http://unraid:8765'. Unsinn -> SetupFehler."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise SetupFehler("Die Server-Adresse fehlt (z. B. http://unraid:8765).")
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SetupFehler(f"Server-Adresse '{url}' verstehe ich nicht – Beispiel: http://unraid:8765")
    try:
        parts.port
    except ValueError:
        raise SetupFehler(f"Server-Adresse '{url}' enthält einen ungültigen Port.")
    return url


def lbx_anzahl(ordner: str | Path) -> int:
    """Anzahl .lbx-Vorlagen direkt im Ordner (keine Unterordner, keine versteckten Dateien)."""
    try:
        return sum(1 for p in Path(ordner).iterdir()
                   if p.is_file() and p.suffix.lower() == ".lbx" and not p.name.startswith("."))
    except OSError:
        return 0


def ordner_text(ordner: str) -> str:
    """Kurzinfo unter dem Ordnerfeld."""
    if not ordner.strip():
        return "Bitte einen Ordner wählen."
    p = Path(ordner)
    if not p.exists():
        return "Ordner gibt es noch nicht – wird beim Speichern angelegt."
    if not p.is_dir():
        return "Das ist kein Ordner."
    n = lbx_anzahl(p)
    return f"{n} Vorlage(n) (.lbx) gefunden." if n else "Noch keine .lbx-Vorlagen in diesem Ordner."


def pruefe(w: Werte, modelle: list[str] | None = None) -> tuple[list[str], list[str]]:
    """-> (fehler, hinweise). Fehler verhindern das Speichern, Hinweise werden nur angezeigt."""
    fehler, hinweise = [], []
    try:
        normalisiere_server(w.server)
    except SetupFehler as e:
        fehler.append(str(e))
    token = (w.token or "").strip()
    if not token:
        fehler.append("Der Helfer-Token fehlt – im Labeltool unter Einstellungen > Helfer "
                      "(oder das Helfer-Paket neu herunterladen, dort ist er schon eingetragen).")
    elif any(c.isspace() for c in token):
        fehler.append("Der Helfer-Token darf keine Leerzeichen enthalten.")
    ordner = (w.vorlagen_ordner or "").strip()
    if not ordner:
        fehler.append("Bitte den Vorlagen-Ordner wählen (dort liegen die .lbx-Vorlagen auf diesem PC).")
    elif not (Path(ordner).is_absolute() or PureWindowsPath(ordner).is_absolute()):   # Windows-Pfad, auch beim Testen unter Linux
        fehler.append("Der Vorlagen-Ordner braucht einen vollständigen Pfad, z. B. C:\\Users\\…\\Dokumente\\Labeltool-Vorlagen.")
    elif Path(ordner).exists() and not Path(ordner).is_dir():
        fehler.append(f"'{ordner}' ist eine Datei, kein Ordner.")
    elif not Path(ordner).exists():
        hinweise.append(f"Der Ordner '{ordner}' wird angelegt.")
    if ordner and Path(ordner).is_absolute() and not lbx_anzahl(ordner):
        hinweise.append("Im Vorlagen-Ordner liegen noch keine .lbx-Vorlagen. Dieser Ordner ist die Quelle der "
                        "Vorlagen für das Labeltool – lege deine Vorlagen dort ab.")
    modell = (w.drucker_modell or "").strip().upper()
    if not modell:
        fehler.append("Bitte das Druckermodell wählen (z. B. PT-P710BT).")
    elif modelle is not None and modell not in {m.upper() for m in modelle} \
            and ptouch_modelle.modell_aus_name(modell) != modell:
        fehler.append(f"Unbekanntes Druckermodell '{w.drucker_modell}'.")
    return fehler, hinweise


# ------------------------------------------------------------------ Modellauswahl
def modell_eintraege(modelle: list[str], installierte: list[dict]) -> list[tuple[str, str]]:
    """[(Anzeige, Modell)] für die Auswahlliste: installierte Modelle oben mit „(installiert)“."""
    oben, gesehen = [], set()
    for d in installierte or []:
        m = (d.get("modell") or "").upper()
        if m and m not in gesehen:
            gesehen.add(m)
            oben.append((f"{m} (installiert)", m))
    rest = [(m, m) for m in ptouch_modelle.sortiert(modelle or []) if m.upper() not in gesehen]
    return oben + rest


def modell_vorauswahl(bisher: str, installierte: list[dict]) -> str:
    """Bisheriges Modell, sonst das erste installierte, sonst leer."""
    if (bisher or "").strip():
        return bisher.strip().upper()
    return next(((d.get("modell") or "").upper() for d in installierte or [] if d.get("modell")), "")


def modell_hinweis(modell: str, installierte: list[dict]) -> tuple[bool, str]:
    """(installiert?, Text unter der Modellauswahl)."""
    if not modell:
        return False, "Bitte das Modell deines Etikettendruckers wählen."
    name = ptouch_modelle.passender_drucker(modell, installierte)
    if name:
        return True, f"✓ Installiert als Windows-Drucker „{name}“."
    return False, (f"Für {modell} ist unter Windows kein Drucker installiert. Bitte den Treiber von Brother "
                   f"installieren und den Drucker anschließen – speichern kannst du trotzdem.")


# ------------------------------------------------------------------ helfer.yaml
def _q(value: str) -> str:
    """YAML-String in doppelten Anführungszeichen (JSON-Escapes sind gültiges YAML, \\ in Pfaden bleibt heil)."""
    return json.dumps(str(value), ensure_ascii=False)


def yaml_text(w: Werte) -> str:
    lines = [
        "# Druck-Helfer – geschrieben vom Einrichtungs-Assistenten (helfer.bat einrichten).",
        "# Kann auch von Hand geändert werden; der Helfer liest die Datei bei jedem Verbindungsaufbau neu.",
        "",
        "# Adresse des Labeltool-Servers (wie im Browser)",
        f"server: {_q(normalisiere_server(w.server))}",
        "# Helfer-Token (Labeltool > Einstellungen > Helfer)",
        f"token: {_q(w.token.strip())}",
        "# Quelle der .lbx-Vorlagen auf diesem PC – der Server gleicht seine Vorlagen damit ab",
        f"vorlagen_ordner: {_q(w.vorlagen_ordner.strip())}",
        "# Modell des Etikettendruckers (Liste aus dem P-touch Editor)",
        f"drucker_modell: {_q(w.drucker_modell.strip().upper())}",
        "# Windows-Druckername; leer = passend zum Modell automatisch suchen",
        f"drucker: {_q(w.drucker.strip())}",
        "# nur Info für den Assistenten (Verknüpfung im Autostart-Ordner)",
        f"autostart: {'true' if w.autostart else 'false'}",
    ]
    extra = {k: v for k, v in (w.extra or {}).items() if k not in BEKANNT}
    if extra:
        lines += ["", "# weitere Einstellungen (unverändert übernommen)",
                  yaml.safe_dump(extra, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()]
    return "\n".join(lines) + "\n"


def schreibe_helfer_yaml(path: Path, w: Werte) -> Path:
    """Prüfen, atomar schreiben (erst .tmp, dann umbenennen) und zur Kontrolle wieder lesen."""
    fehler, _ = pruefe(w)
    if fehler:
        raise SetupFehler(" ".join(fehler))
    path = Path(path)
    text = yaml_text(w)
    data = yaml.safe_load(text)
    if data.get("token") != w.token.strip() or data.get("vorlagen_ordner") != w.vorlagen_ordner.strip():
        raise SetupFehler("helfer.yaml konnte nicht fehlerfrei erzeugt werden.")   # pragma: no cover
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ Autostart / Start
def autostart_ordner() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


_PS_VERKNUEPFUNG = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LT_LNK);"
    "$s.TargetPath = $env:LT_ZIEL; $s.WorkingDirectory = $env:LT_ORDNER;"
    "$s.WindowStyle = 7; $s.Description = 'Labeltool Druck-Helfer'; $s.Save()"
)


def autostart_setzen(an: bool, bat: Path, ordner: Path | None = None, runner=subprocess.run) -> Path:
    """Verknüpfung „Labeltool Druck-Helfer“ (startet helfer.bat minimiert) anlegen bzw. entfernen."""
    lnk = Path(ordner or autostart_ordner()) / AUTOSTART_NAME
    if not an:
        try:
            lnk.unlink(missing_ok=True)
        except OSError as e:
            raise SetupFehler(f"Autostart-Verknüpfung nicht entfernt: {e}")
        return lnk
    bat = Path(bat).resolve()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, LT_LNK=str(lnk), LT_ZIEL=str(bat), LT_ORDNER=str(bat.parent))
    try:
        r = runner(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                    _PS_VERKNUEPFUNG], env=env, capture_output=True, text=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise SetupFehler(f"Autostart-Verknüpfung nicht angelegt: {e}")
    if getattr(r, "returncode", 1) != 0:
        raise SetupFehler(f"Autostart-Verknüpfung nicht angelegt: {(getattr(r, 'stderr', '') or '').strip()[:300]}")
    return lnk


def helfer_starten(ordner: Path, popen=subprocess.Popen) -> None:
    """helfer.bat in einem eigenen Konsolenfenster starten."""
    bat = Path(ordner) / "helfer.bat"
    if not bat.is_file():
        raise SetupFehler(f"{bat} fehlt – bitte das Helfer-Paket vollständig entpacken.")
    popen(["cmd.exe", "/c", str(bat)], cwd=str(ordner),
          creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def speichern(ordner: Path, w: Werte, aus_bat: bool = False, autostart_dir: Path | None = None,
              runner=subprocess.run, popen=subprocess.Popen) -> list[str]:
    """helfer.yaml schreiben, Ordner anlegen, Autostart setzen, Helfer starten -> Meldungen."""
    ordner = Path(ordner)
    w.server = normalisiere_server(w.server)
    w.drucker_modell = (w.drucker_modell or "").strip().upper()
    path = schreibe_helfer_yaml(ordner / DATEI, w)
    out = [f"Gespeichert: {path}"]
    try:
        Path(w.vorlagen_ordner).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SetupFehler(f"Vorlagen-Ordner nicht angelegt: {e}")
    lnk = autostart_setzen(w.autostart, ordner / "helfer.bat", autostart_dir, runner=runner)
    out.append(f"Autostart eingerichtet: {lnk}" if w.autostart else "Kein Autostart.")
    if not aus_bat:
        helfer_starten(ordner, popen=popen)
        out.append("Helfer gestartet.")
    return out


def verbindung_testen(server: str, token: str = "", timeout: float = 6.0,
                      opener=urllib.request.urlopen) -> tuple[bool, str]:
    """GET <server>/api/meta – ist dort ein Labeltool erreichbar? Mit token zusätzlich
    POST /api/helfer/token-pruefen (verdrängt keinen laufenden Helfer)."""
    try:
        url = normalisiere_server(server)
    except SetupFehler as e:
        return False, str(e)
    try:
        with opener(urllib.request.Request(url + "/api/meta", headers={"Accept": "application/json"}),
                    timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"Server antwortet mit HTTP {e.code} – ist das die Adresse des Labeltools?"
    except (urllib.error.URLError, OSError) as e:
        grund = getattr(e, "reason", None) or e
        return False, f"Server nicht erreichbar: {grund}"
    except ValueError:
        return False, "Unter dieser Adresse antwortet kein Labeltool (keine gültige Antwort)."
    if not isinstance(data, dict) or "kategorien" not in data:
        return False, "Unter dieser Adresse antwortet kein Labeltool."
    if not token.strip():
        return True, "✓ Labeltool-Server erreichbar."
    req = urllib.request.Request(url + "/api/helfer/token-pruefen", method="POST",
                                 data=json.dumps({"token": token.strip()}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener(req, timeout=timeout) as r:
            antwort = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:   # älterer Server ohne Token-Prüfung
            return True, "✓ Labeltool-Server erreichbar (Token kann dieser Server nicht prüfen)."
        return False, f"Token-Prüfung fehlgeschlagen: HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"Token-Prüfung fehlgeschlagen: {getattr(e, 'reason', None) or e}"
    if not (isinstance(antwort, dict) and antwort.get("ok")):
        grund = antwort.get("grund") if isinstance(antwort, dict) else ""
        return False, "✗ Server erreichbar, aber der Token passt nicht" + (f": {grund}" if grund else
                      " – im Labeltool unter Einstellungen → Druck-Helfer neu herunterladen.")
    return True, "✓ Labeltool-Server erreichbar, Token gültig."


# ------------------------------------------------------------------ Fenster (tkinter)
class Assistent:
    """Das Fenster. modelle_fn/drucker_fn liefern Modellliste bzw. installierte Drucker
    (Standard: ptouch_modelle) und laufen im Hintergrund, damit das Fenster sofort erscheint."""

    def __init__(self, root, ordner: Path, werte: Werte, aus_bat: bool = False,
                 modelle_fn=ptouch_modelle.alle_modelle, drucker_fn=ptouch_modelle.installierte_drucker,
                 speichern_fn=speichern):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.ordner, self.werte, self.aus_bat = root, Path(ordner), werte, aus_bat
        self.modelle_fn, self.drucker_fn, self.speichern_fn = modelle_fn, drucker_fn, speichern_fn
        self.modelle: list[str] = list(ptouch_modelle.EINGEBAUT)
        self.installierte: list[dict] = []
        self.eintraege: list[tuple[str, str]] = []
        self.ergebnis = False
        self._q: queue.Queue = queue.Queue()

        root.title("Labeltool Druck-Helfer – Einrichtung")
        root.minsize(620, 0)
        f = ttk.Frame(root, padding=14)
        f.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="Dieser PC druckt für das Labeltool. Bitte prüfen und speichern.",
                  font=("Segoe UI", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 10))

        r += 1
        self.v_server = tk.StringVar(value=werte.server)
        ttk.Label(f, text="Server-Adresse").grid(row=r, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_server).grid(row=r, column=1, sticky="ew", padx=6)
        self.b_test = ttk.Button(f, text="Verbindung testen", command=self.testen)
        self.b_test.grid(row=r, column=2, sticky="ew")
        r += 1
        self.l_server = ttk.Label(f, text="", foreground="#555")
        self.l_server.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)

        r += 1
        self.v_token = tk.StringVar(value=werte.token)
        self.v_zeigen = tk.BooleanVar(value=False)
        ttk.Label(f, text="Helfer-Token").grid(row=r, column=0, sticky="w", pady=(8, 0))
        self.e_token = ttk.Entry(f, textvariable=self.v_token, show="•")
        self.e_token.grid(row=r, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Checkbutton(f, text="anzeigen", variable=self.v_zeigen,
                        command=lambda: self.e_token.configure(show="" if self.v_zeigen.get() else "•")
                        ).grid(row=r, column=2, sticky="w", pady=(8, 0))

        r += 1
        self.v_ordner = tk.StringVar(value=werte.vorlagen_ordner)
        ttk.Label(f, text="Vorlagen-Ordner").grid(row=r, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.v_ordner).grid(row=r, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(f, text="Auswählen …", command=self.ordner_waehlen).grid(row=r, column=2, sticky="ew", pady=(8, 0))
        r += 1
        self.l_ordner = ttk.Label(f, text="", foreground="#555", wraplength=440, justify="left")
        self.l_ordner.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        self.v_ordner.trace_add("write", lambda *_: self.l_ordner.configure(text=ordner_text(self.v_ordner.get())))
        self.l_ordner.configure(text=ordner_text(self.v_ordner.get()))

        r += 1
        self.v_modell = tk.StringVar()
        ttk.Label(f, text="Druckermodell").grid(row=r, column=0, sticky="w", pady=(8, 0))
        self.c_modell = ttk.Combobox(f, textvariable=self.v_modell, state="readonly", height=20)
        self.c_modell.grid(row=r, column=1, sticky="ew", padx=6, pady=(8, 0))
        self.c_modell.bind("<<ComboboxSelected>>", lambda _e: self.modell_geaendert())
        self.b_suchen = ttk.Button(f, text="Drucker suchen", command=self.laden)
        self.b_suchen.grid(row=r, column=2, sticky="ew", pady=(8, 0))
        r += 1
        self.l_modell = ttk.Label(f, text="Suche installierte Drucker …", foreground="#555", wraplength=440,
                                  justify="left")
        self.l_modell.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        r += 1
        self.l_link = ttk.Label(f, text="Brother-Treiber herunterladen", foreground="#0645ad", cursor="hand2")
        self.l_link.bind("<Button-1>", lambda _e: self._link())
        self.l_link.grid(row=r, column=1, sticky="w", padx=6)
        self.l_link.grid_remove()

        r += 1
        self.v_auto = tk.BooleanVar(value=werte.autostart)
        ttk.Checkbutton(f, text="Helfer bei der Windows-Anmeldung automatisch starten (minimiert)",
                        variable=self.v_auto).grid(row=r, column=1, columnspan=2, sticky="w", padx=6, pady=(10, 0))

        r += 1
        self.l_status = ttk.Label(f, text="", foreground="#a00", wraplength=580, justify="left")
        self.l_status.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        r += 1
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bf, text="Abbrechen", command=self.abbrechen).pack(side="right", padx=(6, 0))
        self.b_ok = ttk.Button(bf, text="Speichern & starten", command=self.speichern)
        self.b_ok.pack(side="right")
        root.bind("<Escape>", lambda _e: self.abbrechen())
        root.protocol("WM_DELETE_WINDOW", self.abbrechen)

        self._modelle_setzen(self.modelle, [], vorauswahl=werte.drucker_modell)
        self.laden()
        self.root.after(100, self._pumpe)

    # ----------------------------------------------------------- Hintergrund
    def _hintergrund(self, art: str, fn, *args) -> None:
        def run():
            try:
                self._q.put((art, True, fn(*args)))
            except Exception as e:   # pragma: no cover
                self._q.put((art, False, e))
        threading.Thread(target=run, daemon=True).start()

    def _pumpe(self) -> None:
        try:
            while True:
                art, ok, daten = self._q.get_nowait()
                if art == "laden":
                    self.b_suchen.state(["!disabled"])
                    if ok:
                        self._modelle_setzen(*daten, vorauswahl=self.aktuelles_modell() or self.werte.drucker_modell)
                    else:
                        self.l_modell.configure(text=f"Drucker konnten nicht ermittelt werden: {daten}")
                elif art == "test":
                    self.b_test.state(["!disabled"])
                    good, text = daten if ok else (False, str(daten))
                    self.l_server.configure(text=text, foreground="#060" if good else "#a00")
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._pumpe)
        except Exception:   # Fenster schon zu
            pass

    def laden(self) -> None:
        self.b_suchen.state(["disabled"])
        self.l_modell.configure(text="Suche installierte Drucker …")
        self._hintergrund("laden", lambda: (self.modelle_fn(), self.drucker_fn()))

    def testen(self) -> None:
        self.b_test.state(["disabled"])
        self.l_server.configure(text="Teste …", foreground="#555")
        self._hintergrund("test", verbindung_testen, self.v_server.get(), self.v_token.get())

    # ----------------------------------------------------------- Modell
    def _modelle_setzen(self, modelle, installierte, vorauswahl: str = "") -> None:
        self.modelle, self.installierte = list(modelle), list(installierte)
        self.eintraege = modell_eintraege(self.modelle, self.installierte)
        self.c_modell.configure(values=[a for a, _ in self.eintraege])
        wahl = modell_vorauswahl(vorauswahl, self.installierte)
        label = next((a for a, m in self.eintraege if m == wahl), "")
        if wahl and not label:     # z. B. Modell aus alter helfer.yaml, das die Liste nicht kennt
            self.eintraege.insert(0, (wahl, wahl))
            self.c_modell.configure(values=[a for a, _ in self.eintraege])
            label = wahl
        self.v_modell.set(label)
        self.modell_geaendert()

    def aktuelles_modell(self) -> str:
        label = self.v_modell.get()
        return next((m for a, m in self.eintraege if a == label), label.split(" ")[0] if label else "")

    def modell_geaendert(self) -> None:
        ok, text = modell_hinweis(self.aktuelles_modell(), self.installierte)
        self.l_modell.configure(text=text, foreground="#060" if ok else "#555")
        if ok or not self.aktuelles_modell():
            self.l_link.grid_remove()
        else:
            self.l_link.grid()

    def _link(self) -> None:
        import webbrowser
        webbrowser.open(ptouch_modelle.TREIBER_URL)

    def ordner_waehlen(self) -> None:
        from tkinter import filedialog
        start = self.v_ordner.get()
        while start and not Path(start).is_dir() and Path(start).parent != Path(start):
            start = str(Path(start).parent)
        pfad = filedialog.askdirectory(parent=self.root, title="Vorlagen-Ordner wählen",
                                       initialdir=start or str(dokumente_ordner()), mustexist=False)
        if pfad:
            self.v_ordner.set(str(Path(pfad)))

    # ----------------------------------------------------------- Abschluss
    def werte_lesen(self) -> Werte:
        modell = self.aktuelles_modell()
        bisher = self.werte
        # Druckername: zum Modell passender installierter Drucker; der alte Eintrag nur, wenn er noch passt
        name = ptouch_modelle.passender_drucker(modell, self.installierte) or ""
        if not name and bisher.drucker and ptouch_modelle.modell_aus_name(bisher.drucker) == modell:
            name = bisher.drucker
        return Werte(server=self.v_server.get().strip(), token=self.v_token.get().strip(),
                     vorlagen_ordner=self.v_ordner.get().strip(), drucker_modell=modell, drucker=name,
                     autostart=bool(self.v_auto.get()), extra=dict(bisher.extra))

    def speichern(self) -> None:
        from tkinter import messagebox
        w = self.werte_lesen()
        fehler, hinweise = pruefe(w, self.modelle)
        if fehler:
            self.l_status.configure(text="\n".join("• " + t for t in fehler))
            return
        if hinweise and not messagebox.askokcancel("Hinweis", "\n\n".join(hinweise) + "\n\nTrotzdem speichern?",
                                                   parent=self.root):
            return
        try:
            meldungen = self.speichern_fn(self.ordner, w, aus_bat=self.aus_bat)
        except SetupFehler as e:
            self.l_status.configure(text=str(e))
            return
        self.ergebnis = True
        if not self.aus_bat:
            messagebox.showinfo("Gespeichert", "\n".join(meldungen), parent=self.root)
        self.root.destroy()

    def abbrechen(self) -> None:
        self.ergebnis = False
        self.root.destroy()


def fenster_assistent(ordner: Path, werte: Werte, aus_bat: bool) -> bool:
    import tkinter as tk
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    a = Assistent(root, ordner, werte, aus_bat=aus_bat)
    root.mainloop()
    return a.ergebnis


# ------------------------------------------------------------------ Konsole (Plan B ohne tkinter)
def konsole_assistent(ordner: Path, werte: Werte, aus_bat: bool, eingabe=input, ausgabe=print,
                      modelle_fn=ptouch_modelle.alle_modelle, drucker_fn=ptouch_modelle.installierte_drucker,
                      speichern_fn=speichern) -> bool:
    """Dieselben Fragen in der Konsole. Enter übernimmt den Vorschlag in [eckigen Klammern]."""
    def frage(text: str, vorschlag: str = "", geheim: bool = False) -> str:
        zeige = ("…" + vorschlag[-4:]) if geheim and vorschlag else vorschlag
        antwort = eingabe(f"{text}{f' [{zeige}]' if zeige else ''}: ").strip()
        return antwort or vorschlag

    ausgabe("Labeltool Druck-Helfer – Einrichtung (Enter = Vorschlag übernehmen, Strg+C = abbrechen)\n")
    try:
        modelle, installierte = modelle_fn(), drucker_fn()
        w = Werte(**{k: getattr(werte, k) for k in ("server", "token", "vorlagen_ordner", "drucker_modell",
                                                     "drucker", "autostart")}, extra=dict(werte.extra))
        while True:
            w.server = frage("Server-Adresse", w.server)
            w.token = frage("Helfer-Token", w.token, geheim=True)
            ok, text = verbindung_testen(w.server, w.token)
            ausgabe("  " + text)
            w.vorlagen_ordner = frage("Vorlagen-Ordner", w.vorlagen_ordner)
            ausgabe("  " + ordner_text(w.vorlagen_ordner))
            if installierte:
                ausgabe("  Installiert: " + ", ".join(f"{d['name']} ({d.get('modell') or '?'})" for d in installierte))
            w.drucker_modell = frage("Druckermodell (z. B. PT-P710BT)",
                                     modell_vorauswahl(w.drucker_modell, installierte)).upper()
            ausgabe("  " + modell_hinweis(w.drucker_modell, installierte)[1])
            w.drucker = ptouch_modelle.passender_drucker(w.drucker_modell, installierte) or ""
            w.autostart = frage("Autostart bei Windows-Anmeldung (j/n)", "j" if w.autostart else "n").lower() \
                .startswith("j")
            fehler, hinweise = pruefe(w, modelle)
            for t in fehler:
                ausgabe("FEHLER: " + t)
            for t in hinweise:
                ausgabe("Hinweis: " + t)
            if not fehler:
                break
            ausgabe("")
        for zeile in speichern_fn(ordner, w, aus_bat=aus_bat):
            ausgabe(zeile)
        return True
    except (KeyboardInterrupt, EOFError):
        ausgabe("\nEinrichtung abgebrochen.")
        return False
    except SetupFehler as e:
        ausgabe(f"FEHLER: {e}")
        return False


def tkinter_verfuegbar() -> tuple[bool, str]:
    try:
        import tkinter
        r = tkinter.Tk()
        r.destroy()
        return True, ""
    except Exception as e:   # ImportError (ohne Tcl/Tk installiert) oder TclError (keine Anzeige)
        return False, str(e) or type(e).__name__


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    aus_bat = "--aus-bat" in argv
    konsole = "--konsole" in argv
    rest = [a for a in argv if not a.startswith("--")]
    path = Path(rest[0]) if rest else Path(DATEI)
    ordner = path.resolve().parent
    werte = lade_werte(ordner)
    if not konsole:
        ok, grund = tkinter_verfuegbar()
        if not ok:
            print(f"Fenster nicht verfügbar ({grund}) – Einrichtung in der Konsole.")
            konsole = True
    try:
        done = konsole_assistent(ordner, werte, aus_bat) if konsole else fenster_assistent(ordner, werte, aus_bat)
    except SetupFehler as e:
        print(f"FEHLER: {e}")
        done = False
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
