"""Lagerboxen aus Part-DB-Lagerorten lesen, Namen vergeben, Kommentar-Format."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass

from .config import DEFAULT_HEX, Config
from .partdb import ref_id

COMMENT_KEYS = ("Lagerinhalt", "Lagerfarbe", "Lagerabteile", "Lagerart")
_KEY_SPLIT = re.compile(r"(" + "|".join(COMMENT_KEYS) + r")\s*:\s*")
NAME_RE = re.compile(r"^(?P<abbr>.+?)-(?P<comp>\d+)\.(?P<num>\d+)$")


@dataclass
class Box:
    id: int
    name: str
    barcode: str
    abbr: str
    content: str        # Lagerinhalt
    color: str          # Lagerfarbe
    color_hex: str
    compartments: str   # Lagerabteile
    box_type: str       # Lagerart
    parent: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def barcode_for(location_id: int) -> str:
    """Part-DB-Barcode für Lagerorte: S + vierstellige ID (z. B. S0203)."""
    return f"S{location_id:04d}"


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return html.unescape(text).replace(" ", " ")


def parse_comment(text: str) -> dict[str, str]:
    """'Lagerinhalt: SMD ICs Lagerfarbe: Rot Lagerabteile: 1 Lagerart: SMD15' -> dict."""
    chunks = _KEY_SPLIT.split(_plain(text))
    out: dict[str, str] = {}
    # chunks = [vorher, key, wert, key, wert, ...]
    for i in range(1, len(chunks) - 1, 2):
        out[chunks[i]] = " ".join(chunks[i + 1].split())
    return out


def build_comment(content: str, color: str, compartments: int | str, box_type: str) -> str:
    """Gleiches Format wie die bestehenden Boxen, plus Lagerart."""
    return (f"Lagerinhalt: {content} Lagerfarbe: {color} "
            f"Lagerabteile: {compartments} Lagerart: {box_type}")


def comment_prefix(text: str) -> str:
    """Freier Text vor „Lagerinhalt: …“ – bleibt beim Ändern einer Box erhalten."""
    return " ".join(_KEY_SPLIT.split(_plain(text))[0].split())


def _box_type_from_parameters(loc: dict, cfg: Config) -> str:
    """Alte Boxen haben die Lagerart als Parameter (Name = Boxtyp oder Name 'Lagerart')."""
    for p in loc.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if name.lower() == "lagerart":
            return str(p.get("value_text") or "").strip()
        if name in cfg.box_types:
            return name
    return ""


def parse_box(loc: dict, cfg: Config) -> Box:
    lid = ref_id(loc) or 0
    name = str(loc.get("name") or "")
    meta = parse_comment(str(loc.get("comment") or ""))

    m = NAME_RE.match(name)
    abbr = m.group("abbr") if m else ""
    cat = cfg.categories.get(abbr) or cfg.category_by_name(meta.get("Lagerinhalt", ""))

    content = meta.get("Lagerinhalt") or (cat.name if cat else "")
    color = meta.get("Lagerfarbe") or (cat.color if cat else "")
    compartments = meta.get("Lagerabteile") or (m.group("comp") if m else "")
    box_type = meta.get("Lagerart") or _box_type_from_parameters(loc, cfg)

    parent = loc.get("parent")
    parent_name = parent.get("name", "") if isinstance(parent, dict) else ""

    return Box(
        id=lid,
        name=name,
        barcode=barcode_for(lid),
        abbr=abbr or (cat.key if cat else ""),
        content=content,
        color=color,
        color_hex=cfg.hex_for(color) or (cat.color_hex if cat else DEFAULT_HEX),
        compartments=str(compartments),
        box_type=box_type,
        parent=parent_name,
    )


def next_number(abbr: str, existing_names: list[str]) -> int:
    """Laufende Nummer ist pro Kürzel global (M8-1.0002 -> M8-2.0003)."""
    highest = 0
    for n in existing_names:
        m = NAME_RE.match(n or "")
        if m and m.group("abbr") == abbr:
            highest = max(highest, int(m.group("num")))
    return highest + 1


def make_name(abbr: str, compartments: int, number: int) -> str:
    return f"{abbr}-{compartments}.{number:04d}"
