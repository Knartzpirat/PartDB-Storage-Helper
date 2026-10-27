"""Vorschau-Skizze eines Labels als SVG – ohne b-PAC, nur aus der .lbx (lbx.parse).

Koordinaten in pt wie in der Vorlage: x läuft entlang der Labellänge, y quer über das
Band, beides absolut inkl. Rand. Bei landscape-Vorlagen liegt marginLeft/Right quer
zum Band und marginTop/Bottom an den Enden. Das Label selbst ist immer weiß mit
schwarzer Schrift (wie echtes Band), damit es in hellem und dunklem Theme gleich aussieht.

Es ist eine Skizze: Schrift wird nur geschätzt (≈ 0,55 × Schriftgröße pro Zeichen),
Barcodes sind Platzhalter. Exakt ist nur die b-PAC-Vorschau (bpac.render_png).
"""
from __future__ import annotations

import hashlib
import itertools
import re
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import lbx

CHAR_WIDTH = 0.55       # geschätzte Zeichenbreite in Schriftgrößen
LINE_HEIGHT = 1.15
MIN_FONT = 2.0          # pt – kleiner verkleinert die Skizze bei "shrink" nicht, Rest wird gestaucht
SCALE = 3               # Anzeigegröße: px pro pt (Breite/Höhe-Attribut, per CSS überschreibbar)
PAD = 2.0               # pt Luft um das Label (für Rahmen/Schatten)
FONT_FALLBACK = "Helvetica, Arial, sans-serif"
INK = "#000"
OVERFLOW = "#d00000"

# Zeichen, die in XML 1.0 nicht vorkommen dürfen
_ids = itertools.count(1)    # eindeutige clipPath-IDs, falls mehrere SVGs inline auf einer Seite
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _txt(value) -> str:
    return escape(_BAD_XML.sub("", str(value)))


def _attr(value) -> str:
    return quoteattr(_BAD_XML.sub("", str(value)))


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") or "0"


def estimate_width(text: str, font_size: float) -> float:
    """Grob geschätzte Textbreite in pt (längste Zeile)."""
    longest = max((len(line) for line in text.split("\n")), default=0)
    return longest * CHAR_WIDTH * font_size


def _value(obj: lbx.LbxObject, row: dict) -> str:
    if obj.field:
        v = row.get(obj.field, "")
        return "" if v is None else str(v)
    return obj.text


def _frame(obj: lbx.LbxObject) -> tuple[float, float, float, float, str]:
    """Lokales Koordinatensystem für gedrehte Objekte -> (x, y, Breite, Höhe, transform).

    Die Box in der Vorlage ist bereits gedreht (bei 90/270 schmal und hoch). Der Text
    läuft entlang der langen Seite; P-touch zählt den Winkel im Uhrzeigersinn wie SVG
    (270 = Text liest von unten nach oben).
    """
    angle = obj.angle % 360
    if angle == 0:
        return obj.x, obj.y, obj.width, obj.height, ""
    cx, cy = obj.x + obj.width / 2, obj.y + obj.height / 2
    w, h = (obj.height, obj.width) if angle in (90, 270) else (obj.width, obj.height)
    return -w / 2, -h / 2, w, h, f' transform="translate({_n(cx)} {_n(cy)}) rotate({angle})"'


