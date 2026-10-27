"""
covers.py - printable DVD covers and Wikipedia QR stickers for Film Catalogue.

Covers are standard DVD case inserts (273 x 183 mm: back 129.5 | spine 14 | front 129.5),
one per A4 landscape page, black & white, text only.

Needs:  reportlab  (Fedora: python3-reportlab)
"""

import html
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

# --------------------------------------------------------------------------- #
# Geometry (standard Amaray DVD case insert)
# --------------------------------------------------------------------------- #
COVER_H = 183 * mm
SPINE_W = 14 * mm
PANEL_W = 129.5 * mm
COVER_W = 2 * PANEL_W + SPINE_W          # 273 mm
PAGE = landscape(A4)
SPINE_MAX_PT = 28                         # biggest text that fits across the spine

# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
BUILTIN = {  # PDF core fonts - work everywhere but only cover Western European characters
    "Helvetica (built-in, Western only)": "Helvetica",
    "Helvetica Bold (built-in, Western only)": "Helvetica-Bold",
    "Times (built-in, Western only)": "Times-Roman",
    "Times Bold (built-in, Western only)": "Times-Bold",
}
EXTRA_FONT_DIRS = ["/run/host/usr/share/fonts", "/run/host/usr/local/share/fonts"]  # host fonts inside distrobox
REGULAR_STYLES = {"regular", "book", "normal", "roman", "medium"}
_font_cache = None


def discover_fonts():
    """{display name: .ttf path} for every TrueType font fontconfig can see."""
    global _font_cache
    if _font_cache is not None:
        return _font_cache
    found = {}
    fmt = "%{family[0]}|%{style[0]}|%{file}\n"
    cmds = [["fc-list", "--format", fmt]]
    cmds += [["fc-scan", "--format", fmt, d] for d in EXTRA_FONT_DIRS if os.path.isdir(d)]
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) != 3 or not parts[2].lower().endswith(".ttf"):
                continue
            fam, style, path = (p.strip() for p in parts)
            name = fam if style.lower() in REGULAR_STYLES else f"{fam} {style}"
            found.setdefault(name, path)
    if not found:  # no fontconfig - scan by hand
        dirs = ["/usr/share/fonts", Path.home() / ".local/share/fonts", Path.home() / ".fonts"]
        for d in [*dirs, *EXTRA_FONT_DIRS]:
            if os.path.isdir(d):
                for p in Path(d).rglob("*.ttf"):
                    found.setdefault(p.stem.replace("-", " ").replace("_", " "), str(p))
    _font_cache = dict(sorted(found.items(), key=lambda kv: kv[0].casefold()))
    return _font_cache


def font_names():
    return list(discover_fonts()) + list(BUILTIN)


_registered = {}


def register_font(display_name):
    """Return a reportlab font name for a display name (registering TTFs on demand)."""
    if display_name in BUILTIN:
        return BUILTIN[display_name]
    path = discover_fonts().get(display_name)
    if not path:
        raise KeyError(f"Font not found: {display_name}")
    if path not in _registered:
        internal = f"F{len(_registered)}_" + re.sub(r"\W", "", display_name)[:30]
        pdfmetrics.registerFont(TTFont(internal, path))
        _registered[path] = internal
    return _registered[path]


def pick_font(preferences, want_bold, serif):
    names = discover_fonts()
    for p in preferences:
        if p in names:
            return p
    for n in names:  # anything vaguely suitable
        low = n.casefold()
        if ("bold" in low) == want_bold and ("serif" in low) == serif and "italic" not in low:
            return n
    if names:
        return next(iter(names))
    return ("Times Bold" if serif else "Helvetica Bold") + " (built-in, Western only)" if want_bold else \
           ("Times" if serif else "Helvetica") + " (built-in, Western only)"


SERIF_B = ["DejaVu Serif Bold", "Liberation Serif Bold", "Noto Serif Bold"]
SERIF = ["DejaVu Serif", "Liberation Serif", "Noto Serif"]
SANS_B = ["DejaVu Sans Bold", "Liberation Sans Bold", "Noto Sans Bold"]
SANS = ["DejaVu Sans", "Liberation Sans", "Noto Sans"]

