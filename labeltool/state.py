"""Lokaler Status: Namenslabel-Warteschlange und was zuletzt exportiert wurde."""
from __future__ import annotations

import json
import threading
from pathlib import Path

_DEFAULT = {
    "name_queue": [],          # Lagerort-IDs, die ein 12-mm-Namenslabel brauchen
    "last_name_export": [],    # IDs aus dem letzten Namenslabel-Export
    "last_content_export": [], # Part-IDs, deren Tag nach dem Druck entfernt wird
    "last_created": [],        # zuletzt angelegte Boxen (für „Rückgängig“)
    "content_reprint": [],     # Boxen, deren Bauteil-Label zusätzlich (ohne Tag) gedruckt werden soll
    "last_content_reprint": [],  # davon im letzten Export/Druck – erledigt mit „Gedruckt“
}


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = {k: list(v) for k, v in _DEFAULT.items()}   # eigene Listen – queue_names ändert sie
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def queue_names(self, ids) -> None:
        with self._lock:
            q = self.data["name_queue"]
            for i in ids:
                if int(i) not in q:
                    q.append(int(i))
            self.save()

    def unqueue_name(self, lid: int) -> None:
        with self._lock:
            self.data["name_queue"] = [i for i in self.data["name_queue"] if i != int(lid)]
            self.save()

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()
