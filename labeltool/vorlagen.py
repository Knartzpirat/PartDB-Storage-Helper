"""P-touch-Vorlagen (.lbx) prüfen, mit Beispieldaten füllen, als Vorschau zeigen und bearbeiten.

Zum Bearbeiten wird die ORIGINAL-Vorlage im Vorlagenordner mit einer Beispiel-CSV
verknüpft (nur databasePath/mergeTable/dbTable ändern sich), damit der Nutzer sie im
P-touch Editor mit realistischen Daten sieht, ändert und speichert. Gedruckt wird ohnehin
über verknüpfte Kopien (ptouch.linked_copy), das Umhängen des Originals stört also nicht.
Vor dem ersten Umhängen wird die Originaldatei unter .backup/ gesichert.
"""
from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from itertools import cycle, islice
from pathlib import Path
from typing import Callable

from . import lbx
from .config import Config
from .desktop import BACKUP_DIR, Desktop, as_desktop, relink_in_place, valid_name  # noqa: F401
from .desktop import backup_once as _backup_once
from .labels import NAME_COLUMNS, content_columns, content_label_for_box, name_label_rows, write_csv
from .model import Box
from .partdb import PartDB
from .ptouch import TemplateError, _relink

SAMPLE_DIR = "Beispiele"    # im Ausgabeordner: Beispiel-CSVs zum Bearbeiten
SOURCES = ("worst", "vorlage", "box")

_SECTION_RE = re.compile(r"^(?:Section|Footprint)(\d+)$")

# Bewusst lange, realistische Werte – zeigen, ob auf dem Label etwas abgeschnitten wird
WORST_PARTS = ["TPS63060DSCR-Q1", "STM32F103C8T6TR", "LM2596S-ADJ/NOPB", "MCP73831T-2ACI/OT",
               "AMS1117-3.3-SOT223", "ESP32-C3-MINI-1-N4", "SN74LVC1G125DBVR", "IRLML6402TRPBF"]
WORST_FOOTPRINTS = ["VSON-10-EP 3x3mm", "LQFP-48 7x7mm", "TO-263-5 (D2PAK)", "SOT-23-5",
                    "SOT-223-3", "QFN-32-EP 5x5mm", "SOT-23-5", "SOT-23-3"]
WORST_CATEGORY = "Spannungsregler - Schalten"
WORST_BOX_ID = 1234


class TemplateNotFound(TemplateError):
    """-> HTTP 404"""


class TemplateExists(TemplateError):
    """-> HTTP 409"""


# ------------------------------------------------------------------ Dateien
def check_name(name: str) -> str:
    """Nur ein Dateiname mit Endung .lbx – keine Ordner, kein Traversal."""
    name = (name or "").strip()
    if not valid_name(name):
        raise TemplateError(f"Ungültiger Vorlagenname '{name}' – nur ein Dateiname mit Endung .lbx, ohne Ordner.")
    return name


def template_path(cfg: Config, name: str) -> Path:
    path = cfg.template_dir / check_name(name)
    if path.resolve().parent != cfg.template_dir.resolve() or not path.is_file():
        raise TemplateNotFound(f"Vorlage '{name}' gibt es im Vorlagenordner nicht.")
    return path


def read_template(path: Path) -> lbx.LbxTemplate:
    try:
        return lbx.parse(path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, AttributeError, ValueError) as e:
        raise TemplateError(f"{path.name} kann nicht gelesen werden (keine gültige P-touch-Vorlage?): {e}")


def list_templates(cfg: Config) -> list[Path]:
    if not cfg.template_dir.is_dir():
        return []
    return sorted(cfg.template_dir.glob("*.lbx"), key=lambda p: p.name.lower())


# ------------------------------------------------------------------ Prüfungen
def users(cfg: Config, name: str) -> list[str]:
    """Lagerarten, die diese Vorlage für das Bauteil-Label nutzen."""
    return [b.name for b in cfg.box_types.values() if b.lbx == name]