# key, label, font preferences, bold, serif, default max size (pt)
ELEMENTS = [
    ("front_title", "Front - title", SERIF_B, True, True, 40),
    ("front_year", "Front - year", SERIF, False, True, 16),
    ("front_cast", "Front - cast", SANS, False, False, 11),
    ("front_genre", "Front - genre", SANS, False, False, 10),
    ("spine_title", "Spine - title", SANS_B, True, False, 18),
    ("spine_info", "Spine - year & genre", SANS, False, False, 9),
    ("back_title", "Back - title", SERIF_B, True, True, 20),
    ("back_summary", "Back - summary", SERIF, False, True, 10),
    ("back_label", "Back - field labels", SANS_B, True, False, 7.5),
    ("back_text", "Back - field values", SANS, False, False, 8),
]

LABELS = {
    "English": {"directors": "Director", "writers": "Writers", "composers": "Music",
                "cast_list": "Cast", "genres": "Genre", "countries": "Country",
                "languages": "Language", "min": "min", "wiki": "Wikipedia"},
}


def default_settings():
    return {
        "fonts": {k: {"font": pick_font(prefs, bold, serif), "size": size}
                  for k, _, prefs, bold, serif, size in ELEMENTS},
        "spine_dir": "ttb",
        "black_spine": False,
        "front_frame": True,
        "cast_count": 4,
        "labels": "English",
        "crop_marks": True,
    }


def load_settings(db, key, defaults):
    try:
        saved = json.loads(db.get_setting(key, "") or "{}")
    except ValueError:
        saved = {}
    merged = dict(defaults, **{k: v for k, v in saved.items() if k != "fonts"})
    if "fonts" in defaults:
        merged["fonts"] = {k: dict(v, **saved.get("fonts", {}).get(k, {}))
                           for k, v in defaults["fonts"].items()}
    return merged


def resolve_styles(settings):
    """{element: (reportlab font name, max size)} plus a list of warnings."""
    styles, warnings = {}, []
    defaults = default_settings()["fonts"]
    for key, label, *_ in ELEMENTS:
        cfg = settings["fonts"][key]
        try:
            name = register_font(cfg["font"])
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{label}: {e} - using default font")
            name = register_font(defaults[key]["font"])
        styles[key] = (name, float(cfg["size"]))
    return styles, warnings


# --------------------------------------------------------------------------- #
# Text fitting
# --------------------------------------------------------------------------- #
def tw(text, font, size, cs=0.0):
    return pdfmetrics.stringWidth(text, font, size) + cs * max(len(text) - 1, 0)


def wrap(tokens, sep, font, size, width, cs=0.0):
    lines, cur = [], ""
    for t in tokens:
        cand = f"{cur}{sep}{t}" if cur else t
        if not cur or tw(cand, font, size, cs) <= width:
            cur = cand
        else:
            lines.append(cur)
            cur = t
    if cur:
        lines.append(cur)
    return lines


def ellipsize(line, font, size, width, cs=0.0, force=False):
    if not force and tw(line, font, size, cs) <= width:
        return line
    while line and tw(line.rstrip(" ·,") + "…", font, size, cs) > width:
        line = line[:-1]
    return line.rstrip(" ·,") + "…"


def fit(text, font, max_size, width, max_lines, min_size=6.0, cs=0.0, tokens=None, sep=" "):
    """Shrink the font until the text fits in max_lines; truncate with … as last resort."""
    tokens = tokens if tokens is not None else text.split()
    size = max_size
    while True:
        lines = wrap(tokens, sep, font, size, width, cs)
        ok = len(lines) <= max_lines and all(tw(l, font, size, cs) <= width for l in lines)
        if ok or size <= min_size:
            break
        size = max(min_size, size - 0.5)
    if not ok:
        cut = len(lines) > max_lines
        lines = lines[:max_lines]
        lines = [ellipsize(l, font, size, width, cs) for l in lines]
        if cut:
            lines[-1] = ellipsize(lines[-1], font, size, width, cs, force=True)
    return lines, size


def para_style(font, size, align=TA_LEFT, leading=1.25):
    return ParagraphStyle("p", fontName=font, fontSize=size, leading=size * leading,
                          alignment=align)


