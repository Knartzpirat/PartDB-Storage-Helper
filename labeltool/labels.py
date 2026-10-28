"""Labeldaten bauen und als CSV für die P-touch-Editor-Datenbankverknüpfung schreiben.

Die Zeilen (list[dict]) sind bewusst vom Ausgabeformat getrennt, damit später
ein b-PAC-Exporter dieselben Daten direkt in .lbx-Vorlagen drucken kann.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .model import Box
from .partdb import PartDB, ref_id

# Spalten der CSV für die Box-Label-Vorlage (24 mm) – Vorlagen sind mit diesen Namen verknüpft.
NAME_COLUMNS = ["ID", "Lagername", "Barcodeinhalt", "Lagerinhalt", "Lagerabteile",
                "Lagerfarbe", "Lagerart", "Lager-ID"]


def name_label_rows(boxes: list[Box]) -> list[dict]:
    return [{
        "ID": b.id,
        "Lagername": b.name,
        "Barcodeinhalt": b.barcode,
        "Lagerinhalt": b.content,
        "Lagerabteile": b.compartments,
        "Lagerfarbe": b.color,
        "Lagerart": b.box_type,
        "Lager-ID": b.id,
    } for b in boxes]


# Die P-touch-Vorlagen kennen immer Section1..6 / Footprint1..6, auch wenn sie weniger nutzen.
MIN_CONTENT_COLUMNS = 6


def content_columns(slots: int) -> list[str]:
    # Reihenfolge: Location, Category, Section1..n, Footprint1..n
    n = max(slots, MIN_CONTENT_COLUMNS)
    cols = ["Location", "Category"]
    cols += [f"Section{i}" for i in range(1, n + 1)]
    cols += [f"Footprint{i}" for i in range(1, n + 1)]
    cols += ["Barcode", "Lagerfarbe", "Lagerinhalt"]  # zusätzlich, am Ende -> stört bestehende Vorlagen nicht
    return cols


@dataclass
class PartEntry:
    part_id: int
    name: str
    footprint: str
    category: str
    compartment: int | None
    tagged: bool
    mpn: str = ""        # Herstellernummer – nur für die Suche
    ipn: str = ""        # interne Teilenummer – nur für die Suche


@dataclass
class ContentLabel:
    box: Box
    template_file: str
    slots: int
    parts: list[PartEntry]
    warnings: list[str] = field(default_factory=list)

    @property
    def tagged_part_ids(self) -> list[int]:
        return [p.part_id for p in self.parts if p.tagged]

    def row(self, cfg: Config) -> dict:
        ordered = self.ordered_slots()
        if cfg.content_category_source == "box":
            category = self.box.content
        else:
            cats = Counter(p.category for p in self.parts if p.category)
            category = cats.most_common(1)[0][0] if cats else self.box.content
        row = {"Location": self.box.name, "Category": category}
        for i, p in enumerate(ordered, start=1):
            row[f"Section{i}"] = p.name if p else ""
            row[f"Footprint{i}"] = p.footprint if p else ""
        row.update({"Barcode": self.box.barcode, "Lagerfarbe": self.box.color,
                    "Lagerinhalt": self.box.content})
        return row

    def ordered_slots(self) -> list[PartEntry | None]:
        """Bauteile mit Fachnummer kommen in ihr Fach, der Rest füllt freie Felder."""
        slots: list[PartEntry | None] = [None] * self.slots
        rest: list[PartEntry] = []
        for p in sorted(self.parts, key=lambda x: (x.compartment or 10**6, x.name.lower())):
            if p.compartment and 1 <= p.compartment <= self.slots and slots[p.compartment - 1] is None:
                slots[p.compartment - 1] = p
            else:
                rest.append(p)
        for p in rest:
            try:
                slots[slots.index(None)] = p
            except ValueError:
                break
        return slots


def compartment_of(lot: dict, regex: str) -> int | None:
    for field_name in ("description", "comment"):
        m = re.search(regex, str(lot.get(field_name) or ""))
        if m:
            return int(m.group(1))
    return None


def has_tag(tags: str, tag: str) -> bool:
    return tag.lower() in [t.strip().lower() for t in (tags or "").split(",")]


def remove_tag(tags: str, tag: str) -> str:
    return ",".join(t.strip() for t in (tags or "").split(",")
                    if t.strip() and t.strip().lower() != tag.lower())


def parts_by_location(api: PartDB, cfg: Config) -> tuple[dict[int, list[PartEntry]], set[int]]:
    """(Bauteile je Lagerort, Lagerorte mit mindestens einem getaggten Bauteil)."""
    parts = api.parts()
    lots = api.lots_by_part(parts)

    by_location: dict[int, list[PartEntry]] = {}
    locations_with_tag: set[int] = set()

    for p in parts:
        pid = ref_id(p)
        if pid is None:
            continue
        tagged = has_tag(str(p.get("tags") or ""), cfg.label_tag)
        footprint = api.name_of(p.get("footprint"), "footprints")
        category = api.name_of(p.get("category"), "categories")
        seen: set[int] = set()
        for lot in lots.get(pid, []):
            lid = ref_id(lot.get("storage_location") or lot.get("storageLocation"))
            if lid is None or lid in seen:
                continue
            seen.add(lid)
            by_location.setdefault(lid, []).append(PartEntry(
                part_id=pid,
                name=str(p.get("name") or ""),
                footprint=footprint,
                category=category,
                compartment=compartment_of(lot, cfg.compartment_regex),
                tagged=tagged,
                mpn=str(p.get("manufacturer_product_number") or ""),
                ipn=str(p.get("ipn") or ""),
            ))
            if tagged:
                locations_with_tag.add(lid)
    return by_location, locations_with_tag


def make_content_label(cfg: Config, box: Box, parts: list[PartEntry]) -> ContentLabel:
    """Label einer Box mit den übergebenen Bauteilen, inkl. Warnungen."""
    tpl = cfg.template_for(box.box_type)
    label = ContentLabel(box=box, template_file=tpl.file, slots=tpl.slots, parts=parts)
    if not box.box_type:
        label.warnings.append("Keine Lagerart hinterlegt – Standardvorlage wird genutzt.")
    if len(label.parts) > tpl.slots:
        label.warnings.append(f"{len(label.parts)} Bauteile, aber nur {tpl.slots} Felder – Rest fehlt auf dem Label.")
    comps = Counter(p.compartment for p in label.parts if p.compartment)
    dup = [str(c) for c, n in comps.items() if n > 1]
    if dup:
        label.warnings.append(f"Mehrere Bauteile im selben Fach: {', '.join(dup)}")
    return label


def build_content_labels(api: PartDB, cfg: Config, boxes: dict[int, Box]) -> list[ContentLabel]:
    """Alle Boxen, in denen mindestens ein Bauteil den Tag hat – mit ALLEN Bauteilen der Box."""
    by_location, locations_with_tag = parts_by_location(api, cfg)
    return [make_content_label(cfg, boxes[lid], by_location[lid])
            for lid in sorted(locations_with_tag) if lid in boxes]


def content_label_for_box(api: PartDB, cfg: Config, box: Box) -> ContentLabel:
    """Label einer einzelnen Box mit allen Bauteilen, unabhängig vom Tag (für die Vorschau)."""
    by_location, _ = parts_by_location(api, cfg)
    return make_content_label(cfg, box, by_location.get(box.id, []))


_ID_RE = re.compile(r"^(?:s|#)?0*(\d+)$", re.I)   # "S0203", "#203", "203"


def search_boxes(boxes: dict[int, Box], by_location: dict[int, list[PartEntry]], query: str,
                 limit: int = 30) -> list[tuple[Box, list[PartEntry]]]:
    """Boxen zu einer Suche: Box-ID/Barcode, Boxname, Inhalt, Lagerart, Farbe oder ein Bauteil darin
    (Name, ID, MPN, IPN, Footprint, Kategorie). Alle Suchwörter müssen passen.

    -> [(Box, passende Bauteile)], exakte ID-Treffer zuerst, dann nach Boxname.
    """
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        return []
    exact: set[int] = set()
    m = _ID_RE.match(query.strip()) if len(words) == 1 else None
    if m:
        exact.add(int(m.group(1)))

    def box_text(b: Box) -> str:
        return " ".join([str(b.id), b.barcode, b.name, b.content, b.box_type, b.color]).lower()

    def part_text(p: PartEntry) -> str:
        return " ".join([str(p.part_id), p.name, p.mpn, p.ipn, p.footprint, p.category]).lower()

    hits = []
    for b in boxes.values():
        parts = by_location.get(b.id, [])
        btext = box_text(b)
        ptexts = [part_text(p) for p in parts]
        # jedes Wort muss in der Box selbst oder in einem ihrer Bauteile vorkommen
        if not all(w in btext or any(w in t for t in ptexts) for w in words):
            if not (m and (b.id in exact or any(p.part_id in exact for p in parts))):
                continue
        matched = [p for p, t in zip(parts, ptexts)
                   if any(w in t for w in words) or (m and p.part_id in exact)]
        hits.append((b, matched))
    hits.sort(key=lambda h: (h[0].id not in exact, h[0].name.lower()))
    return hits[:limit]


def write_csv(path: Path, columns: list[str], rows: list[dict], cfg: Config) -> Path:
    """Überschreibt die Datei, damit die verknüpfte P-touch-Vorlage immer denselben Pfad hat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=cfg.csv_encoding, errors="replace") as f:
        w = csv.DictWriter(f, fieldnames=columns, delimiter=cfg.csv_delimiter,
                           extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})
    return path


def export_content_labels(labels: list[ContentLabel], cfg: Config) -> list[dict]:
    """Eine CSV je Vorlage. Liefert eine Übersicht der geschriebenen Dateien."""
    grouped: dict[tuple[str, int], list[ContentLabel]] = {}  # (CSV, Felder) -> Labels
    for lab in labels:
        grouped.setdefault((lab.template_file, lab.slots), []).append(lab)
    written = []
    for (file, slots), labs in grouped.items():
        path = write_csv(cfg.output_dir / file, content_columns(slots),
                         [l.row(cfg) for l in labs], cfg)
        types = sorted({l.box.box_type for l in labs if l.box.box_type in cfg.box_types})
        written.append({"datei": str(path), "labels": len(labs), "lagerarten": types,
                        "lbx": [cfg.box_types[t].lbx for t in types if cfg.box_types[t].lbx]})
    # Konfigurierte Vorlagen ohne Labels leeren, damit kein alter Stand doppelt gedruckt wird
    used = {f for f, _ in grouped}
    for tpl in cfg.content_templates.values():
        if tpl.file not in used:
            write_csv(cfg.output_dir / tpl.file, content_columns(tpl.slots), [], cfg)
            used.add(tpl.file)
    return written
