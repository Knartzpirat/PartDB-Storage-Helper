"""Brother-Symbolschriften des P-touch Editors für b-PAC bereitstellen.

P-touch Editor 6 bringt Schriften wie "PTC Dingbats3" (u. a. der Trennstrich zwischen Feldern) nur
eingebettet in lbxcore*.dll mit; in Windows sind sie nicht installiert. b-PAC zeichnet Symbole aus
diesen Schriften deshalb als leeres Kästchen. Wir lesen die Schriften aus der lokalen Installation und
laden sie nur für diesen Prozess (AddFontMemResourceEx) – nichts wird installiert, kopiert oder
weitergegeben, und Admin-Rechte sind nicht nötig. b-PAC (bpac.dll, in-process) sieht sie dann.

Diagnose: python -m labeltool.ptouch_fonts
"""
from __future__ import annotations

import logging
import struct
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "PTC "                 # nur die P-touch-eigenen Schriften, keine anderen eingebetteten
MAX_FONT = 30 * 1024 * 1024

_lock = threading.Lock()
_loaded: dict[str, object] | None = None    # Familie -> (Handle, Puffer); Puffer muss am Leben bleiben


def _family(font: bytes) -> str | None:
    """Familienname (nameID 1) aus der name-Tabelle einer TrueType/OpenType-Schrift."""
    try:
        n = struct.unpack_from(">H", font, 4)[0]
        for i in range(n):
            tag, _, off, length = struct.unpack_from(">4sIII", font, 12 + 16 * i)
            if tag != b"name":
                continue
            _, count, strings = struct.unpack_from(">HHH", font, off)
            fallback = None
            for j in range(count):
                pid, eid, _, nid, ln, so = struct.unpack_from(">HHHHHH", font, off + 6 + 12 * j)
                if nid != 1:
                    continue
                raw = font[off + strings + so: off + strings + so + ln]
                if pid in (0, 3):
                    return raw.decode("utf-16-be", "replace")
                fallback = fallback or raw.decode("latin-1")
            return fallback
    except struct.error:
        return None
    return None


def extract_fonts(data: bytes) -> dict[str, bytes]:
    """Eingebettete Schriften (sfnt) aus einer Datei: {Familie: Bytes}, nur PTC-Schriften."""
    out: dict[str, bytes] = {}
    for sig in (b"\x00\x01\x00\x00", b"OTTO"):
        pos = 0
        while (pos := data.find(sig, pos)) != -1:
            start, pos = pos, pos + 4
            try:
                n = struct.unpack_from(">H", data, start + 4)[0]
                if not 4 <= n <= 40:
                    continue
                end, tags = start, []
                for i in range(n):
                    tag, _, off, length = struct.unpack_from(">4sIII", data, start + 12 + 16 * i)
                    if not (tag.isascii() and tag.strip().isalnum() or tag in (b"OS/2", b"cvt ")):
                        break
                    tags.append(tag)
                    end = max(end, start + off + length)
                else:
                    if b"name" in tags and b"cmap" in tags and end - start <= MAX_FONT and end <= len(data):
                        font = data[start:end]
                        fam = _family(font)
                        if fam and fam.startswith(PREFIX) and fam not in out:
                            out[fam] = font
            except struct.error:
                continue
    return out


def font_files() -> list[Path]:
    """lbxcore*.dll der installierten P-touch-Editor-Versionen (neueste zuerst)."""
    from .ptouch_modelle import editor_ordner
    files: list[Path] = []
    for folder in editor_ordner():
        for pattern in ("lbxcore*.dll", "*/lbxcore*.dll"):
            files += sorted(p for p in folder.glob(pattern) if p.is_file())
    return list(dict.fromkeys(p.resolve() for p in files))   # Registry und Standardpfad zeigen oft gleich


def find_fonts() -> dict[str, bytes]:
    fonts: dict[str, bytes] = {}
    for f in font_files():
        try:
            for fam, data in extract_fonts(f.read_bytes()).items():
                fonts.setdefault(fam, data)
        except OSError as e:
            log.warning("P-touch-Schriften: %s nicht lesbar (%s)", f, e)
    return fonts


def load() -> list[str]:
    """Schriften einmal pro Prozess laden. -> geladene Familien (leer ohne Windows/P-touch Editor 6)."""
    global _loaded
    with _lock:
        if _loaded is None:
            _loaded = {}
            if sys.platform.startswith("win"):
                try:
                    _loaded = _add_private(find_fonts())
                except Exception as e:   # nie den Start verhindern – dann greift der Linien-Ersatz in bpac
                    log.warning("P-touch-Schriften konnten nicht geladen werden: %s", e)
            if _loaded:
                log.info("P-touch-Schriften für b-PAC geladen: %s", ", ".join(sorted(_loaded)))
        return sorted(_loaded)


def _add_private(fonts: dict[str, bytes]) -> dict[str, object]:
    import ctypes
    from ctypes import wintypes
    gdi = ctypes.windll.gdi32
    gdi.AddFontMemResourceEx.restype = wintypes.HANDLE
    gdi.AddFontMemResourceEx.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.DWORD)]
    loaded = {}
    for fam, data in fonts.items():
        buf = ctypes.create_string_buffer(data, len(data))
        count = wintypes.DWORD(0)
        handle = gdi.AddFontMemResourceEx(buf, len(data), None, ctypes.byref(count))
        if handle and count.value:
            loaded[fam] = (handle, buf)
    return loaded


if __name__ == "__main__":
    print("Gefundene Dateien:", *[str(p) for p in font_files()] or ["– (kein P-touch Editor 6)"], sep="\n  ")
    print("Geladen:", ", ".join(load()) or "keine")