def esc(s):
    return html.escape(str(s), quote=False)


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def draw_qr(c, url, x, y, size):
    w = QrCodeWidget(url, barLevel="M")
    w.barBorder = 2
    x1, y1, x2, y2 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def split_list(s):
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def draw_front(c, f, x, y, S, opt):
    cx, m = x + PANEL_W / 2, 13 * mm
    w = PANEL_W - 2 * m
    if opt["front_frame"]:
        c.setLineWidth(0.9)
        c.rect(x + 5 * mm, y + 5 * mm, PANEL_W - 10 * mm, COVER_H - 10 * mm)
        c.setLineWidth(0.3)
        c.rect(x + 6.6 * mm, y + 6.6 * mm, PANEL_W - 13.2 * mm, COVER_H - 13.2 * mm)

    # cast - top
    region_top = y + COVER_H - 18 * mm
    cast = split_list(f.get("cast_list"))[: int(opt["cast_count"])]
    if cast:
        font, size = S["front_cast"]
        cs = size * 0.08
        names = [n.upper() for n in cast]
        lines, size = fit("", font, size, w, 3, 6, cs, tokens=names, sep="  ·  ")
        lead = size * 1.45
        yb = region_top - size * 0.72
        for l in lines:
            c.setFont(font, size)
            c.drawCentredString(cx, yb, l, charSpace=cs)
            yb -= lead
        region_top = yb + lead - size * 0.3 - 8 * mm

    # genre - bottom
    region_bottom = y + 17 * mm
    genres = split_list(f.get("genres"))
    if genres:
        font, size = S["front_genre"]
        cs = size * 0.15
        lines, size = fit("", font, size, w, 2, 6, cs, tokens=[g.upper() for g in genres], sep="  ·  ")
        lead = size * 1.4
        yb = region_bottom + (len(lines) - 1) * lead
        c.setFont(font, size)
        for l in lines:
            c.drawCentredString(cx, yb, l, charSpace=cs)
            yb -= lead
        region_bottom += (len(lines) - 1) * lead + size + 8 * mm

    # title + year - centred in what's left
    tfont, tsize = S["front_title"]
    avail_h = region_top - region_bottom
    max_lines = 5
    while True:
        lines, tsize = fit(f["title"], tfont, tsize, w, max_lines, 12)
        lead = tsize * 1.12
        title_h = tsize * 0.72 + (len(lines) - 1) * lead
        if title_h < avail_h * 0.7 or tsize <= 12:
            break
        tsize -= 1
    year = str(f["year"]) if f.get("year") else ""
    yfont, ysize = S["front_year"]
    gap = 7 * mm
    group_h = title_h + 2 * gap + (gap * 0.9 + ysize * 0.72 if year else 0)
    top = (region_top + region_bottom) / 2 + group_h / 2

    c.setLineWidth(0.6)
    c.line(cx - 12 * mm, top, cx + 12 * mm, top)
    yb = top - gap - tsize * 0.72
    c.setFont(tfont, tsize)
    for l in lines:
        c.drawCentredString(cx, yb, l)
        yb -= lead
    rule = yb + lead - gap
    c.line(cx - 12 * mm, rule, cx + 12 * mm, rule)
    if year:
        c.setFont(yfont, ysize)
        c.drawCentredString(cx, rule - gap * 0.9 - ysize * 0.72, year, charSpace=ysize * 0.2)


def draw_spine(c, f, x, y, S, opt):
    if opt["black_spine"]:
        c.setFillGray(0)
        c.rect(x, y, SPINE_W, COVER_H, stroke=0, fill=1)
        c.setFillGray(1)
    c.saveState()
    if opt["spine_dir"] == "ttb":
        c.translate(x + SPINE_W / 2, y + COVER_H)
        c.rotate(-90)
    else:
        c.translate(x + SPINE_W / 2, y)
        c.rotate(90)
    length, m = COVER_H, 9 * mm

    info_parts = [str(f["year"])] if f.get("year") else []
    info_parts += split_list(f.get("genres"))[:2]
    info_w = 0
    if info_parts:
        ifont, isize = S["spine_info"]
        isize = min(isize, SPINE_MAX_PT * 0.6)
        ilines, isize = fit("", ifont, isize, (length - 2 * m) * 0.4, 1, 5, tokens=info_parts, sep="  ·  ")
        c.setFont(ifont, isize)
        c.drawRightString(length - m, -isize * 0.36, ilines[0])
        info_w = tw(ilines[0], ifont, isize) + 7 * mm

    tfont, tsize = S["spine_title"]
    tsize = min(tsize, SPINE_MAX_PT)
    tlines, tsize = fit(f["title"], tfont, tsize, length - 2 * m - info_w, 1, 6)
    c.setFont(tfont, tsize)
    c.drawString(m, -tsize * 0.36, tlines[0])
    c.restoreState()
    c.setFillGray(0)