def is_box_label(cfg: Config, name: str) -> bool:
    return bool(cfg.box_template) and name == cfg.box_template


def section_numbers(tpl: lbx.LbxTemplate) -> list[int]:
    """Nummern der Section-Felder (Bauteilnamen) der Vorlage."""
    return sorted({int(m.group(1)) for f in tpl.fields if (m := re.fullmatch(r"Section(\d+)", f))})


def slots_for(cfg: Config, tpl: lbx.LbxTemplate) -> int:
    """Bauteilfelder für Beispieldaten: so viele, wie Vorlage oder Lagerarten brauchen."""
    used = [int(m.group(1)) for f in tpl.fields if (m := _SECTION_RE.match(f))]
    comps = [cfg.box_types[n].compartments for n in users(cfg, tpl.file)]
    return max(used + comps + [1])


def columns_for(cfg: Config, tpl: lbx.LbxTemplate) -> list[str]:
    """CSV-Spalten, die das Tool für diese Vorlage liefert."""
    if is_box_label(cfg, tpl.file):
        return list(NAME_COLUMNS)
    return content_columns(slots_for(cfg, tpl))


def warnings(cfg: Config, tpl: lbx.LbxTemplate) -> list[str]:
    name = tpl.file
    out: list[str] = []
    if not tpl.database:
        out.append("Keine Datenbank-Verknüpfung – im P-touch Editor einmal Datei > Datenbank > "
                   "Verbinden mit einer CSV ausführen und speichern.")
    used_by = users(cfg, name)
    box_label = is_box_label(cfg, name)
    if not used_by and not box_label:
        out.append("Wird von keiner Lagerart genutzt und ist nicht das Box-Label.")
    if box_label and used_by:
        out.append(f"Ist das Box-Label, wird aber auch als Bauteil-Vorlage genutzt von: {', '.join(used_by)}.")
    sections = section_numbers(tpl)
    for bt_name in used_by:
        comps = cfg.box_types[bt_name].compartments
        if sections and sections[-1] > comps:
            out.append(f"Nutzt Section{sections[-1]}, aber Lagerart '{bt_name}' hat nur {comps} "
                       f"Abteil{'e' if comps != 1 else ''} – Feld bleibt leer.")
        if comps > len(sections):
            out.append(f"Lagerart '{bt_name}' hat {comps} Abteile, die Vorlage aber nur {len(sections)} "
                       f"Section-Feld{'er' if len(sections) != 1 else ''} – weitere Bauteile fehlen auf dem Label.")
    unknown = [f for f in tpl.fields if f not in columns_for(cfg, tpl)]
    if unknown:
        out.append(f"Nutzt Spalten, die das Tool nicht liefert: {', '.join(unknown)}.")
    return out


def info(cfg: Config, tpl: lbx.LbxTemplate) -> dict:
    return {"lagerarten": users(cfg, tpl.file), "box_label": is_box_label(cfg, tpl.file),
            "warnungen": warnings(cfg, tpl)}


def overview(cfg: Config, preview: bool) -> dict:
    templates = []
    for path in list_templates(cfg):
        try:
            tpl = read_template(path)
        except TemplateError as e:
            templates.append({"datei": path.name, "band_mm": None, "laenge_mm": None, "felder": [],
                              "lagerarten": users(cfg, path.name), "box_label": is_box_label(cfg, path.name),
                              "warnungen": [str(e)]})
            continue
        templates.append(dict(datei=path.name, band_mm=tpl.tape_mm, laenge_mm=tpl.length_mm,
                              felder=tpl.fields, **info(cfg, tpl)))
    present = {p.name for p in list_templates(cfg)}
    return {
        "ordner": str(cfg.template_dir),
        "bpac_vorschau": preview,
        "vorlagen": templates,
        # zusätzlich: Lagerarten ohne bzw. mit fehlender Vorlage
        "ohne_vorlage": [b.name for b in cfg.box_types.values() if not b.lbx],
        "fehlende_dateien": [{"lagerart": b.name, "datei": b.lbx} for b in cfg.box_types.values()
                             if b.lbx and b.lbx not in present],
    }


