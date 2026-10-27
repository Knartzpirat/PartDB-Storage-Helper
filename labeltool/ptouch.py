"""P-touch-Vorlagen (.lbx) mit den CSVs des Tools verknüpfen und im P-touch Editor öffnen.

Eine .lbx ist ein ZIP mit label.xml. Die Datenbank-Verknüpfung steht dort nur als
Dateipfad (databasePath, mergeTable, dbTable name) – Datensätze werden nicht
gespeichert. Das Tool legt deshalb eine Kopie der Vorlage im Ausgabeordner ab, deren
Pfad auf die aktuelle CSV zeigt. Die Originalvorlage bleibt unverändert.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

LINKED_DIR = "Vorlagen"   # Unterordner im Ausgabeordner für die verknüpften Kopien


class TemplateError(Exception):
    pass


def _relink(xml: str, csv_path: Path) -> str:
    # Ersetzungen als Funktion, sonst würden "\U..." in Windows-Pfaden als Regex-Escape gelesen
    path, table = quoteattr(str(csv_path)), quoteattr(csv_path.name)
    xml, n = re.subn(r'databasePath="[^"]*"', lambda _: "databasePath=" + path, xml)
    if not n:
        raise TemplateError("Die Vorlage hat keine Datenbank-Verknüpfung. Im P-touch Editor einmal "
                            "Datei > Datenbank > Verbinden mit einer CSV ausführen und speichern.")
    xml = re.sub(r'mergeTable="[^"]*"', lambda _: "mergeTable=" + table, xml)
    return re.sub(r'(<database:dbTable name=)"[^"]*"', lambda m: m.group(1) + table, xml)


def linked_copy(template: Path, csv_path: Path, out_dir: Path) -> Path:
    """Kopie von `template`, deren Datenbank auf `csv_path` zeigt."""
    if not template.is_file():
        raise TemplateError(f"Vorlage nicht gefunden: {template}")
    target = out_dir / LINKED_DIR / template.name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with zipfile.ZipFile(template) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "label.xml":
                data = _relink(data.decode("utf-8"), csv_path.resolve()).encode("utf-8")
            dst.writestr(item, data)
    try:
        tmp.replace(target)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise TemplateError(f"{target.name} ist noch im P-touch Editor geöffnet – bitte dort schließen.")
    return target


def open_file(path: Path) -> None:
    """Datei mit dem Standardprogramm öffnen (.lbx -> P-touch Editor)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])