def fit_paragraph(text, font, max_size, width, height, min_size=6.5, align=TA_JUSTIFY):
    """Biggest paragraph that fits; drops trailing sentences if even min_size is too big."""
    size = max_size
    while True:
        p = Paragraph(esc(text), para_style(font, size, align, 1.3))
        if p.wrap(width, height)[1] <= height:
            return p
        if size > min_size:
            size = max(min_size, size - 0.25)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if len(sentences) > 1:
            text = " ".join(sentences[:-1])
        else:
            words = text.split()
            if len(words) <= 3:
                return None
            text = " ".join(words[: int(len(words) * 0.85)]) + "…"


def draw_back(c, f, x, y, S, opt):
    L = LABELS.get(opt["labels"], LABELS["English"])
    m = 11 * mm
    left, w, top = x + m, PANEL_W - 2 * m, y + COVER_H - m

    # header
    tfont, tsize = S["back_title"]
    lines, tsize = fit(f["title"], tfont, tsize, w, 2, 10)
    yb = top - tsize * 0.75
    c.setFont(tfont, tsize)
    for l in lines:
        c.drawString(left, yb, l)
        yb -= tsize * 1.15
    cur = yb + tsize * 1.15 - tsize * 0.3

    sub = []
    if f.get("original_title") and f["original_title"] != f["title"]:
        sub.append(f["original_title"])
    if f.get("year"):
        sub.append(str(f["year"]))
    if f.get("runtime"):
        sub.append(f"{f['runtime']} {L['min']}")
    if sub:
        sfont, ssize = S["back_text"]
        ssize += 1
        slines, ssize = fit("", sfont, ssize, w, 1, 6, tokens=sub, sep="  ·  ")
        c.setFont(sfont, ssize)
        cur -= ssize * 1.5
        c.drawString(left, cur, slines[0])
        cur -= ssize * 0.4
    cur -= 3 * mm
    c.setLineWidth(0.5)
    c.line(left, cur, left + w, cur)
    cur -= 4.5 * mm

    # QR - bottom right
    q = 24 * mm
    lfont, lsize = S["back_label"]
    vfont, vsize = S["back_text"]
    qr_top = y + m
    if f.get("wiki_url"):
        cap = 6.5
        draw_qr(c, f["wiki_url"], left + w - q + 1.5 * mm, y + m + cap + 1 * mm, q)
        c.setFont(lfont, cap)
        c.drawCentredString(left + w - q / 2 + 1.5 * mm, y + m, L["wiki"])
        qr_top = y + m + cap + 1 * mm + q

    # credits - bottom left, beside the QR
    fields = [(k, f.get(k)) for k in ("directors", "writers", "composers", "cast_list",
                                       "genres", "countries", "languages") if f.get(k)]
    cw = w - q - 5 * mm if f.get("wiki_url") else w
    credits_top = y + m
    if fields:
        max_h = (cur - (y + m)) * 0.5
        ls, vs = lsize, vsize
        while True:
            paras = [Paragraph(f'<font name="{lfont}" size="{ls}">{esc(L[k].upper())}</font>'
                               f'&nbsp;&nbsp;{esc(v)}', para_style(vfont, vs, TA_LEFT, 1.25))
                     for k, v in fields]
            heights = [p.wrap(cw, 10000)[1] for p in paras]
            total = sum(heights) + 1.6 * (len(paras) - 1)
            if total <= max_h or vs <= 5.5:
                break
            ls, vs = max(5, ls - 0.25), max(5.5, vs - 0.25)
        yy = y + m + total
        credits_top = yy
        for p, h in zip(paras, heights):
            p.drawOn(c, left, yy - h)
            yy -= h + 1.6

    # summary - fills the middle
    bottom = max(credits_top, qr_top) + 6 * mm
    if f.get("extract") and cur - bottom > 15 * mm:
        sfont, ssize = S["back_summary"]
        p = fit_paragraph(f["extract"], sfont, ssize, w, cur - bottom)
        if p:
            _, ph = p.wrap(w, cur - bottom)
            p.drawOn(c, left, cur - ph)


