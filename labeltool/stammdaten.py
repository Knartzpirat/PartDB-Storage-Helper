"""Stammdaten (Inhalte = Kategorien, Lagerarten) bearbeiten, speichern und rückgängig machen.

Gespeichert wird in stammdaten.yaml. Fehlt die Datei, dienen die Einträge aus
config.yaml als Startwerte. Vor jeder Änderung wird ein Schnappschuss auf einen
Rückgängig-Stapel gelegt (in stammdaten.verlauf.json, überlebt also Neustarts).
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import yaml

from .config import BoxType, Category, Config, color_key, parse_box_types, parse_colors

UNDO_LIMIT = 50
ABBR_RE = re.compile(r"^[A-Za-z0-9ÄÖÜäöüß._+]+$")   # Kürzel steht im Boxnamen vor "-<Abteile>.<Nr>"
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class MasterDataError(ValueError):
    pass


class MasterData:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.master_file
        self.undo_path = self.path.with_name(self.path.stem + ".verlauf.json")
        self._lock = threading.Lock()
        self.undo: list[dict] = []
        if self.path.exists():
            self._apply(yaml.safe_load(self.path.read_text(encoding="utf-8")) or {})
        else:
            self._write()   # Startwerte aus config.yaml festhalten
        if self.undo_path.exists():
            try:
                self.undo = json.loads(self.undo_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.undo = []

    # ------------------------------------------------------------ Serialisierung
    def snapshot(self) -> dict:
        return {
            "farben": dict(self.cfg.colors),
            "kategorien": {c.key: {"name": c.name, "farbe": c.color}
                           for c in self.cfg.categories.values()},
            "lagerarten": {b.name: {k: v for k, v in (("abteile", b.compartments),
                                                       ("datei", b.file), ("lbx", b.lbx)) if v}
                           for b in self.cfg.box_types.values()},
        }

    def _apply(self, data: dict) -> None:
        cats = {}
        for key, c in (data.get("kategorien") or {}).items():
            c = c or {}
            cats[str(key)] = Category(key=str(key), name=str(c.get("name", key)),
                                      color=str(c.get("farbe", "")).strip(),
                                      color_hex=str(c.get("hex", "")))
        self.cfg.categories.clear()
        self.cfg.categories.update(cats)
        self.cfg.colors.clear()
        self.cfg.colors.update(parse_colors(data.get("farben"), cats))
        self.cfg.sync_category_colors()
        self.cfg.box_types.clear()
        self.cfg.box_types.update(parse_box_types(data.get("lagerarten") or {}))

    def _write(self) -> None:
        header = ("# Wird vom Labeltool verwaltet (Reiter \"Stammdaten\").\n"
                  "# farben = Boxfarben (Name -> Anzeigefarbe), kategorien = Lagerinhalt\n"
                  "# (Schlüssel = Kürzel im Boxnamen, farbe = Name aus farben), lagerarten = Boxtypen.\n")
        data = self.snapshot()
        # Farben eine pro Zeile, Inhalte/Lagerarten kompakt als {…} je Eintrag
        body = (yaml.safe_dump({"farben": data.pop("farben")}, allow_unicode=True, sort_keys=False,
                               default_flow_style=False)
                + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(header + body, encoding="utf-8")
        tmp.replace(self.path)

    def _write_undo(self) -> None:
        tmp = self.undo_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.undo, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.undo_path)

    def _change(self, description: str, fn) -> None:
        """fn ändert cfg in place. Bei Fehler bleibt alles beim Alten."""
        with self._lock:
            before = self.snapshot()
            try:
                fn()
            except Exception:
                self._apply(before)
                raise
            self.undo.append({"text": description, "daten": before})
            del self.undo[:-UNDO_LIMIT]
            self._write()
            self._write_undo()

    def undo_last(self) -> str:
        with self._lock:
            if not self.undo:
                raise MasterDataError("Nichts zum Rückgängigmachen.")
            step = self.undo.pop()
            self._apply(step["daten"])
            self._write()
            self._write_undo()
            return step["text"]

    def info(self) -> dict:
        used = {}
        for c in self.cfg.categories.values():
            used.setdefault(color_key(c.color), []).append(c.name)
        return {
            "farben": [{"name": n, "hex": h, "inhalte": used.get(color_key(n), [])}
                       for n, h in self.cfg.colors.items()],
            "kategorien": [vars(c) for c in self.cfg.categories.values()],
            "lagerarten": [dict(vars(b), vorlage=vars(self.cfg.template_for(b.name)),
                                lbx_ok=bool(b.lbx) and (self.cfg.template_dir / b.lbx).is_file())
                           for b in self.cfg.box_types.values()],
            "vorlagen_ordner": str(self.cfg.template_dir),
            "vorlagen": sorted(p.name for p in self.cfg.template_dir.glob("*.lbx"))
                        if self.cfg.template_dir.is_dir() else [],
            "standard_felder": self.cfg.default_content_slots,
            "rueckgaengig": self.undo[-1]["text"] if self.undo else None,
            "rueckgaengig_anzahl": len(self.undo),
        }

    # ------------------------------------------------------------ Kategorien
    def _check_category(self, c: Category) -> Category:
        color = self.cfg.color_name(c.color)
        if not color:
            raise MasterDataError(f"Boxfarbe '{c.color}' gibt es nicht – erst unter Boxfarben anlegen.")
        c = Category(key=c.key.strip(), name=c.name.strip(), color=color, color_hex=self.cfg.colors[color])
        if not ABBR_RE.match(c.key):
            raise MasterDataError("Kürzel darf nur Buchstaben, Ziffern und . _ + enthalten (keine Leerzeichen, kein -).")
        if not c.name:
            raise MasterDataError("Name fehlt.")
        return c

    def save_category(self, old_key: str | None, c: Category) -> None:
        c = self._check_category(c)
        cats = self.cfg.categories
        if old_key is not None and old_key not in cats:
            raise MasterDataError(f"Inhalt '{old_key}' gibt es nicht (mehr).")
        if c.key != old_key and c.key in cats:
            raise MasterDataError(f"Kürzel '{c.key}' ist schon vergeben.")

        def fn():
            items = [(c.key, c) if k == old_key else (k, v) for k, v in cats.items()]
            if old_key is None:
                items.append((c.key, c))
            cats.clear()
            cats.update(items)

        self._change(f"Inhalt '{c.name}' {'angelegt' if old_key is None else 'geändert'}", fn)

    def delete_category(self, key: str) -> None:
        cats = self.cfg.categories
        if key not in cats:
            raise MasterDataError(f"Inhalt '{key}' gibt es nicht (mehr).")
        name = cats[key].name
        self._change(f"Inhalt '{name}' gelöscht", lambda: cats.pop(key))

    # ------------------------------------------------------------ Boxfarben
    def save_color(self, old_name: str | None, name: str, hex_value: str) -> None:
        name, hex_value = name.strip(), hex_value.strip().lower()
        colors = self.cfg.colors
        if not name:
            raise MasterDataError("Name der Boxfarbe fehlt.")
        if not HEX_RE.match(hex_value):
            raise MasterDataError("Anzeigefarbe muss ein Hexwert wie #2e8b57 sein.")
        if old_name is not None and old_name not in colors:
            raise MasterDataError(f"Boxfarbe '{old_name}' gibt es nicht (mehr).")
        clash = self.cfg.color_name(name)
        if clash and clash != old_name:
            raise MasterDataError(f"Boxfarbe '{clash}' gibt es schon.")

        def fn():
            items = [(name, hex_value) if k == old_name else (k, v) for k, v in colors.items()]
            if old_name is None:
                items.append((name, hex_value))
            colors.clear()
            colors.update(items)
            for cat in self.cfg.categories.values():   # Umbenennen wirkt auf alle Inhalte
                if old_name is not None and cat.color == old_name:
                    cat.color = name
            self.cfg.sync_category_colors()

        self._change(f"Boxfarbe '{name}' {'angelegt' if old_name is None else 'geändert'}", fn)

    def delete_color(self, name: str) -> None:
        if name not in self.cfg.colors:
            raise MasterDataError(f"Boxfarbe '{name}' gibt es nicht (mehr).")
        users = [c.name for c in self.cfg.categories.values() if c.color == name]
        if users:
            raise MasterDataError(f"Boxfarbe '{name}' wird noch genutzt von: {', '.join(users)}. "
                                  f"Erst diesen Inhalten eine andere Farbe geben.")
        self._change(f"Boxfarbe '{name}' gelöscht", lambda: self.cfg.colors.pop(name))

    # ------------------------------------------------------------ Lagerarten
    def _check_box_type(self, b: BoxType) -> BoxType:
        b = BoxType(name=b.name.strip(), compartments=int(b.compartments),
                    file=(b.file or "").strip(), lbx=(b.lbx or "").strip())
        if not b.name or re.search(r"\s", b.name):
            raise MasterDataError("Name der Lagerart darf nicht leer sein und keine Leerzeichen enthalten.")
        if not 1 <= b.compartments <= 99:
            raise MasterDataError("Abteile müssen zwischen 1 und 99 liegen.")
        for value, ext, what in ((b.file, ".csv", "CSV-Datei"), (b.lbx, ".lbx", "P-touch-Vorlage")):
            if value and (not value.lower().endswith(ext) or re.search(r"[\\/:*?\"<>|]", value)):
                raise MasterDataError(f"{what}: nur ein Dateiname mit Endung {ext}, ohne Ordner.")
        return b

    def save_box_type(self, old_name: str | None, b: BoxType) -> None:
        b = self._check_box_type(b)
        types = self.cfg.box_types
        if old_name is not None and old_name not in types:
            raise MasterDataError(f"Lagerart '{old_name}' gibt es nicht (mehr).")
        if b.name != old_name and b.name in types:
            raise MasterDataError(f"Lagerart '{b.name}' gibt es schon.")

        def fn():
            items = [(b.name, b) if k == old_name else (k, v) for k, v in types.items()]
            if old_name is None:
                items.append((b.name, b))
            types.clear()
            types.update(items)

        self._change(f"Lagerart '{b.name}' {'angelegt' if old_name is None else 'geändert'}", fn)

    def delete_box_type(self, name: str) -> None:
        types = self.cfg.box_types
        if name not in types:
            raise MasterDataError(f"Lagerart '{name}' gibt es nicht (mehr).")
        self._change(f"Lagerart '{name}' gelöscht", lambda: types.pop(name))
