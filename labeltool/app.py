"""FastAPI-Server mit kleiner Web-Oberfläche (lokal unter Windows oder im Docker mit Druck-Helfer)."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import bpac, helfer_server, routes_einstellungen, routes_helfer_download, vorlagen
from . import lbx as lbx_mod
from .config import BoxType, Category, Config
from .labels import (NAME_COLUMNS, build_content_labels, content_label_for_box, export_content_labels,
                     has_tag, join_unique, make_content_label, name_label_rows, parts_by_location, remove_tag,
                     search_boxes, write_csv)
from .model import Box, build_comment, make_name, next_number, parse_box
from .partdb import PartDB, PartDBError, ref_id
from .ptouch import TemplateError, open_file
from .stammdaten import MasterData, MasterDataError
from .state import State

STATIC = Path(__file__).parent / "static"


class NewBox(BaseModel):
    abbr: str
    compartments: int = Field(ge=1, le=99)
    box_type: str
    count: int = Field(default=1, ge=1, le=50)
    color: str | None = None        # leer = Boxfarbe des Inhalts, sonst Name aus der Palette
    parent_id: int | None = None


class IdList(BaseModel):
    ids: list[int]


class CategoryIn(BaseModel):
    key: str
    name: str
    color: str = ""


class ColorIn(BaseModel):
    name: str
    hex: str


class BoxTypeIn(BaseModel):
    name: str
    compartments: int = Field(default=1, ge=1, le=99)
    file: str = ""
    lbx: str = ""


class PrintOptions(BaseModel):
    kopien: int = Field(default=1, ge=1, le=50)     # Exemplare je Label (direkt hintereinander, ein Auftrag)
    band_gewechselt: bool = False                    # Nutzer hat den Rollenwechsel bestätigt


class NamePrint(PrintOptions):
    ids: list[int] | None = None                    # nur diese Boxen der Warteschlange (Nachdruck fehlender)


class Reprint(PrintOptions):
    ids: list[int] = Field(min_length=1, max_length=200)   # Lagerort-IDs der Boxen
    direkt: bool = False                                    # True = Direktdruck, sonst CSV schreiben


class PrintCheck(BaseModel):
    art: str                                # "box" | "bauteile" | "nachdruck"
    ids: list[int] = []                     # "nachdruck"; bei "box" optional: nur diese Boxen


def copies(rows: list[dict], n: int) -> list[dict]:
    """Jede Zeile n-mal direkt hintereinander – im selben Druckauftrag (Autocut dazwischen)."""
    return [r for r in rows for _ in range(max(1, n))]


class OpenTemplate(BaseModel):
    box_type: str | None = None     # None = 24-mm-Box-Label


class ExportOptions(BaseModel):
    open_template: bool = False     # nach dem Schreiben die verknüpfte Vorlage öffnen


class SampleSource(BaseModel):
    quelle: str = "worst"           # worst | vorlage | box
    box_id: int | None = None


class PreviewRow(BaseModel):
    zeile: dict[str, Any] = Field(default_factory=dict)


class TokenCheck(BaseModel):
    token: str = ""


class HelperUpdate(BaseModel):
    erzwingen: bool = False         # auch bei gleicher/neuerer Version neu installieren


class TemplateCopy(BaseModel):
    quelle: str                     # vorhandene .lbx
    name: str                       # neuer Dateiname (.lbx)
    lagerart: str | None = None     # optional dieser Lagerart zuweisen


def create_app(cfg: Config, transport: httpx.BaseTransport | None = None,
               opener: Callable[[Path], None] = open_file, printer=None, desktop=None) -> FastAPI:
    """printer: Modul/Objekt mit status(drucker), print_labels(vorlage, zeilen, drucker, flags)
    und für die Vorschau preview_available() und render_png(vorlage, zeile).
    desktop: Objekt mit open_linked/edit_original/open_folder (s. desktop.py).

    Ohne Angabe wählen printer/desktop bei jedem Aufruf je nach cfg.print_mode: "lokal" =
    b-PAC/P-touch im selben Prozess (opener öffnet Dateien), "helfer" = über den verbundenen
    Windows-Helfer (WebSocket /api/helfer/ws). Ausdrücklich übergebene Objekte gelten immer.
    """
    app = FastAPI(title="Part-DB Labeltool")
    state = State(cfg.state_file)
    master = MasterData(cfg)

    hub = helfer_server.HelperHub(cfg)
    # lokaler Status: ausdrücklich übergebener printer (Tests) oder b-PAC im Prozess
    local_printer = printer if printer is not None else helfer_server.LocalPrinter()
    if printer is None:
        printer = helfer_server.ModePrinter(cfg, local_printer, helfer_server.HelperPrinter(hub))
    if desktop is None:
        desktop = helfer_server.ModeDesktop(cfg, helfer_server.local_desktop(cfg, opener),
                                            helfer_server.HelperDesktop(hub))
    helper_status = helfer_server.StatusProvider(cfg, hub, local_printer)
    app.state.helper_hub = hub
    app.state.printer = printer
    app.state.desktop = desktop
    # Hooks für Einstellungen/Status (Helfer-Status, Token-Wechsel trennt den Helfer)
    app.state.hooks = helfer_server.Hooks(helper_status=helper_status,
                                          on_helper_token_changed=hub.disconnect_all)
    app.add_api_websocket_route("/api/helfer/ws", hub.endpoint)

    @app.post("/api/helfer/token-pruefen")
    def helper_token_check(req: TokenCheck):
        """Token prüfen, ohne eine Helfer-Verbindung aufzubauen (verdrängt keinen laufenden Helfer)."""
        if not hub.token:
            return {"ok": False, "grund": "Auf dem Server ist noch kein Helfer-Token eingerichtet."}
        ok = hub.token_ok(req.token.strip())
        return {"ok": ok, "grund": "" if ok else "Helfer-Token ungültig."}

    @app.post("/api/helfer/aktualisieren")
    def helper_update(req: HelperUpdate | None = None):
        """Update per Knopf: verbundener (installierter) Helfer lädt das Setup vom Server und startet es."""
        try:
            return hub.start_update(bool(req and req.erzwingen))
        except helfer_server.UpdateError as e:
            raise HTTPException(e.status, str(e))

    @app.post("/api/helfer/vorlagen/loeschen-bestaetigen")
    def helper_confirm_delete():
        """Massen-Löschschutz: fehlende Vorlagen wirklich nach .geloescht verschieben."""
        names = hub.sync.confirm_delete()
        if not names:
            raise HTTPException(400, "Es steht kein Löschen von Vorlagen zur Bestätigung an.")
        return {"bestaetigt": names, "abgleich_gestartet": hub.resync()}

    def api() -> PartDB:
        return PartDB(cfg.partdb_url, cfg.partdb_token, cfg.verify_ssl, transport=transport)

    def load_boxes(client: PartDB) -> dict[int, Box]:
        return {b.id: b for b in (parse_box(l, cfg) for l in client.storage_locations()) if b.id}

    @app.exception_handler(PartDBError)
    async def _pdb_error(_, exc: PartDBError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(MasterDataError)
    async def _master_error(_, exc: MasterDataError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TemplateError)
    async def _template_error(_, exc: TemplateError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(vorlagen.TemplateNotFound)
    async def _template_missing(_, exc: vorlagen.TemplateNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(vorlagen.TemplateExists)
    async def _template_exists(_, exc: vorlagen.TemplateExists):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(bpac.BpacError)
    async def _bpac_error(_, exc: bpac.BpacError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(httpx.HTTPError)
    async def _http_error(_, exc: httpx.HTTPError):
        return JSONResponse(status_code=502, content={"detail": f"Part-DB nicht erreichbar: {exc}"})

    # ------------------------------------------------------------- Allgemein
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta():
        return {
            "kategorien": [vars(c) for c in cfg.categories.values()],
            "farben": [{"name": n, "hex": h} for n, h in cfg.colors.items()],
            "boxtypen": [vars(b) for b in cfg.box_types.values()],
            "ausgabe": str(cfg.output_dir),
            "tag": cfg.label_tag,
            "box_vorlage": cfg.box_template,
            "partdb": cfg.partdb_url,
            "bpac_vorschau": vorlagen.preview_available(printer),
            "installer_verfuegbar": routes_helfer_download.installer_info(cfg)["installer_verfuegbar"],
            "vorschau_dpi": cfg.preview_dpi,
        }

    # Geschriebene CSVs zum Herunterladen: eine Datei direkt, mehrere als ZIP (Browser blocken sonst
    # mehrere automatische Downloads). Nur *.csv direkt im Ausgabeordner – keine Pfade.
    @app.get("/api/ausgabe/download")
    def download_output(datei: list[str] = Query(...)):
        files = []
        for name in dict.fromkeys(datei):
            if Path(name).name != name or not name.lower().endswith(".csv") or name.startswith("."):
                raise HTTPException(400, f"Ungültiger Dateiname: {name}")
            path = cfg.output_dir / name
            if not path.is_file():
                raise HTTPException(404, f"{name} gibt es im Ausgabeordner nicht (mehr).")
            files.append(path)
        if len(files) == 1:
            return FileResponse(files[0], media_type="text/csv", filename=files[0].name,
                                headers={"Cache-Control": "no-store"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path in files:
                z.write(path, path.name)
        name = f"Labels-{datetime.now():%Y%m%d-%H%M%S}.zip"
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.post("/api/open-output")
    def open_output():
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            desktop.open_folder(cfg.output_dir)
        except TemplateError:
            raise
        except Exception as e:  # pragma: no cover
            raise HTTPException(500, f"Ordner konnte nicht geöffnet werden: {e}")
        return {"ok": True}

    # ------------------------------------------------------------- P-touch-Vorlagen
    def template_target(box_type: str | None) -> tuple[str, Path]:
        """(.lbx-Name, CSV-Pfad) für das Box-Label (None) oder eine Lagerart."""
        if box_type is None:
            return cfg.box_template, cfg.output_dir / cfg.name_label_file
        bt = cfg.box_types.get(box_type)
        if bt is None:
            raise HTTPException(400, f"Unbekannte Lagerart '{box_type}'.")
        return bt.lbx, cfg.output_dir / cfg.template_for(box_type).file

    def open_template(box_type: str | None) -> str:
        lbx, csv_path = template_target(box_type)
        if not lbx:
            what = "das Box-Label (labels.box_vorlage in config.yaml)" if box_type is None \
                else f"Lagerart '{box_type}' (Reiter Stammdaten)"
            raise TemplateError(f"Keine P-touch-Vorlage für {what} eingetragen.")
        if not csv_path.exists():
            raise TemplateError(f"{csv_path.name} wurde noch nicht geschrieben.")
        return str(desktop.open_linked(cfg.template_dir / lbx, csv_path))

    # ------------------------------------------------------------- Direktdruck (b-PAC, optional)
    def tape_info(templates) -> dict:
        """Band der Vorlagen und eingelegtes Band. passt: True/False, None = eingelegtes Band unbekannt."""
        needed: dict[float, list[str]] = {}
        for t in dict.fromkeys(templates):
            try:
                tape = lbx_mod.parse(cfg.template_dir / t).tape_mm
            except Exception:
                tape = None
            needed.setdefault(tape, []).append(t)
        band = None
        if isinstance(printer, helfer_server.ModePrinter):
            try:
                band = (app.state.hooks.helper_status().get("drucker") or {}).get("band")
            except Exception:
                band = None
        try:
            loaded = float(str(band).split()[0].replace(",", ".")) if band else None
        except ValueError:
            loaded = None
        known = [t for t in needed if t]
        passt = None if loaded is None or not known else all(abs(t - loaded) <= 0.5 for t in known)
        return {"baender": [{"band_mm": t, "vorlagen": v} for t, v in needed.items()],
                "eingelegt": band, "eingelegt_mm": loaded, "passt": passt}

    def check_tape(templates) -> None:
        """Eingelegtes Band (vom Status des Helfers bzw. lokal) mit den Vorlagen vergleichen – bei
        Abweichung 409, bevor der Auftrag zum Drucker geht. Unbekanntes Band: nicht prüfen.
        Nur für den echten Drucker (lokal/Helfer) – nicht für übergebene Drucker-Objekte (Tests)."""
        info = tape_info(templates)
        if info["passt"] is not False:
            return
        wrong = [b for b in info["baender"] if b["band_mm"] and abs(b["band_mm"] - info["eingelegt_mm"]) > 0.5]
        names = sorted(v for b in wrong for v in b["vorlagen"])
        need = ", ".join(f"{b['band_mm']:g} mm" for b in wrong)
        raise HTTPException(409, f"Falsches Band eingelegt: {info['eingelegt']} – {', '.join(names)} braucht {need}. "
                                 f"Bitte Band wechseln, es wurde nichts gedruckt.")

    def require_direct_print() -> int:
        if not cfg.direct_print:
            raise HTTPException(400, "Direktdruck ist aus – in config.yaml unter druck: direktdruck: true setzen.")
        return bpac.print_flags(cfg.cut_options, getattr(cfg, "print_quality", "standard"))

    @app.get("/api/print-status")
    def print_status():
        if not cfg.direct_print:
            return {"aktiv": False, "ok": False, "grund": "Direktdruck ist in config.yaml ausgeschaltet.", "drucker": []}
        return dict(printer.status(cfg.printer), aktiv=True, gewaehlt=cfg.printer)

    @app.post("/api/templates/open")
    def open_template_endpoint(req: OpenTemplate):
        return {"vorlage": open_template(req.box_type)}

    # ------------------------------------------------------------- Vorlagen prüfen, Vorschau, bearbeiten
    bpac_errors = tuple({bpac.BpacError, getattr(printer, "BpacError", bpac.BpacError)})

    def load_box(client: PartDB, box_id: int | None) -> Box:
        if box_id is None:
            raise HTTPException(400, "Für die Quelle 'box' bitte eine Box auswählen (box_id).")
        box = load_boxes(client).get(box_id)
        if box is None:
            raise HTTPException(404, f"Box {box_id} gibt es in Part-DB nicht.")
        return box

    def sample(datei: str, quelle: str, box_id: int | None) -> tuple[Path, list[str], list[dict]]:
        """(Vorlage, Spalten, Zeilen) mit Beispieldaten aus der gewählten Quelle."""
        path = vorlagen.template_path(cfg, datei)
        tpl = vorlagen.read_template(path)
        quelle = vorlagen.check_source(quelle)
        if quelle == "vorlage":
            return path, *vorlagen.sample_from_template(cfg, tpl)
        if quelle == "box":
            with api() as c:
                return path, *vorlagen.sample_from_box(c, cfg, tpl, load_box(c, box_id))
        real = None
        try:   # echte lange Namen sind nett, aber Part-DB darf hier fehlen
            with api() as c:
                real = vorlagen.real_values(c)
        except (PartDBError, httpx.HTTPError):
            pass
        return path, *vorlagen.sample_worst(cfg, tpl, real)

    def preview_response(template: Path, row: dict) -> Response:
        content, media, kind = vorlagen.render_preview(printer, template, row, bpac_errors)
        return Response(content, media_type=media, headers={"X-Vorschau": kind})

    @app.get("/api/vorlagen")
    def templates_overview():
        out = vorlagen.overview(cfg, vorlagen.preview_available(printer))
        # Quelle der Vorlagen: im Helfer-Modus mit Vorlagen-Ordner der Ordner auf dem Helfer-PC
        quelle = {"modus": helfer_server.print_mode(cfg), "rechner": None, "ordner": str(cfg.template_dir),
                  "abgeglichen": None, "fehler": None, "hinweis": None}
        if quelle["modus"] == "helfer":
            st = hub.status()["helfer"]
            v = st["vorlagen"]
            if v["ordner"]:
                quelle.update(rechner=hub.sync.state.get("rechner") or st.get("rechner"), ordner=v["ordner"])
            quelle.update(abgeglichen=v["abgeglichen"], fehler=v["fehler"], hinweis=v["hinweis"])
        out["quelle"] = quelle
        return out

    @app.post("/api/vorlagen/kopie")
    def template_copy(req: TemplateCopy):
        bt = None
        if req.lagerart:
            bt = cfg.box_types.get(req.lagerart)
            if bt is None:
                raise HTTPException(400, f"Unbekannte Lagerart '{req.lagerart}'.")
        hub.sync.protect(req.name.strip())   # Abgleich soll die neue Datei nicht als „gelöscht“ werten
        target = vorlagen.copy_template(cfg, req.quelle, req.name)
        push = getattr(desktop, "new_template", None)
        if push is not None:     # Helfer mit Vorlagen-Ordner: Kopie zuerst in den PC-Ordner
            try:
                push(target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        if bt is not None:   # über die Stammdaten, damit es rückgängig gemacht werden kann
            master.save_box_type(bt.name, BoxType(name=bt.name, compartments=bt.compartments,
                                                  file=bt.file, lbx=target.name))
        path, cols, rows = sample(target.name, "worst", None)
        opened = vorlagen.edit_original(cfg, path, cols, rows, desktop)
        return {"datei": target.name, "lagerart": bt.name if bt else None,
                "vorlage": opened["vorlage"], "csv": opened["csv"]}

    @app.get("/api/vorlagen/{datei}")
    def template_detail(datei: str):
        return vorlagen.detail(cfg, vorlagen.read_template(vorlagen.template_path(cfg, datei)))

    @app.get("/api/vorlagen/{datei}/beispiel")
    def template_sample(datei: str, quelle: str = "worst", box_id: int | None = None):
        _, cols, rows = sample(datei, quelle, box_id)
        return {"spalten": cols, "zeilen": rows, "quelle": quelle}

    @app.get("/api/vorlagen/{datei}/vorschau")
    def template_preview_get(datei: str, quelle: str = "worst", box_id: int | None = None):
        path, _, rows = sample(datei, quelle, box_id)
        return preview_response(path, rows[0])

    @app.post("/api/vorlagen/{datei}/vorschau")
    def template_preview(datei: str, req: PreviewRow):
        return preview_response(vorlagen.template_path(cfg, datei), req.zeile)

    @app.post("/api/vorlagen/{datei}/bearbeiten")
    def template_edit(datei: str, req: SampleSource):
        path, cols, rows = sample(datei, req.quelle, req.box_id)
        return vorlagen.edit_original(cfg, path, cols, rows, desktop)

    @app.get("/api/name-labels/{box_id}/vorschau")
    def name_label_preview(box_id: int):
        if not cfg.box_template:
            raise HTTPException(400, "Keine Box-Label-Vorlage (labels.box_vorlage) eingetragen.")
        path = vorlagen.template_path(cfg, cfg.box_template)
        with api() as c:
            box = load_box(c, box_id)
        return preview_response(path, name_label_rows([box])[0])

    @app.get("/api/content-labels/{box_id}/vorschau")
    def content_label_preview(box_id: int):
        with api() as c:
            box = load_box(c, box_id)
            bt = cfg.box_types.get(box.box_type)
            if bt is None or not bt.lbx:
                what = f"Lagerart '{box.box_type}'" if box.box_type else "Box ohne Lagerart"
                raise HTTPException(400, f"Keine P-touch-Vorlage für {what} – im Reiter Stammdaten eintragen.")
            path = vorlagen.template_path(cfg, bt.lbx)
            row = content_label_for_box(c, cfg, box).row(cfg)
        return preview_response(path, row)

    # ------------------------------------------------------------- Stammdaten
    @app.get("/api/stammdaten")
    def master_info():
        return master.info()

    @app.post("/api/stammdaten/kategorien")
    def add_category(req: CategoryIn):
        master.save_category(None, Category(**req.model_dump()))
        return master.info()

    @app.put("/api/stammdaten/kategorien/{key}")
    def edit_category(key: str, req: CategoryIn):
        master.save_category(key, Category(**req.model_dump()))
        return master.info()

    @app.delete("/api/stammdaten/kategorien/{key}")
    def delete_category(key: str):
        master.delete_category(key)
        return master.info()

    @app.post("/api/stammdaten/farben")
    def add_color(req: ColorIn):
        master.save_color(None, req.name, req.hex)
        return master.info()

    @app.put("/api/stammdaten/farben/{name}")
    def edit_color(name: str, req: ColorIn):
        master.save_color(name, req.name, req.hex)
        return master.info()

    @app.delete("/api/stammdaten/farben/{name}")
    def delete_color(name: str):
        master.delete_color(name)
        return master.info()

    @app.post("/api/stammdaten/lagerarten")
    def add_box_type(req: BoxTypeIn):
        master.save_box_type(None, BoxType(**req.model_dump()))
        return master.info()

    @app.put("/api/stammdaten/lagerarten/{name}")
    def edit_box_type(name: str, req: BoxTypeIn):
        master.save_box_type(name, BoxType(**req.model_dump()))
        return master.info()

    @app.delete("/api/stammdaten/lagerarten/{name}")
    def delete_box_type(name: str):
        master.delete_box_type(name)
        return master.info()

    @app.post("/api/stammdaten/undo")
    def undo_master():
        text = master.undo_last()
        return dict(master.info(), rueckgaengig_gemacht=text)

    # ------------------------------------------------------------- Boxen
    @app.get("/api/boxes")
    def list_boxes():
        with api() as c:
            boxes = sorted(load_boxes(c).values(), key=lambda b: b.name.lower())
        queued = set(state.data["name_queue"])
        return [dict(b.to_dict(), in_queue=b.id in queued) for b in boxes]

    @app.get("/api/boxes/preview")
    def preview_name(abbr: str, compartments: int, count: int = 1):
        if abbr not in cfg.categories:
            raise HTTPException(400, f"Unbekanntes Kürzel '{abbr}'")
        with api() as c:
            names = [l.get("name", "") for l in c.storage_locations()]
        start = next_number(abbr, names)
        cat = cfg.categories[abbr]
        return {"names": [make_name(abbr, compartments, start + i) for i in range(count)],
                "color": cat.color, "color_hex": cat.color_hex}

    @app.post("/api/boxes")
    def create_boxes(req: NewBox):
        cat = cfg.categories.get(req.abbr)
        if not cat:
            raise HTTPException(400, f"Unbekanntes Kürzel '{req.abbr}' – in config.yaml unter kategorien eintragen.")
        if req.box_type not in cfg.box_types:
            raise HTTPException(400, f"Unbekannte Lagerart '{req.box_type}'.")
        color = cat.color
        if req.color and req.color.strip():
            color = cfg.color_name(req.color)
            if not color:
                raise HTTPException(400, f"Unbekannte Boxfarbe '{req.color}' – unter Stammdaten > Boxfarben anlegen.")
        created = []
        with api() as c:
            names = [l.get("name", "") for l in c.storage_locations()]
            number = next_number(req.abbr, names)
            for i in range(req.count):
                name = make_name(req.abbr, req.compartments, number + i)
                loc = c.create_storage_location(
                    name, build_comment(cat.name, color, req.compartments, req.box_type), req.parent_id)
                created.append(parse_box(loc, cfg))
        state.queue_names([b.id for b in created])
        return {"created": [b.to_dict() for b in created], "color": color, "color_hex": cfg.hex_for(color)}

    # ------------------------------------------------------------- Namenslabels (12 mm)
    @app.get("/api/name-labels")
    def name_queue():
        with api() as c:
            boxes = load_boxes(c)
        queue = state.data["name_queue"]
        missing = [i for i in queue if i not in boxes]
        return {"boxes": [boxes[i].to_dict() for i in queue if i in boxes],
                "missing": missing, "last_export": state.data["last_name_export"]}

    @app.post("/api/name-labels/queue")
    def add_to_queue(req: IdList):
        state.queue_names(req.ids)
        return {"queue": state.data["name_queue"]}

    @app.delete("/api/name-labels/queue/{lid}")
    def remove_from_queue(lid: int):
        state.unqueue_name(lid)
        return {"queue": state.data["name_queue"]}

    @app.post("/api/name-labels/export")
    def export_names(opts: ExportOptions | None = None):
        with api() as c:
            boxes = load_boxes(c)
        selected = [boxes[i] for i in state.data["name_queue"] if i in boxes]
        if not selected:
            raise HTTPException(400, "Keine Boxen in der Warteschlange.")
        path = write_csv(cfg.output_dir / cfg.name_label_file, NAME_COLUMNS, name_label_rows(selected), cfg)
        state.set("last_name_export", [b.id for b in selected])
        result = {"datei": str(path), "labels": len(selected), "boxen": [b.name for b in selected]}
        if opts and opts.open_template:
            try:   # CSV ist geschrieben – ein Vorlagenproblem soll das nicht als Fehler melden
                result["vorlage"] = open_template(None)
            except TemplateError as e:
                result["vorlage_fehler"] = str(e)
        return result

    @app.post("/api/name-labels/print")
    def print_names(opts: NamePrint | None = None):
        opts = opts or NamePrint()
        flags = require_direct_print()
        if not cfg.box_template:
            raise HTTPException(400, "Keine Box-Label-Vorlage (labels.box_vorlage) eingetragen.")
        with api() as c:
            boxes = load_boxes(c)
        queue = state.data["name_queue"]
        if opts.ids is not None:
            queue = [i for i in queue if i in set(opts.ids)]
        selected = [boxes[i] for i in queue if i in boxes]
        if not selected:
            raise HTTPException(400, "Keine Boxen in der Warteschlange." if opts.ids is None
                                else "Die gewählten Boxen sind nicht (mehr) in der Warteschlange.")
        if not opts.band_gewechselt:
            check_tape([cfg.box_template])
        n = printer.print_labels(cfg.template_dir / cfg.box_template,
                                 copies(name_label_rows(selected), opts.kopien), cfg.printer, flags)
        ids = [b.id for b in selected]
        if opts.ids is not None:     # Nachdruck fehlender: „Gedruckt ✓“ gilt weiter für den ganzen Druck
            ids = list(dict.fromkeys(state.data["last_name_export"] + ids))
        state.set("last_name_export", ids)
        return {"gedruckt": n, "boxen": [b.name for b in selected],
                "labels": [{"id": b.id, "name": b.name} for b in selected]}

    @app.post("/api/druck/pruefen")
    def print_check(req: PrintCheck):
        """Vor dem Direktdruck: welches Band brauchen die Vorlagen, welches ist eingelegt, wie viele Labels."""
        if req.art == "box":
            if not cfg.box_template:
                raise HTTPException(400, "Keine Box-Label-Vorlage (labels.box_vorlage) eingetragen.")
            queue = state.data["name_queue"]
            if req.ids:
                queue = [i for i in queue if i in set(req.ids)]
            labels, templates, ohne = len(queue), [cfg.box_template], []
        elif req.art in ("bauteile", "nachdruck"):
            if req.art == "bauteile":
                labs = _content_labels()
            else:
                with api() as c:
                    boxes = load_boxes(c)
                labs = [type("L", (), {"box": boxes[i]}) for i in dict.fromkeys(req.ids) if i in boxes]
            templates, ohne = [], []
            for l in labs:
                bt = cfg.box_types.get(l.box.box_type)
                (templates.append(bt.lbx) if bt and bt.lbx else ohne.append(l.box.name))
            labels = len(templates)
        else:
            raise HTTPException(400, f"Unbekannte Druckart '{req.art}'.")
        return dict(tape_info(templates), labels=labels, ohne_vorlage=ohne)

    @app.get("/api/druck/warteschlange")
    def print_queue_status():
        """Windows-Warteschlange nach dem Direktdruck: läuft der Auftrag noch, meldet der Drucker einen Fehler
        (z. B. Band leer)? unterstuetzt=False, wenn das nicht abfragbar ist (kein Windows, alter Helfer)."""
        if not cfg.direct_print:
            return {"unterstuetzt": False}
        queue = getattr(printer, "queue", None)
        if queue is None:
            return {"unterstuetzt": False}
        try:
            q = queue(cfg.printer)
        except Exception as e:      # Helfer weg o. Ä. – die Druckkontrolle zeigt das nur an
            return {"unterstuetzt": False, "grund": str(e)}
        return dict(q, unterstuetzt=True) if q else {"unterstuetzt": False}

    @app.post("/api/name-labels/done")
    def names_done():
        done = set(state.data["last_name_export"])
        state.set("name_queue", [i for i in state.data["name_queue"] if i not in done])
        state.set("last_name_export", [])
        return {"erledigt": len(done)}

    # ------------------------------------------------------------- Inhaltslabels (24 mm)
    def _content_labels():
        with api() as c:
            boxes = load_boxes(c)
            return build_content_labels(c, cfg, boxes)

    def _label_dict(l) -> dict:
        return {
            "box": l.box.to_dict(),
            "vorlage": l.template_file,
            "felder": l.slots,
            "slots": [None if not g else {
                          "name": cfg.part_separator.join(p.name for p in g),
                          "footprint": join_unique((p.footprint for p in g), cfg.part_separator),
                          "fach": g[0].compartment, "tag": any(p.tagged for p in g), "anzahl": len(g)}
                      for g in l.ordered_slots()],
            "row": l.row(cfg),
            "warnungen": l.warnings,
        }

    def _print_content(labels, flags: int, kopien: int = 1,
                       band_gewechselt: bool = False) -> tuple[list[dict], list[str], list]:
        """Direktdruck je Vorlage. -> (gedruckt, übersprungen, gedruckte Labels)."""
        by_template: dict[str, list] = {}
        skipped = []
        for l in labels:
            bt = cfg.box_types.get(l.box.box_type)
            if bt and bt.lbx:
                by_template.setdefault(bt.lbx, []).append(l)
            else:
                skipped.append(f"{l.box.name} ({l.box.box_type or 'ohne Lagerart'})")
        printed, done = [], []
        if not by_template:
            return printed, skipped, done
        # Alle Vorlagen in EINEM Druckauftrag (ein Vorlauf, Autocut dazwischen) – je Bandbreite,
        # denn eine Vorlage mit anderer Bandbreite beendet den Auftrag in b-PAC ohnehin.
        by_tape: dict[object, list] = {}
        for lbx in by_template:
            try:
                tape = lbx_mod.parse(cfg.template_dir / lbx).tape_mm
            except Exception:
                tape = None
            by_tape.setdefault(tape, []).append(lbx)
        batch = getattr(printer, "print_batch", None)
        if not band_gewechselt:     # nach bestätigtem Wechsel prüft der Helfer das Band selbst (live)
            check_tape([lbx for names in by_tape.values() for lbx in names])
        for names in by_tape.values():
            jobs = [(cfg.template_dir / lbx, copies([l.row(cfg) for l in by_template[lbx]], kopien))
                    for lbx in names]
            if batch is not None:
                batch(jobs, cfg.printer, flags)
            else:   # Drucker-Objekte ohne Sammeldruck (z. B. Test-Attrappen)
                for path, rows in jobs:
                    printer.print_labels(path, rows, cfg.printer, flags)
            for lbx in names:
                printed.append({"vorlage": lbx, "labels": len(by_template[lbx]) * max(1, kopien)})
                done.extend(by_template[lbx])
        return printed, skipped, done

    def label_list(done) -> list[dict]:
        """Gedruckte Bauteil-Labels für die Druckkontrolle – Nachdruck fehlender über die Box-ID."""
        return [{"id": l.box.id, "name": l.box.name} for l in done]

    @app.get("/api/content-labels")
    def content_preview():
        return [_label_dict(l) for l in _content_labels()]

    # Nachdruck: Box suchen und ihr Bauteil-Label drucken – unabhängig vom Tag, Tags bleiben unberührt
    @app.get("/api/content-labels/suche")
    def content_search(q: str = "", limit: int = 30):
        if not q.strip():
            return {"treffer": [], "anzahl": 0}
        with api() as c:
            boxes = load_boxes(c)
            by_location, _ = parts_by_location(c, cfg)
        hits = search_boxes(boxes, by_location, q, limit=max(1, min(limit, 100)))
        return {"anzahl": len(hits), "treffer": [
            dict(_label_dict(make_content_label(cfg, b, by_location.get(b.id, []))),
                 passende_bauteile=[p.name for p in matched])
            for b, matched in hits]}

    @app.post("/api/content-labels/nachdruck")
    def content_reprint(req: Reprint):
        flags = require_direct_print() if req.direkt else 0
        if not req.direkt and state.data["last_content_export"]:
            # Die CSVs würden den offenen Export überschreiben – danach entfernte "Gedruckt – Tags entfernen"
            # Tags von Bauteilen, deren Label nie gedruckt wurde.
            raise HTTPException(409, "Es gibt einen offenen Export mit getaggten Bauteilen. Erst drucken und "
                                     "„Gedruckt – Tags entfernen“ bestätigen – oder den Nachdruck direkt drucken.")
        with api() as c:
            boxes = load_boxes(c)
            by_location, _ = parts_by_location(c, cfg)
        missing = [i for i in req.ids if i not in boxes]
        if missing:
            raise HTTPException(404, f"Box(en) nicht (mehr) in Part-DB: {', '.join(map(str, missing))}")
        labels = [make_content_label(cfg, boxes[i], by_location.get(i, [])) for i in dict.fromkeys(req.ids)]
        if req.direkt:
            printed, skipped, done = _print_content(labels, flags, req.kopien, req.band_gewechselt)
            return {"gedruckt": printed, "uebersprungen": skipped, "labels": label_list(done)}
        return {"dateien": export_content_labels(labels, cfg), "labels": len(labels)}

    @app.post("/api/content-labels/export")
    def content_export():
        labels = _content_labels()
        if not labels:
            raise HTTPException(400, f"Keine Bauteile mit Tag '{cfg.label_tag}' gefunden.")
        written = export_content_labels(labels, cfg)
        part_ids = sorted({pid for l in labels for pid in l.tagged_part_ids})
        state.set("last_content_export", part_ids)
        return {"dateien": written, "bauteile": len(part_ids)}

    @app.post("/api/content-labels/print")
    def content_print(opts: PrintOptions | None = None):
        opts = opts or PrintOptions()
        flags = require_direct_print()
        labels = _content_labels()
        if not labels:
            raise HTTPException(400, f"Keine Bauteile mit Tag '{cfg.label_tag}' gefunden.")
        printed, skipped, done = _print_content(labels, flags, opts.kopien, opts.band_gewechselt)
        part_ids = {pid for l in done for pid in l.tagged_part_ids}
        # Tags werden erst nach "Gedruckt – Tags entfernen" entfernt, nur für tatsächlich gedruckte Labels
        state.set("last_content_export", sorted(part_ids))
        return {"gedruckt": printed, "uebersprungen": skipped, "bauteile": len(part_ids), "labels": label_list(done)}

    @app.post("/api/content-labels/done")
    def content_done():
        """Tag bei genau den Bauteilen entfernen, die im letzten Export waren."""
        ids = state.data["last_content_export"]
        if not ids:
            raise HTTPException(400, "Kein offener Export.")
        removed, failed = 0, []
        with api() as c:
            for pid in ids:
                try:
                    part = c.get(f"/parts/{pid}")
                    tags = str(part.get("tags") or "")
                    if has_tag(tags, cfg.label_tag):
                        c.set_part_tags(pid, remove_tag(tags, cfg.label_tag))
                        removed += 1
                except PartDBError as e:
                    failed.append({"id": pid, "fehler": str(e)})
        state.set("last_content_export", [f["id"] for f in failed])
        return {"entfernt": removed, "fehler": failed}

    routes_einstellungen.register(app, cfg, app.state.hooks, transport=transport)
    routes_helfer_download.register(app, cfg, app.state.hooks)
    return app