def draw_marks(c, ox, oy, label):
    c.setStrokeGray(0)
    c.setLineWidth(0.25)
    g, ln = 2 * mm, 6 * mm
    for X, dx in ((ox, -1), (ox + COVER_W, 1)):
        for Y, dy in ((oy, -1), (oy + COVER_H, 1)):
            c.line(X + dx * g, Y, X + dx * (g + ln), Y)
            c.line(X, Y + dy * g, X, Y + dy * (g + ln))
    c.setDash(1.5, 1.5)
    for X in (ox + PANEL_W, ox + PANEL_W + SPINE_W):
        c.line(X, oy - g, X, oy - g - ln)
        c.line(X, oy + COVER_H + g, X, oy + COVER_H + g + ln)
    c.setDash()
    c.setFillGray(0.45)
    c.setFont("Helvetica", 6)
    c.drawString(ox + 10 * mm, oy - 7 * mm,
                 "Print at 100% / actual size  -  cut at the corner marks, fold at the dashed marks")
    c.setFillGray(0)


def make_covers_pdf(path, films, settings):
    S, warnings = resolve_styles(settings)
    c = canvas.Canvas(str(path), pagesize=PAGE)
    c.setTitle("DVD covers")
    c.setAuthor("Film Catalogue")
    ox, oy = (PAGE[0] - COVER_W) / 2, (PAGE[1] - COVER_H) / 2
    for f in films:
        draw_back(c, f, ox, oy, S, settings)
        draw_spine(c, f, ox + PANEL_W, oy, S, settings)
        draw_front(c, f, ox + PANEL_W + SPINE_W, oy, S, settings)
        if settings["crop_marks"]:
            draw_marks(c, ox, oy, f["title"])
        c.showPage()
    c.save()
    return warnings


# --------------------------------------------------------------------------- #
# QR stickers
# --------------------------------------------------------------------------- #
def default_sticker_settings():
    return {"size": 25, "caption": True, "outlines": True,
            "font": pick_font(SANS, False, False), "font_size": 7}