def detail(cfg: Config, tpl: lbx.LbxTemplate) -> dict:
    return dict(tpl.to_dict(), **info(cfg, tpl))


# ------------------------------------------------------------------ Beispieldaten
def check_source(source: str) -> str:
    if source not in SOURCES:
        raise TemplateError(f"Unbekannte Quelle '{source}' – möglich: {', '.join(SOURCES)}.")
    return source


def sample_from_template(cfg: Config, tpl: lbx.LbxTemplate) -> tuple[list[str], list[dict]]:
    """Die in der Vorlage gespeicherten Texte je verknüpftem Feld."""
    cols = columns_for(cfg, tpl)
    row = {c: "" for c in cols}
    for o in tpl.objects:
        if o.field and o.text and not row.get(o.field):
            row[o.field] = o.text
    return cols, [row]


def real_values(api: PartDB) -> dict[str, list[str]]:
    """Echte Bauteilnamen, Kategorien und Footprints aus Part-DB (für den Worst Case)."""
    return {
        "teile": [str(p.get("name") or "") for p in api.parts()],
        "kategorien": [str(o.get("name") or "") for o in api.lookup("categories").values()],
        "footprints": [str(o.get("name") or "") for o in api.lookup("footprints").values()],
    }


def _longest(values: list[str], n: int) -> list[str]:
    """Die n längsten verschiedenen Werte, bei Bedarf wiederholt."""
    unique = sorted({v.strip() for v in values if v and v.strip()}, key=lambda v: (-len(v), v))
    return list(islice(cycle(unique), n)) if unique else [""] * n


def sample_worst(cfg: Config, tpl: lbx.LbxTemplate,
                 real: dict[str, list[str]] | None = None) -> tuple[list[str], list[dict]]:
    """Bewusst lange Werte je Spalte. Echte Werte aus Part-DB fließen mit ein, wenn sie länger sind."""
    real = real or {}
    cols = columns_for(cfg, tpl)
    abbr = _longest(list(cfg.categories) or ["SMDICs"], 1)[0]
    content = _longest([c.name for c in cfg.categories.values()] or ["Microcontroller"], 1)[0]
    color = _longest(list(cfg.colors) or ["Dunkelgrau"], 1)[0]
    box_type = _longest(list(cfg.box_types) or ["WinBin12"], 1)[0]
    location = f"{abbr}-12.0123"            # zweistellige Abteile
    barcode = f"S{WORST_BOX_ID}"
    if is_box_label(cfg, tpl.file):
        row = {"ID": WORST_BOX_ID, "Lagername": location, "Barcodeinhalt": barcode, "Lagerinhalt": content,
               "Lagerabteile": "12", "Lagerfarbe": color, "Lagerart": box_type, "Lager-ID": WORST_BOX_ID}
        return cols, [row]
    n = slots_for(cfg, tpl)
    names = _longest(WORST_PARTS + real.get("teile", []), n)
    footprints = _longest(WORST_FOOTPRINTS + real.get("footprints", []), n)
    row = {c: "" for c in cols}
    row.update({"Location": location,
                "Category": _longest([WORST_CATEGORY] + real.get("kategorien", []), 1)[0],
                "Barcode": barcode, "Lagerfarbe": color, "Lagerinhalt": content})
    for i in range(n):
        row[f"Section{i + 1}"] = names[i]
        row[f"Footprint{i + 1}"] = footprints[i]
    return cols, [row]