def _text(obj: lbx.LbxObject, value: str) -> str:
    x, y, w, h, transform = _frame(obj)
    fs = obj.font_size or 6.0
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    auto_width = w <= 0.5                 # Breite 0 = Textfeld wächst mit dem Inhalt
    need = estimate_width(value, fs)
    overflow = not auto_width and need > w + 0.5
    squeeze = overflow and obj.shrink
    if squeeze:
        # P-touch verkleinert die Schrift; was dann noch übersteht, staucht textLength
        fs = max(MIN_FONT, fs * w / need)

    align = (obj.align_h or "LEFT").upper()
    anchor = {"CENTER": "middle", "RIGHT": "end"}.get(align, "start")
    if auto_width:
        tx = x                            # Ankerpunkt = x-Position der Vorlage
    else:
        tx = {"middle": x + w / 2, "end": x + w}.get(anchor, x)

    block = fs * (1 + LINE_HEIGHT * (len(lines) - 1))
    valign = (obj.align_v or "CENTER").upper()
    if valign == "TOP":
        top = y
    elif valign == "BOTTOM":
        top = y + h - block
    else:
        top = y + (h - block) / 2
    baseline = top + fs * 0.8             # Oberlänge ≈ 0,8 × Schriftgröße

    font = ", ".join(f"'{f}'" for f in [obj.font] if f and "'" not in f)
    family = f"{font}, {FONT_FALLBACK}" if font else FONT_FALLBACK
    attrs = [f'x="{_n(tx)}"', f'y="{_n(baseline)}"', f'font-size="{_n(fs)}"',
             f"font-family={_attr(family)}", f'text-anchor="{anchor}"', "xml:space=\"preserve\""]
    if obj.bold:
        attrs.append('font-weight="bold"')
    classes = ["obj", "text"]
    if obj.field:
        classes.append("feld")
    if squeeze:
        classes.append("gestaucht")
    elif overflow:
        classes.append("ueberlauf")
    attrs.append(f'fill="{OVERFLOW if overflow and not squeeze else INK}"')

    spans = []
    for i, line in enumerate(lines):
        extra = ""
        if squeeze and estimate_width(line, fs) >= w * 0.98:   # die Zeile(n), die das Feld füllen
            extra = f' textLength="{_n(w)}" lengthAdjust="spacingAndGlyphs"'
        dy = "" if i == 0 else f' dy="{_n(fs * LINE_HEIGHT)}"'
        spans.append(f'<tspan x="{_n(tx)}"{dy}{extra}>{_txt(line)}</tspan>')

    title = f"{obj.name}: {obj.field}" if obj.field else obj.name
    if overflow and not squeeze:
        title += " – Text passt vermutlich nicht"
    out = [f'<g class={_attr(" ".join(classes))} data-index="{obj.index}"{transform}>',
           f"<title>{_txt(title)}</title>"]
    if overflow and not squeeze:
        out.append(f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" fill="none" '
                   f'stroke="{OVERFLOW}" stroke-width="0.4" stroke-dasharray="1.2 0.8"/>')
    out.append(f'<text {" ".join(attrs)}>{"".join(spans)}</text>')
    out.append("</g>")
    return "".join(out)


def _modules(value: str, n: int) -> list[list[bool]]:
    """Deterministisches Pseudo-Muster (kein echter Code) mit DataMatrix-Suchmuster."""
    bits = []
    seed = value.encode("utf-8")
    counter = 0
    while len(bits) < n * n:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        bits.extend(b >> k & 1 for b in digest for k in range(8))
        counter += 1
    grid = [[bool(bits[r * n + c]) for c in range(n)] for r in range(n)]
    for i in range(n):
        grid[i][0] = True                 # durchgehende Kante links
        grid[n - 1][i] = True             # und unten
        grid[0][i] = i % 2 == 0           # Taktmuster oben
        grid[i][n - 1] = i % 2 == 1       # und rechts
    return grid


def _barcode(obj: lbx.LbxObject, value: str) -> str:
    x, y, w, h, transform = _frame(obj)
    proto = (obj.barcode or "").upper()
    title = f"{obj.name} ({proto or 'Barcode'}): {value}"
    out = [f'<g class="obj barcode" data-index="{obj.index}" data-wert={_attr(value)}{transform}>',
           f"<title>{_txt(title)}</title>"]
    if proto in ("DATAMATRIX", "QRCODE", "QR", "PDF417", "MAXICODE", "AZTEC") or abs(w - h) < 0.5:
        side = min(w, h)
        n = 10 if len(value) <= 3 else 12 if len(value) <= 6 else 14 if len(value) <= 10 else 16 \
            if len(value) <= 16 else 18 if len(value) <= 25 else 22
        cell = side / n
        ox, oy = x + (w - side) / 2, y + (h - side) / 2
        path = []
        for r, cols in enumerate(_modules(value, n)):
            for c, on in enumerate(cols):
                if on:
                    path.append(f"M{_n(ox + c * cell)} {_n(oy + r * cell)}h{_n(cell)}v{_n(cell)}h-{_n(cell)}z")
        out.append(f'<path d="{"".join(path)}" fill="{INK}" opacity="0.85"/>')
    else:
        bars = []
        bx = x
        seed = hashlib.sha256(value.encode("utf-8")).digest()
        i = 0
        while bx < x + w - 0.6:
            bw = 0.3 + (seed[i % len(seed)] % 3) * 0.3
            if i % 2 == 0:
                bars.append(f"M{_n(bx)} {_n(y)}h{_n(bw)}v{_n(h * 0.75)}h-{_n(bw)}z")
            bx += bw
            i += 1
        out.append(f'<path d="{"".join(bars)}" fill="{INK}" opacity="0.85"/>')
    # Wert gut lesbar mittig auf weißem Grund – es ist ja nur ein Platzhalter
    fs = max(1.6, min(3.2, h / 5))
    label = value if len(value) <= 24 else value[:23] + "…"
    lw = min(w, estimate_width(label, fs) + 1)
    cx, cy = x + w / 2, y + h / 2
    out.append(f'<rect x="{_n(cx - lw / 2)}" y="{_n(cy - fs * 0.75)}" width="{_n(lw)}" height="{_n(fs * 1.4)}" '
               f'fill="#fff" opacity="0.9"/>')
    extra = f' textLength="{_n(w)}" lengthAdjust="spacingAndGlyphs"' if lw >= w else ""
    out.append(f'<text x="{_n(cx)}" y="{_n(cy + fs * 0.35)}" font-size="{_n(fs)}" text-anchor="middle" '
               f'font-family={_attr(FONT_FALLBACK)} fill="{INK}"{extra}>{_txt(label)}</text>')
    out.append("</g>")
    return "".join(out)