def make_stickers_pdf(path, films, opt):
    films = [f for f in films if f.get("wiki_url")]
    try:
        font = register_font(opt["font"])
    except Exception:  # noqa: BLE001
        font = register_font(default_sticker_settings()["font"])
    q = float(opt["size"]) * mm
    pad = 2.5 * mm if opt["outlines"] else 0
    fsize = float(opt["font_size"])
    cap_h = (2 * fsize * 1.2 + 1.5 * mm) if opt["caption"] else 0
    cell_w, cell_h = q + 2 * pad, q + cap_h + 2 * pad
    gap = 3 * mm
    W, H = A4
    margin = 10 * mm
    cols = max(1, int((W - 2 * margin + gap) // (cell_w + gap)))
    rows = max(1, int((H - 2 * margin + gap) // (cell_h + gap)))
    x0 = (W - (cols * cell_w + (cols - 1) * gap)) / 2

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("Wikipedia QR stickers")
    for i, f in enumerate(films):
        slot = i % (cols * rows)
        if i and slot == 0:
            c.showPage()
        r, col = divmod(slot, cols)
        x = x0 + col * (cell_w + gap)
        y = H - margin - (r + 1) * cell_h - r * gap
        if opt["outlines"]:
            c.setStrokeGray(0.6)
            c.setLineWidth(0.3)
            c.setDash(2, 2)
            c.roundRect(x, y, cell_w, cell_h, 2 * mm)
            c.setDash()
            c.setStrokeGray(0)
        draw_qr(c, f["wiki_url"], x + pad, y + pad + cap_h, q)
        if opt["caption"]:
            title = f["title"] + (f" ({f['year']})" if f.get("year") else "")
            lines, s = fit(title, font, fsize, cell_w - 2 * mm, 2, 4)
            c.setFont(font, s)
            yb = y + pad + cap_h - s * 1.05
            for l in lines:
                c.drawCentredString(x + cell_w / 2, yb, l)
                yb -= s * 1.2
    c.save()
    return len(films), cols * rows


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #
def _dialog(parent, title):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    frm = ttk.Frame(dlg, padding=12)
    frm.pack(fill="both", expand=True)
    return dlg, frm


def open_cover_dialog(parent, films, db, data_dir, open_path):
    parent.config(cursor="watch")
    parent.update()
    names = font_names()  # first call scans the system fonts
    parent.config(cursor="")
    st = load_settings(db, "cover_settings", default_settings())

    dlg, frm = _dialog(parent, f"DVD covers - {len(films)} film(s)")

    fonts_box = ttk.LabelFrame(frm, text="Fonts  (size = maximum; long text shrinks to fit)", padding=8)
    fonts_box.grid(row=0, column=0, sticky="nsew")
    font_vars = {}
    for i, (key, label, *_rest) in enumerate(ELEMENTS):
        ttk.Label(fonts_box, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
        fv = tk.StringVar(value=st["fonts"][key]["font"])
        ttk.Combobox(fonts_box, textvariable=fv, values=names, width=34).grid(row=i, column=1, pady=2)
        sv = tk.StringVar(value=str(st["fonts"][key]["size"]))
        ttk.Spinbox(fonts_box, textvariable=sv, from_=5, to=72, increment=0.5, width=5).grid(
            row=i, column=2, padx=(6, 0))
        ttk.Label(fonts_box, text="pt").grid(row=i, column=3, padx=(2, 0))
        font_vars[key] = (fv, sv)

    lay = ttk.LabelFrame(frm, text="Layout", padding=8)
    lay.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
    spine = tk.StringVar(value=st["spine_dir"])
    ttk.Label(lay, text="Spine text reads").pack(anchor="w")
    ttk.Radiobutton(lay, text="top → bottom", value="ttb", variable=spine).pack(anchor="w", padx=10)
    ttk.Radiobutton(lay, text="bottom → top", value="btt", variable=spine).pack(anchor="w", padx=10)
    black = tk.BooleanVar(value=st["black_spine"])
    ttk.Checkbutton(lay, text="Black spine, white text", variable=black).pack(anchor="w", pady=(8, 0))
    frame = tk.BooleanVar(value=st["front_frame"])
    ttk.Checkbutton(lay, text="Double frame on front", variable=frame).pack(anchor="w")
    marks = tk.BooleanVar(value=st["crop_marks"])
    ttk.Checkbutton(lay, text="Cut and fold marks", variable=marks).pack(anchor="w")
    row = ttk.Frame(lay)
    row.pack(anchor="w", pady=(8, 0))
    ttk.Label(row, text="Cast names on front").pack(side="left")
    cast = tk.StringVar(value=str(st["cast_count"]))
    ttk.Spinbox(row, textvariable=cast, from_=1, to=8, width=3).pack(side="left", padx=6)
    labels = tk.StringVar(value=st["labels"] if st["labels"] in LABELS else "English")
    if len(LABELS) > 1:  # only offer a choice once more label languages are added
        row2 = ttk.Frame(lay)
        row2.pack(anchor="w", pady=(6, 0))
        ttk.Label(row2, text="Back labels").pack(side="left")
        ttk.Combobox(row2, textvariable=labels, values=list(LABELS), state="readonly",
                     width=9).pack(side="left", padx=6)

    ttk.Label(frm, foreground="#666", justify="left", text=(
        "Standard DVD case insert (273 × 183 mm), one per A4 landscape page.\n"
        "Print at 100% / “Actual size”, not “Fit to page”.")).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def collect():
        try:
            s = {
                "fonts": {k: {"font": fv.get().strip(), "size": float(sv.get())}
                          for k, (fv, sv) in font_vars.items()},
                "spine_dir": spine.get(), "black_spine": black.get(),
                "front_frame": frame.get(), "crop_marks": marks.get(),
                "cast_count": max(1, min(8, int(float(cast.get())))), "labels": labels.get(),
            }
        except ValueError:
            messagebox.showwarning("DVD covers", "Font sizes must be numbers.", parent=dlg)
            return None
        db.set_setting("cover_settings", json.dumps(s))
        return s

    def render(path, subset):
        s = collect()
        if not s:
            return False
        try:
            warnings = make_covers_pdf(path, subset, s)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("DVD covers", f"Could not create the PDF:\n{e}", parent=dlg)
            return False
        if warnings:
            messagebox.showwarning("DVD covers", "\n".join(warnings), parent=dlg)
        open_path(path)
        return True

    def preview():
        render(Path(data_dir) / "cover_preview.pdf", films[:1])

    def save():
        path = filedialog.asksaveasfilename(
            parent=dlg, defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile=f"dvd_covers_{datetime.now():%Y-%m-%d}.pdf")
        if path and render(path, films):
            dlg.destroy()

    def reset():
        d = default_settings()
        for k, (fv, sv) in font_vars.items():
            fv.set(d["fonts"][k]["font"])
            sv.set(str(d["fonts"][k]["size"]))
        spine.set(d["spine_dir"]); black.set(d["black_spine"]); frame.set(d["front_frame"])
        marks.set(d["crop_marks"]); cast.set(str(d["cast_count"])); labels.set(d["labels"])

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    ttk.Button(btns, text="Reset to defaults", command=reset).pack(side="left")
    ttk.Button(btns, text="Close", command=dlg.destroy).pack(side="right")
    ttk.Button(btns, text=f"Save PDF ({len(films)})…", command=save).pack(side="right", padx=6)
    ttk.Button(btns, text=f"Preview “{films[0]['title'][:25]}”", command=preview).pack(side="right")
    dlg.grab_set()


def open_sticker_dialog(parent, films, db, data_dir, open_path):
    names = font_names()
    st = load_settings(db, "sticker_settings", default_sticker_settings())
    with_url = [f for f in films if f.get("wiki_url")]
    dlg, frm = _dialog(parent, f"QR stickers - {len(with_url)} film(s)")

    size = tk.StringVar(value=str(st["size"]))
    caption = tk.BooleanVar(value=st["caption"])
    outlines = tk.BooleanVar(value=st["outlines"])
    font = tk.StringVar(value=st["font"])
    fsize = tk.StringVar(value=str(st["font_size"]))

    r = ttk.Frame(frm)
    r.grid(row=0, column=0, sticky="w")
    ttk.Label(r, text="QR code size").pack(side="left")
    ttk.Spinbox(r, textvariable=size, from_=12, to=80, width=4).pack(side="left", padx=6)
    ttk.Label(r, text="mm").pack(side="left")
    ttk.Checkbutton(frm, text="Title and year under the code", variable=caption).grid(
        row=1, column=0, sticky="w", pady=(8, 0))
    r2 = ttk.Frame(frm)
    r2.grid(row=2, column=0, sticky="w", padx=(20, 0))
    ttk.Combobox(r2, textvariable=font, values=names, width=30).pack(side="left")
    ttk.Spinbox(r2, textvariable=fsize, from_=4, to=14, increment=0.5, width=4).pack(side="left", padx=6)
    ttk.Label(r2, text="pt").pack(side="left")
    ttk.Checkbutton(frm, text="Dashed cutting outlines", variable=outlines).grid(
        row=3, column=0, sticky="w", pady=(4, 0))
    if len(with_url) < len(films):
        ttk.Label(frm, foreground="#a33",
                  text=f"{len(films) - len(with_url)} selected film(s) have no Wikipedia link "
                       "and will be skipped.").grid(row=4, column=0, sticky="w", pady=(8, 0))

    def save():
        try:
            s = {"size": float(size.get()), "caption": caption.get(), "outlines": outlines.get(),
                 "font": font.get().strip(), "font_size": float(fsize.get())}
        except ValueError:
            messagebox.showwarning("QR stickers", "Sizes must be numbers.", parent=dlg)
            return
        db.set_setting("sticker_settings", json.dumps(s))
        path = filedialog.asksaveasfilename(
            parent=dlg, defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile=f"qr_stickers_{datetime.now():%Y-%m-%d}.pdf")
        if not path:
            return
        try:
            make_stickers_pdf(path, with_url, s)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("QR stickers", f"Could not create the PDF:\n{e}", parent=dlg)
            return
        dlg.destroy()
        open_path(path)

    b = ttk.Frame(frm)
    b.grid(row=5, column=0, sticky="e", pady=(12, 0))
    ttk.Button(b, text="Close", command=dlg.destroy).pack(side="right")
    save_btn = ttk.Button(b, text="Save PDF…", command=save)
    save_btn.pack(side="right", padx=6)
    if not with_url:
        save_btn.state(["disabled"])
    dlg.grab_set()