def sample_from_box(api: PartDB, cfg: Config, tpl: lbx.LbxTemplate, box: Box) -> tuple[list[str], list[dict]]:
    """Echte Box: Box-Label oder Bauteil-Label mit ALLEN Bauteilen der Box (unabhängig vom Tag)."""
    if is_box_label(cfg, tpl.file):
        return list(NAME_COLUMNS), name_label_rows([box])
    label = content_label_for_box(api, cfg, box)
    cols = content_columns(max(slots_for(cfg, tpl), label.slots))
    row = label.row(cfg)
    return cols, [{c: row.get(c, "") for c in cols}]


# ------------------------------------------------------------------ Vorschau
def render_preview(printer, template: Path, row: dict, bpac_errors: tuple) -> tuple[bytes | str, str, str]:
    """(Inhalt, Medientyp, Art): exakt per b-PAC als PNG, sonst als SVG-Skizze."""
    row = {k: "" if v is None else str(v) for k, v in row.items()}
    if preview_available(printer):
        try:
            return printer.render_png(template, row), "image/png", "bpac"
        except bpac_errors:
            pass   # z. B. Drucker/Treiber fehlt -> Skizze statt Fehler
    try:
        from .preview_svg import render_svg   # wird erst bei Bedarf geladen
    except ImportError:
        raise TemplateError("Vorschau nicht möglich: b-PAC nicht verfügbar und Skizzen-Vorschau fehlt.")
    return render_svg(template, row), "image/svg+xml", "skizze"


def preview_available(printer) -> bool:
    check = getattr(printer, "preview_available", None)
    try:
        return bool(check and check())
    except Exception:   # b-PAC/COM-Probleme dürfen die Oberfläche nicht stören
        return False


# ------------------------------------------------------------------ Bearbeiten im P-touch Editor
def backup_once(cfg: Config, path: Path) -> Path | None:
    """Sicherung der Originalvorlage – nur beim ersten Bearbeiten. Liefert die neue Sicherung oder None."""
    return _backup_once(cfg.template_dir, path)


def edit_original(cfg: Config, path: Path, columns: list[str], rows: list[dict],
                  desktop: Desktop | Callable[[Path], None]) -> dict:
    """Beispiel-CSV schreiben, Original sichern und verknüpfen, im P-touch Editor öffnen.

    desktop: Desktop-Objekt (lokal oder Helfer) oder – wie früher – eine opener-Funktion.
    Verknüpfen + Öffnen macht der Desktop, damit im Helfer-Modus der Windows-Pfad der CSV
    in der Vorlage landet. Ist der Vorlagen-Ordner des Helfer-PCs die Quelle
    (desktop.remote_backup()), sichert und ändert der Helfer die Vorlage DORT; die Änderung
    kommt über den Abgleich zum Server zurück. Die Antwort nennt dann die PC-Pfade.
    """
    with zipfile.ZipFile(path) as z:   # ohne Datenbank-Verknüpfung gar nicht erst anfangen
        _relink(z.read("label.xml").decode("utf-8"), Path("x.csv"))
    csv_path = write_csv(cfg.output_dir / SAMPLE_DIR / f"{path.stem}.csv", columns, rows, cfg)
    d = as_desktop(desktop)
    remote = bool(getattr(d, "remote_backup", lambda: False)())
    backup = None if remote else backup_once(cfg, path)
    out = {"vorlage": str(path), "csv": str(csv_path), "backup": str(backup) if backup else None}
    res = d.edit_original(path, csv_path)
    if isinstance(res, dict):   # Helfer: Pfade auf dem PC (verknüpfte CSV, ggf. Sicherung dort)
        out.update({k: str(res[k]) for k in ("vorlage", "csv") if res.get(k)})
        if remote:
            out["backup"] = res.get("backup")
    return out


def copy_template(cfg: Config, source: str, new_name: str) -> Path:
    src = template_path(cfg, source)
    target = cfg.template_dir / check_name(new_name)
    if target.exists():
        raise TemplateExists(f"Vorlage '{target.name}' gibt es schon – bitte anderen Namen wählen.")
    shutil.copy2(src, target)
    return target
