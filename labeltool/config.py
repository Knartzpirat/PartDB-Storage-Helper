"""Lädt config.yaml. Kategorien und Lagerarten (Stammdaten) liegen in stammdaten.yaml,
siehe stammdaten.py – config.yaml liefert nur die Startwerte.

Part-DB-Zugang, Druck- und Helfer-Einstellungen werden zusätzlich von einstellungen.yaml
(im Ordner der config.yaml, vom Tool verwaltet, siehe einstellungen.py) überlagert.
Rangfolge: einstellungen.yaml > config.yaml > Umgebungsvariablen PARTDB_URL/PARTDB_TOKEN
(nur Startwert, z. B. im Docker-Container)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_HEX = "#9a9a9a"


def color_key(name: str) -> str:
    """Vergleichsschlüssel für Farbnamen: 'Hell Blau' == 'hellblau' == 'Hell-Blau'."""
    return re.sub(r"[\s\-_]+", "", (name or "").lower())


@dataclass
class Category:
    key: str            # Abkürzung, z. B. "M8", "SMDICs"
    name: str           # Lagerinhalt, z. B. "SMD ICs"
    color: str          # Boxfarbe, Name aus der Farbpalette, z. B. "Rot"
    color_hex: str = DEFAULT_HEX  # wird immer aus der Palette übernommen (nur Anzeige)


@dataclass
class ContentTemplate:
    file: str           # CSV-Dateiname, mit dem die P-touch-Vorlage verknüpft ist
    slots: int          # Anzahl Section/Footprint-Felder auf dem Label (= Abteile)


@dataclass
class BoxType:
    name: str               # Lagerart, z. B. "WinBin2", "SMD15"
    compartments: int = 1   # Abteile – Vorbelegung bei "Neue Box" und Felder auf dem Bauteil-Label
    file: str = ""          # CSV der Bauteil-Vorlage, leer = Inhalt_<name>.csv
    lbx: str = ""           # P-touch-Vorlage im Vorlagenordner, z. B. "PartDB_Window-Div-Bin-2.lbx"


@dataclass
class Config:
    partdb_url: str
    partdb_token: str
    verify_ssl: bool
    output_dir: Path
    csv_encoding: str
    csv_delimiter: str
    label_tag: str
    name_label_file: str
    categories: dict[str, Category]
    box_types: dict[str, BoxType]
    colors: dict[str, str]  # Boxfarben: Name -> Hex, z. B. "Hellblau" -> "#8ecae6"
    default_content_slots: int
    content_category_source: str   # "part" (Kategorie der Bauteile) oder "box" (Lagerinhalt)
    part_separator: str            # trennt mehrere Bauteile/Footprints/Kategorien in einem Feld
    compartment_regex: str
    state_file: Path
    master_file: Path
    template_dir: Path      # Ordner mit den .lbx-Vorlagen
    box_template: str       # .lbx des 24-mm-Box-Labels
    direct_print: bool = False          # Direktdruck über b-PAC anbieten
    printer: str = ""                   # Windows-Druckername, leer = Drucker aus der Vorlage
    cut_options: list[str] = field(default_factory=lambda: ["autocut", "cutatend"])
    preview_dpi: int = 360              # Auflösung der b-PAC-Vorschau (höher = schärfer, langsamer)
    print_quality: str = "standard"     # Direktdruck: standard | hoch (180×360) | hoch_langsam
    print_mode: str = "lokal"           # "lokal" = b-PAC im Prozess, "helfer" = über den Windows-Druck-Helfer
    helper_token: str = ""              # Token, mit dem sich der Druck-Helfer anmeldet
    settings_file: Path = field(default_factory=lambda: Path("einstellungen.yaml"))
    host: str = "127.0.0.1"
    port: int = 8765
    raw: dict = field(default_factory=dict)

    def color_name(self, color: str) -> str | None:
        """Schreibweise aus der Palette, oder None wenn die Farbe unbekannt ist."""
        k = color_key(color)
        return next((n for n in self.colors if color_key(n) == k), None) if k else None

    def hex_for(self, color: str) -> str:
        name = self.color_name(color)
        return self.colors[name] if name else ""

    def sync_category_colors(self) -> None:
        """Anzeigefarbe der Inhalte aus der Palette übernehmen."""
        for cat in self.categories.values():
            cat.color = self.color_name(cat.color) or cat.color
            cat.color_hex = self.hex_for(cat.color) or DEFAULT_HEX

    def category_by_name(self, name: str) -> Category | None:
        for c in self.categories.values():
            if c.name.strip().lower() == (name or "").strip().lower():
                return c
        return None

    @property
    def content_templates(self) -> dict[str, ContentTemplate]:
        """Lagerarten mit eigener CSV-Datei."""
        return {n: self.template_for(n) for n, b in self.box_types.items() if b.file}

    def template_for(self, box_type: str) -> ContentTemplate:
        bt = self.box_types.get(box_type)
        safe = box_type or "Unbekannt"
        return ContentTemplate(file=(bt.file if bt and bt.file else f"Inhalt_{safe}.csv"),
                               slots=(bt.compartments if bt else self.default_content_slots))


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    base = path.parent
    pdb = raw.get("partdb", {})
    out = raw.get("output", {})
    lab = raw.get("labels", {})
    prn = raw.get("druck") or {}

    # Umgebungsvariablen nur als Startwert, wenn config.yaml nichts einträgt
    url = str(pdb.get("url") or os.environ.get("PARTDB_URL") or "")
    token = str(pdb.get("token") or os.environ.get("PARTDB_TOKEN") or "")
    hlp = raw.get("helfer") or {}

    cats = {}
    for key, c in (raw.get("kategorien") or {}).items():
        key = str(key)
        cats[key] = Category(
            key=key,
            name=str(c.get("name", key)),
            color=str(c.get("farbe", "")).strip(),
            color_hex=str(c.get("hex", DEFAULT_HEX)),
        )

    box_types = parse_box_types(raw.get("boxtypen") or raw.get("lagerarten"), lab.get("inhalt_vorlagen"))

    template_dir = Path(lab.get("vorlagen_ordner", "Vorlagen"))
    if not template_dir.is_absolute():
        template_dir = base / template_dir

    output_dir = Path(out.get("ordner", "labels"))
    if not output_dir.is_absolute():
        output_dir = base / output_dir

    cfg = Config(
        partdb_url=url.strip().rstrip("/"),
        partdb_token=token,
        verify_ssl=bool(pdb.get("ssl_pruefen", True)),
        output_dir=output_dir,
        csv_encoding=str(out.get("encoding", "utf-8-sig")),
        csv_delimiter=str(out.get("trennzeichen", ",")),
        label_tag=str(lab.get("tag", "noLabel")),
        name_label_file=str(lab.get("namenslabel_datei", "Namenslabels.csv")),
        categories=cats,
        box_types=box_types,
        colors=parse_colors(raw.get("farben"), cats),
        default_content_slots=int(lab.get("standard_felder", 6)),
        content_category_source=str(lab.get("kategorie_quelle", "part")),
        part_separator=str(lab.get("trenner", " | ")),
        compartment_regex=str(lab.get("fach_regex", r"(?i)^\s*(?:fach|f)\s*[-:#]?\s*(\d+)")),
        state_file=base / str(raw.get("status_datei", "status.json")),
        master_file=base / str(raw.get("stammdaten_datei", "stammdaten.yaml")),
        template_dir=template_dir,
        box_template=str(lab.get("box_vorlage", "")),
        direct_print=bool(prn.get("direktdruck", False)),
        printer=str(prn.get("drucker") or ""),
        cut_options=[str(o) for o in (prn.get("schnitt") or ["autocut", "cutatend"])],
        preview_dpi=int(prn.get("vorschau_dpi") or 360),
        print_quality=str(prn.get("qualitaet") or "standard"),
        print_mode=str(prn.get("modus") or "lokal").strip().lower(),
        helper_token=str(hlp.get("token") or ""),
        settings_file=base / "einstellungen.yaml",
        host=str(raw.get("server", {}).get("host", "127.0.0.1")),
        port=int(raw.get("server", {}).get("port", 8765)),
        raw=raw,
    )
    cfg.sync_category_colors()
    from .einstellungen import apply_settings, load_settings   # einstellungen.py nutzt Config
    apply_settings(cfg, load_settings(cfg.settings_file))
    return cfg


def parse_colors(entries, categories: dict[str, Category]) -> dict[str, str]:
    """Palette aus 'farben' – fehlt sie (älteres Format), aus den Hexwerten der Inhalte bauen."""
    if entries:
        return {str(k): str(v) for k, v in entries.items()}
    colors: dict[str, str] = {}
    seen = set()
    for cat in categories.values():
        if cat.color and color_key(cat.color) not in seen:
            seen.add(color_key(cat.color))
            colors[cat.color] = cat.color_hex
    return colors


def _guess_compartments(name: str, slots: int | None) -> int:
    """Startwert für Abteile: WinBin3 / 3U2x1_3 -> 3, sonst alte Feldanzahl, sonst 1."""
    m = re.fullmatch(r"WinBin(\d+)|3U2x1_(\d+)", name)
    if m:
        return int(m.group(1) or m.group(2))
    return slots or 1


def parse_box_types(entries, templates=None) -> dict[str, BoxType]:
    """Lagerarten als Liste (altes Format) oder Mapping name -> {abteile, datei, lbx}.
    'felder' aus älteren Versionen dient nur noch als Startwert für die Abteile."""
    templates = templates or {}
    if isinstance(entries, dict):
        items = [(str(k), v or {}) for k, v in entries.items()]
    else:
        items = [(str(k), {}) for k in (entries or [])]
    out: dict[str, BoxType] = {}
    for name, v in items:
        if name in out:   # Duplikate entfernen, Reihenfolge behalten
            continue
        t = templates.get(name) or {}
        file = str(v.get("datei") or t.get("datei") or "")
        slots = v.get("felder", t.get("felder"))
        slots = int(slots) if slots else None
        comp = v.get("abteile")
        out[name] = BoxType(name=name, file=file, lbx=str(v.get("lbx") or ""),
                            compartments=int(comp) if comp else _guess_compartments(name, slots))
    return out