def _other(obj: lbx.LbxObject) -> str:
    """Formen/Symbole dezent andeuten."""
    x, y, w, h = obj.x, obj.y, obj.width, obj.height
    title = f"<title>{_txt(obj.name or obj.kind)}</title>"
    head = f'<g class={_attr("obj " + obj.kind.replace(":", "-"))} data-index="{obj.index}">{title}'
    if obj.kind.startswith("draw:") and (w < 1.5 or h < 1.5):
        # dünne Form = Linie entlang der langen Seite
        if w < h:
            line = f'x1="{_n(x + w / 2)}" y1="{_n(y)}" x2="{_n(x + w / 2)}" y2="{_n(y + h)}"'
        else:
            line = f'x1="{_n(x)}" y1="{_n(y + h / 2)}" x2="{_n(x + w)}" y2="{_n(y + h / 2)}"'
        return f'{head}<line {line} stroke="{INK}" stroke-width="0.5"/></g>'
    if obj.kind.startswith("draw:"):
        return (f'{head}<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" fill="none" '
                f'stroke="{INK}" stroke-width="0.5"/></g>')
    return (f'{head}<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" rx="0.4" fill="#000" '
            f'fill-opacity="0.04" stroke="#777" stroke-width="0.3" stroke-dasharray="0.8 0.6"/></g>')


def render_svg(template: Path, row: dict) -> str:
    """Skizze des Labels mit den Werten aus row als eigenständiges SVG (inline einbettbar)."""
    tpl = lbx.parse(template)
    length, tape = tpl.length_pt, tpl.tape_pt
    m = tpl.margins
    # landscape: left/right quer zum Band, top/bottom an den Enden
    if tpl.orientation == "portrait":
        mx0, mx1, my0, my1 = m["left"], m["right"], m["top"], m["bottom"]
    else:
        mx0, mx1, my0, my1 = m["top"], m["bottom"], m["left"], m["right"]

    clip = f"band-clip-{next(_ids)}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="label-skizze" '
        f'viewBox="{_n(-PAD)} {_n(-PAD)} {_n(length + 2 * PAD)} {_n(tape + 2 * PAD)}" '
        f'width="{_n((length + 2 * PAD) * SCALE)}" height="{_n((tape + 2 * PAD) * SCALE)}" '
        f'style="max-width:100%;height:auto" role="img" '
        f'aria-label={_attr(f"Vorschau {tpl.file}: {tpl.length_mm} × {tpl.tape_mm} mm (Skizze)")}>',
        f"<title>{_txt(tpl.file)} – {_txt(tpl.length_mm)} × {_txt(tpl.tape_mm)} mm (Skizze ohne b-PAC)</title>",
        # Band: weiß, Rand grau – in beiden Themes sichtbar
        f'<rect class="band" x="0" y="0" width="{_n(length)}" height="{_n(tape)}" rx="1" '
        f'fill="#fff" stroke="#888" stroke-width="0.4"/>',
        # bedruckbarer Bereich angedeutet
        f'<rect class="rand" x="{_n(mx0)}" y="{_n(my0)}" width="{_n(max(0, length - mx0 - mx1))}" '
        f'height="{_n(max(0, tape - my0 - my1))}" fill="none" stroke="#8ab4e0" stroke-width="0.25" '
        f'stroke-dasharray="1 1"/>',
        f'<clipPath id="{clip}"><rect x="0" y="0" width="{_n(length)}" height="{_n(tape)}"/></clipPath>',
        f'<g clip-path="url(#{clip})">',
    ]
    for obj in tpl.objects:
        if obj.kind == "text":
            parts.append(_text(obj, _value(obj, row)))
        elif obj.kind == "barcode":
            parts.append(_barcode(obj, _value(obj, row)))
        else:
            parts.append(_other(obj))
    parts.append("</g></svg>")
    return "".join(parts)
