#!/usr/bin/env python3
"""
Film Catalogue - a small desktop app for tracking a physical movie collection.

You paste a Wikipedia link, pick the format and storage location, and the app
downloads the rest (title, year, director, cast, genres, runtime, poster ...)
from Wikipedia and Wikidata. Everything is stored locally in an SQLite file, so
after a film has been added the app works fully offline.

Requirements: Python 3.9+ (standard library only).
Optional:     Pillow  ->  pip install pillow   (poster previews + smaller exports)

Run:          python film_catalogue.py
"""

import base64
import csv
import html
import io
import json
import queue
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False
try:
    from PIL import ImageTk  # Fedora ships this separately as python3-pillow-tk
    HAVE_PIL_TK = True
except ImportError:
    HAVE_PIL_TK = False

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
APP_NAME = "Film Catalogue"
DATA_DIR = Path(__file__).resolve().parent / "film_catalogue_data"
DB_PATH = DATA_DIR / "films.db"
POSTER_DIR = DATA_DIR / "posters"

# Wikimedia asks API clients to send a descriptive User-Agent.
# Adding your e-mail address here is good etiquette.
USER_AGENT = "FilmCatalogue/1.0 (personal DVD collection manager; Python urllib)"

DEFAULT_FORMATS = ["DVD", "Blu-ray", "4K UHD Blu-ray", "VHS", "Digital", "Other"]
CAST_LIMIT = 8
GENRE_LIMIT = 4

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
ARTICLES = ("the ", "a ", "an ", "az ", "der ", "die ", "das ", "le ", "la ", "les ", "l'")


def natural_key(s):
    """'Shelf 2' sorts before 'Shelf 10'."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s or "")]


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def title_key(title):
    """Sort key that ignores leading articles and accents ('The Godfather' -> G)."""
    s = (title or "").strip().casefold()
    for a in ARTICLES:
        if s.startswith(a) and len(s) > len(a):
            s = s[len(a):]
            break
    return natural_key(fold(s))


def open_path(path):
    """Open a file with the desktop's default app (works from inside distrobox too)."""
    target = str(path)
    for cmd in (["distrobox-host-exec", "xdg-open", target], ["xdg-open", target]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return
            except OSError:
                pass
    webbrowser.open(target if "://" in target else Path(target).resolve().as_uri())


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Wikipedia / Wikidata fetching
# --------------------------------------------------------------------------- #
WIKI_RE = re.compile(r"^https?://([a-z\-]+)\.(?:m\.)?wikipedia\.org/wiki/([^#?]+)", re.I)


def parse_wiki_url(url):
    """Return (language, article_title) from a Wikipedia URL."""
    m = WIKI_RE.match(url.strip())
    if not m:
        raise ValueError("Not a Wikipedia article link")
    return m.group(1).lower(), urllib.parse.unquote(m.group(2)).replace(" ", "_")


def article_key(url):
    """Normalised identity of an article, used for duplicate checks."""
    try:
        lang, title = parse_wiki_url(url)
        return f"{lang}:{title.casefold()}"
    except ValueError:
        return None


MIN_REQUEST_GAP = 1.0      # seconds between any two requests to Wikimedia
MAX_RETRIES = 6
_rate_lock = threading.Lock()
_last_request = 0.0
on_wait = None             # set by the GUI: called with (seconds, reason) before sleeping


def _throttle():
    global _last_request
    with _rate_lock:
        delay = _last_request + MIN_REQUEST_GAP - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _last_request = time.monotonic()


def http_get(url, timeout=25):
    """GET with polite pacing; on 429/503 waits (Retry-After if given) and retries."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == MAX_RETRIES:
                raise
            try:
                wait = float(e.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0
            wait = min(max(wait, 5 * 2 ** attempt), 300)   # 5, 10, 20, 40 … s, max 5 min
            if on_wait:
                on_wait(wait, f"Wikipedia asked us to slow down (HTTP {e.code})")
            time.sleep(wait)


def get_json(url):
    return json.loads(http_get(url).decode("utf-8"))


def wd_claim_values(entity, prop):
    vals = []
    for c in entity.get("claims", {}).get(prop, []):
        if c.get("rank") == "deprecated":
            continue
        dv = c.get("mainsnak", {}).get("datavalue")
        if dv:
            vals.append(dv["value"])
    return vals


def wd_item_ids(entity, prop, limit=None):
    ids = [v["id"] for v in wd_claim_values(entity, prop) if isinstance(v, dict) and "id" in v]
    ids = list(dict.fromkeys(ids))
    return ids[:limit] if limit else ids


def wd_labels(ids, langs):
    """Resolve Wikidata Q-ids to labels, preferring the given languages."""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        q = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]),
            "props": "labels", "languages": "|".join(langs),
            "languagefallback": "1", "format": "json",
        })
        data = get_json("https://www.wikidata.org/w/api.php?" + q)
        for qid, ent in data.get("entities", {}).items():
            labels = ent.get("labels", {})
            for lang in langs:
                if lang in labels:
                    out[qid] = labels[lang]["value"]
                    break
            else:
                if labels:
                    out[qid] = next(iter(labels.values()))["value"]
    return out


UNIT_TO_MIN = {"Q7727": 1.0, "Q11574": 1 / 60, "Q25235": 60.0}  # minute, second, hour


def parse_wikidata(entity, labels):
    """Pull the interesting film properties out of a Wikidata entity."""
    def names(prop, limit=None, strip_film=False):
        out = []
        for q in wd_item_ids(entity, prop, limit):
            name = labels.get(q)
            if not name:
                continue
            if strip_film:
                name = re.sub(r"\s+film$", "", name, flags=re.I)
            out.append(name)
        return ", ".join(out)

    years = []
    for v in wd_claim_values(entity, "P577"):
        m = re.match(r"[+-](\d{4})", v.get("time", "")) if isinstance(v, dict) else None
        if m:
            years.append(int(m.group(1)))

    runtime = None
    for v in wd_claim_values(entity, "P2047"):
        unit = v.get("unit", "").rsplit("/", 1)[-1]
        if unit in UNIT_TO_MIN:
            runtime = round(float(v["amount"]) * UNIT_TO_MIN[unit])
            break

    orig = [v["text"] for v in wd_claim_values(entity, "P1476") if isinstance(v, dict) and "text" in v]

    return {
        "year": min(years) if years else None,
        "runtime": runtime,
        "original_title": orig[0] if orig else None,
        "directors": names("P57"),
        "writers": names("P58"),
        "composers": names("P86"),
        "cast_list": names("P161", CAST_LIMIT),
        "genres": names("P136", GENRE_LIMIT, strip_film=True),
        "countries": names("P495"),
        "languages": names("P364"),
    }


WD_PROPS = [("P57", None), ("P58", None), ("P86", None), ("P161", CAST_LIMIT),
            ("P136", GENRE_LIMIT), ("P495", None), ("P364", None)]


def download_poster(image_url, key):
    if not image_url:
        return None
    ext = Path(urllib.parse.urlparse(image_url).path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    safe = re.sub(r"[^\w\-]+", "_", key)[:80]
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTER_DIR / f"{safe}{ext}"
    path.write_bytes(http_get(image_url, timeout=40))
    return path.relative_to(DATA_DIR).as_posix()


def fetch_film(url):
    """Download all metadata for the film behind a Wikipedia link."""
    lang, title = parse_wiki_url(url)
    s = get_json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                 + urllib.parse.quote(title, safe=""))
    if s.get("type") == "disambiguation":
        raise ValueError("This is a disambiguation page - please link the film's own article")

    page_title = s.get("title") or title.replace("_", " ")
    film = {
        "wiki_url": s.get("content_urls", {}).get("desktop", {}).get("page") or url.strip(),
        "lang": lang,
        "title": re.sub(r"\s*\([^()]*\bfilm\b[^()]*\)$", "", page_title, flags=re.I),
        "description": s.get("description"),
        "extract": s.get("extract"),
        "wikidata_id": s.get("wikibase_item"),
    }

    # The small thumbnail is plenty for previews/exports and is far less rate-limited
    # than full-size originals on upload.wikimedia.org.
    film["image_url"] = (s.get("thumbnail") or {}).get("source")

    qid = film["wikidata_id"]
    if qid:
        data = get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
        ents = data.get("entities", {})
        entity = ents.get(qid) or next(iter(ents.values()))
        ids = [q for prop, lim in WD_PROPS for q in wd_item_ids(entity, prop, lim)]
        langs = list(dict.fromkeys([lang, "en"]))
        labels = wd_labels(ids, langs) if ids else {}
        film.update(parse_wikidata(entity, labels))
        # Clean title in the article's language, e.g. "Heat" rather than "Heat (1995 film)"
        lbl = entity.get("labels", {})
        if lang in lbl:
            film["title"] = lbl[lang]["value"]

    try:
        film["poster_path"] = download_poster(film["image_url"], qid or f"{lang}_{title}")
    except Exception:
        film["poster_path"] = None
    return film


def friendly_error(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "Article not found on Wikipedia"
        if e.code == 429:
            return "Wikipedia is still rate-limiting - try these again in a few minutes"
        return f"HTTP error {e.code}"
    if isinstance(e, urllib.error.URLError):
        return "Could not reach Wikipedia (no internet connection?)"
    return str(e) or e.__class__.__name__


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_url TEXT, lang TEXT, wikidata_id TEXT,
    title TEXT NOT NULL, original_title TEXT, year INTEGER,
    directors TEXT, writers TEXT, composers TEXT, cast_list TEXT, genres TEXT,
    countries TEXT, languages TEXT, runtime INTEGER,
    description TEXT, extract TEXT, image_url TEXT, poster_path TEXT,
    format TEXT, location TEXT, notes TEXT,
    added_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

META_FIELDS = ["wiki_url", "lang", "wikidata_id", "title", "original_title", "year",
               "directors", "writers", "composers", "cast_list", "genres", "countries",
               "languages", "runtime", "description", "extract", "image_url", "poster_path"]


class Database:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def add(self, film, fmt, location, notes=""):
        cols = META_FIELDS + ["format", "location", "notes", "added_at", "updated_at"]
        vals = [film.get(c) for c in META_FIELDS] + [fmt, location, notes, now_iso(), now_iso()]
        cur = self.con.execute(
            f"INSERT INTO films ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self.con.commit()
        return cur.lastrowid

    def update(self, fid, fields):
        fields = dict(fields, updated_at=now_iso())
        sets = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE films SET {sets} WHERE id=?", [*fields.values(), fid])
        self.con.commit()

    def delete(self, fid):
        self.con.execute("DELETE FROM films WHERE id=?", (fid,))
        self.con.commit()

    def get(self, fid):
        return self.con.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()

    def all(self):
        return self.con.execute("SELECT * FROM films").fetchall()

    def distinct(self, col):
        vals = [r[0] for r in self.con.execute(
            f"SELECT DISTINCT {col} FROM films WHERE {col} IS NOT NULL AND {col} != ''")]
        return sorted(vals, key=lambda v: natural_key(fold(v)))

    def poster_in_use(self, rel_path):
        return self.con.execute("SELECT COUNT(*) FROM films WHERE poster_path=?",
                                (rel_path,)).fetchone()[0] > 0

    def get_setting(self, key, default=""):
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self.con.commit()


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def poster_data_uri(rel_path):
    if not rel_path:
        return None
    path = DATA_DIR / rel_path
    if not path.exists():
        return None
    try:
        if HAVE_PIL:
            img = Image.open(path).convert("RGB")
            img.thumbnail((180, 270))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            data, mime = buf.getvalue(), "image/jpeg"
        else:
            data = path.read_bytes()
            mime = {".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception:
        return None


CATALOGUE_CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", Helvetica, Arial, sans-serif; font-size: 10pt; color: #111; margin: 0 auto; max-width: 190mm; }
header { border-bottom: 2px solid #111; margin-bottom: 10px; padding-bottom: 4px; }
header h1 { font-size: 20pt; margin: 0; letter-spacing: .5px; }
header p { margin: 2px 0 0; color: #555; font-size: 9pt; }
h2 { font-size: 13pt; margin: 18px 0 4px; padding: 3px 6px; background: #eee; border-left: 4px solid #111; break-after: avoid; }
h2 small { font-weight: normal; color: #555; font-size: 9pt; }
table { width: 100%; border-collapse: collapse; }
thead th { text-align: left; font-size: 8.5pt; text-transform: uppercase; color: #555; border-bottom: 1px solid #999; padding: 3px 4px; }
td { padding: 4px; border-bottom: 1px solid #ddd; vertical-align: top; }
tr { break-inside: avoid; }
td.n { color: #999; width: 2.2em; text-align: right; }
td.t b { font-size: 10.5pt; }
td.t .o { color: #555; font-style: italic; }
td.t .d { color: #555; font-size: 8.5pt; margin-top: 2px; }
td.y, td.r { white-space: nowrap; }
td.p { width: 52px; }
td.p img { width: 48px; display: block; }
.badge { display: inline-block; border: 1px solid #999; border-radius: 3px; padding: 0 4px; font-size: 8.5pt; white-space: nowrap; }
@media screen { body { padding: 20px; } .hint { background: #fffbe6; border: 1px solid #e6d77a; padding: 6px 10px; margin-bottom: 12px; font-size: 9pt; } }
@media print { .hint { display: none; } }
"""


def build_catalogue_html(rows, group_by="location", show_posters=False, show_details=True):
    esc = lambda v: html.escape(str(v)) if v not in (None, "") else ""
    rows = sorted(rows, key=lambda r: title_key(r["title"]))

    if group_by in ("location", "format"):
        groups = {}
        for r in rows:
            groups.setdefault(r[group_by] or "(not set)", []).append(r)
        group_list = sorted(groups.items(), key=lambda kv: natural_key(fold(kv[0])))
    else:
        group_list = [(None, rows)]

    cols = []
    if show_posters:
        cols.append("")
    cols += ["Title", "Year", "Director", "Runtime"]
    if group_by != "format":
        cols.append("Format")
    if group_by != "location":
        cols.append("Location")

    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'>",
           f"<title>{APP_NAME}</title><style>{CATALOGUE_CSS}</style></head><body>",
           "<div class='hint'>Press <b>Ctrl+P</b> (<b>Cmd+P</b> on Mac) to print or save as PDF. "
           "Turn on &ldquo;Background graphics&rdquo; for shaded headings.</div>",
           f"<header><h1>{APP_NAME}</h1><p>{len(rows)} films &middot; "
           f"generated {datetime.now():%Y-%m-%d}</p></header>"]

    n = 0
    for name, items in group_list:
        if name is not None:
            out.append(f"<h2>{esc(name)} <small>({len(items)})</small></h2>")
        out.append("<table><thead><tr><th></th>"
                   + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>")
        for r in items:
            n += 1
            cells = [f"<td class='n'>{n}</td>"]
            if show_posters:
                uri = poster_data_uri(r["poster_path"])
                cells.append(f"<td class='p'>{f'<img src={chr(39)}{uri}{chr(39)}>' if uri else ''}</td>")
            t = f"<b>{esc(r['title'])}</b>"
            if r["original_title"] and r["original_title"] != r["title"]:
                t += f" <span class='o'>({esc(r['original_title'])})</span>"
            if show_details:
                bits = [b for b in (r["genres"], r["cast_list"]) if b]
                if bits:
                    t += f"<div class='d'>{' &middot; '.join(esc(b) for b in bits)}</div>"
                if r["notes"]:
                    t += f"<div class='d'>Note: {esc(r['notes'])}</div>"
            cells.append(f"<td class='t'>{t}</td>")
            cells.append(f"<td class='y'>{esc(r['year'])}</td>")
            cells.append(f"<td>{esc(r['directors'])}</td>")
            cells.append(f"<td class='r'>{esc(r['runtime']) + ' min' if r['runtime'] else ''}</td>")
            if group_by != "format":
                cells.append(f"<td><span class='badge'>{esc(r['format'])}</span></td>")
            if group_by != "location":
                cells.append(f"<td>{esc(r['location'])}</td>")
            out.append("<tr>" + "".join(cells) + "</tr>")
        out.append("</tbody></table>")
    out.append("</body></html>")
    return "\n".join(out)


CSV_COLUMNS = [("title", "Title"), ("original_title", "Original title"), ("year", "Year"),
               ("directors", "Director"), ("writers", "Writers"), ("composers", "Music"),
               ("cast_list", "Cast"), ("genres", "Genres"), ("countries", "Country"),
               ("languages", "Language"), ("runtime", "Runtime (min)"), ("format", "Format"),
               ("location", "Location"), ("notes", "Notes"), ("wiki_url", "Wikipedia"),
               ("added_at", "Added")]


def write_csv(rows, path):
    rows = sorted(rows, key=lambda r: title_key(r["title"]))
    # UTF-8 with BOM + semicolons: opens correctly in Excel with European regional settings.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([h for _, h in CSV_COLUMNS])
        for r in rows:
            w.writerow(["" if r[c] is None else r[c] for c, _ in CSV_COLUMNS])


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
ALL_LOC, ALL_FMT = "All locations", "All formats"
TREE_COLS = [("title", "Title", 300), ("year", "Year", 55), ("directors", "Director", 170),
             ("format", "Format", 100), ("location", "Location", 130), ("runtime", "Min", 50)]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1200x720")
        self.minsize(950, 560)
        self.db = Database(DB_PATH)
        self.q = queue.Queue()
        self.busy = 0
        self.errors = []
        self.sort_col, self.sort_rev = "title", False
        self._poster_img = None
        self._visible_rows = []

        global on_wait
        on_wait = lambda secs, why: self.q.put(("wait", secs, why))
        self._build_ui()
        self.refresh_list()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- layout ---------------------------------------------------------- #
    def _build_ui(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names() and self.tk.call("tk", "windowingsystem") == "x11":
            style.theme_use("clam")
        style.configure("Treeview", rowheight=24)

        # Add-films panel
        add = ttk.LabelFrame(self, text="Add films", padding=8)
        add.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(add, text="Wikipedia link(s) - one per line:").grid(row=0, column=0, sticky="w")
        self.url_text = tk.Text(add, height=3, width=70, wrap="none", undo=True)
        self.url_text.grid(row=1, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        self.url_text.bind("<Control-Return>", lambda e: (self.add_films(), "break")[1])

        ttk.Label(add, text="Format").grid(row=0, column=1, sticky="w")
        self.fmt_var = tk.StringVar(value=self.db.get_setting("last_format", DEFAULT_FORMATS[0]))
        self.fmt_box = ttk.Combobox(add, textvariable=self.fmt_var, width=16)
        self.fmt_box.grid(row=1, column=1, sticky="ew")

        ttk.Label(add, text="Storage location").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.loc_var = tk.StringVar(value=self.db.get_setting("last_location", ""))
        self.loc_box = ttk.Combobox(add, textvariable=self.loc_var, width=22)
        self.loc_box.grid(row=1, column=2, sticky="ew", padx=(8, 0))

        self.add_btn = ttk.Button(add, text="Add  (Ctrl+Enter)", command=self.add_films)
        self.add_btn.grid(row=1, column=3, sticky="ew", padx=(8, 0))
        ttk.Label(add, foreground="#666",
                  text="Tip: paste a whole shelf at once - all links get the same format and location."
                  ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(add, text="Manual entry…", command=lambda: self.film_editor(None)).grid(
            row=2, column=3, sticky="ew", padx=(8, 0), pady=(4, 0))
        add.columnconfigure(0, weight=1)

        # Toolbar: filters + actions
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh_list())
        ttk.Entry(bar, textvariable=self.search_var, width=26).pack(side="left", padx=(4, 10))
        self.loc_filter = ttk.Combobox(bar, state="readonly", width=20)
        self.loc_filter.pack(side="left")
        self.fmt_filter = ttk.Combobox(bar, state="readonly", width=16)
        self.fmt_filter.pack(side="left", padx=6)
        for cb in (self.loc_filter, self.fmt_filter):
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())

        # Action buttons on their own row, so none get pushed off-screen
        act = ttk.Frame(self, padding=(10, 0, 10, 4))
        act.pack(fill="x")
        ttk.Button(act, text="Edit…", command=self.edit_selected).pack(side="left")
        ttk.Button(act, text="Delete", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(act, text="Re-download", command=self.refetch_selected).pack(side="left")
        ttk.Separator(act, orient="vertical").pack(side="left", fill="y", padx=10)
        cov = ttk.Menubutton(act, text="Covers ▾")
        cmenu = tk.Menu(cov, tearoff=False)
        cmenu.add_command(label="DVD covers for selected films…", command=self.cover_dialog)
        cmenu.add_command(label="Wikipedia QR stickers for selected films…",
                          command=self.sticker_dialog)
        cov["menu"] = cmenu
        cov.pack(side="left")
        exp = ttk.Menubutton(act, text="Export ▾")
        menu = tk.Menu(exp, tearoff=False)
        menu.add_command(label="Printable catalogue (HTML/PDF)…", command=self.export_dialog)
        menu.add_command(label="Spreadsheet (CSV)…", command=self.export_csv)
        exp["menu"] = menu
        exp.pack(side="left", padx=4)
        ttk.Button(act, text="Statistics", command=self.show_stats).pack(side="left")

        # Right-click menu on the film list
        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="Edit…", command=self.edit_selected)
        self.ctx.add_command(label="Re-download from Wikipedia", command=self.refetch_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="DVD covers…", command=self.cover_dialog)
        self.ctx.add_command(label="QR stickers…", command=self.sticker_dialog)
        self.ctx.add_separator()
        self.ctx.add_command(label="Delete", command=self.delete_selected)

        # List + details
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.Frame(pane)
        self.tree = ttk.Treeview(left, columns=[c for c, _, _ in TREE_COLS],
                                 show="headings", selectmode="extended")
        for col, head, width in TREE_COLS:
            self.tree.heading(col, text=head, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor="w",
                             stretch=col in ("title", "directors", "location"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<F2>", lambda e: self.edit_selected())
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        self.poster_lbl = ttk.Label(right, anchor="center")
        self.poster_lbl.pack(fill="x", pady=(0, 6))
        self.details = tk.Text(right, wrap="word", width=42, relief="flat",
                               background=self.cget("background"), padx=4, pady=4)
        self.details.tag_configure("h", font=("TkDefaultFont", 13, "bold"))
        self.details.tag_configure("k", foreground="#666")
        self.details.tag_configure("link", foreground="#1a5fb4", underline=True)
        self.details.tag_bind("link", "<Button-1>", self._open_link)
        self.details.tag_bind("link", "<Enter>", lambda e: self.details.config(cursor="hand2"))
        self.details.tag_bind("link", "<Leave>", lambda e: self.details.config(cursor=""))
        self.details.pack(fill="both", expand=True)
        self.details.config(state="disabled")
        pane.add(right, weight=2)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(8, 2)).pack(fill="x", side="bottom")

    # ----- list ------------------------------------------------------------ #
    def _format_values(self):
        extra = [f for f in self.db.distinct("format") if f not in DEFAULT_FORMATS]
        return DEFAULT_FORMATS + extra

    def _update_choices(self):
        locs, fmts = self.db.distinct("location"), self._format_values()
        self.loc_box["values"] = locs
        self.fmt_box["values"] = fmts
        self.loc_filter["values"] = [ALL_LOC] + locs
        self.fmt_filter["values"] = [ALL_FMT] + self.db.distinct("format")
        if self.loc_filter.get() not in self.loc_filter["values"]:
            self.loc_filter.set(ALL_LOC)
        if self.fmt_filter.get() not in self.fmt_filter["values"]:
            self.fmt_filter.set(ALL_FMT)

    def _filtered_rows(self):
        rows = self.db.all()
        loc, fmt = self.loc_filter.get(), self.fmt_filter.get()
        terms = fold(self.search_var.get()).split()
        out = []
        for r in rows:
            if loc not in ("", ALL_LOC) and r["location"] != loc:
                continue
            if fmt not in ("", ALL_FMT) and r["format"] != fmt:
                continue
            if terms:
                hay = fold(" ".join(str(r[k] or "") for k in (
                    "title", "original_title", "year", "directors", "cast_list",
                    "genres", "notes", "location", "format")))
                if not all(t in hay for t in terms):
                    continue
            out.append(r)
        return out

    def _sort_key(self, r):
        c = self.sort_col
        if c == "title":
            return title_key(r["title"])
        if c in ("year", "runtime"):
            return (r[c] is None, r[c] or 0)
        return natural_key(fold(r[c] or "~"))

    def _sort_by(self, col):
        self.sort_rev = not self.sort_rev if self.sort_col == col else False
        self.sort_col = col
        self.refresh_list()

    def refresh_list(self, select=None):
        if not hasattr(self, "tree"):
            return
        select = select if select is not None else self._selected_ids()
        self._update_choices()
        rows = sorted(self._filtered_rows(), key=self._sort_key, reverse=self.sort_rev)
        self._visible_rows = rows
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", iid=str(r["id"]), values=(
                r["title"], r["year"] or "", r["directors"] or "", r["format"] or "",
                r["location"] or "", r["runtime"] or ""))
        for col, head, _ in TREE_COLS:
            arrow = (" ▼" if self.sort_rev else " ▲") if col == self.sort_col else ""
            self.tree.heading(col, text=head + arrow)
        keep = [str(i) for i in select if self.tree.exists(str(i))]
        if keep:
            self.tree.selection_set(keep)
            self.tree.see(keep[0])
        total = len(self.db.all())
        if not self.busy:
            shown = f"{len(rows)} of {total} films shown" if len(rows) != total else f"{total} films"
            self.status.set(shown)
        self.show_details()

    def _context_menu(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self.ctx.tk_popup(e.x_root, e.y_root)

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()] if hasattr(self, "tree") else []

    # ----- details panel ---------------------------------------------------- #
    def show_details(self):
        ids = self._selected_ids()
        self.details.config(state="normal")
        self.details.delete("1.0", "end")
        self.poster_lbl.config(image="", text="")
        self._poster_img = None
        if not ids:
            self.details.insert("end", "Select a film to see its details.", "k")
            self.details.config(state="disabled")
            return
        r = self.db.get(ids[0])
        if len(ids) > 1:
            self.details.insert("end", f"{len(ids)} films selected\n\n", "k")

        if r["poster_path"]:
            p = DATA_DIR / r["poster_path"]
            if HAVE_PIL_TK and p.exists():
                try:
                    img = Image.open(p)
                    img.thumbnail((230, 330))
                    self._poster_img = ImageTk.PhotoImage(img)
                    self.poster_lbl.config(image=self._poster_img)
                except Exception:
                    pass
            elif not HAVE_PIL_TK:
                self.poster_lbl.config(text="(install python3-pillow-tk to see posters)")

        head = r["title"] + (f" ({r['year']})" if r["year"] else "")
        self.details.insert("end", head + "\n", "h")
        if r["original_title"] and r["original_title"] != r["title"]:
            self.details.insert("end", r["original_title"] + "\n", "k")
        self.details.insert("end", "\n")
        for key, label in [("format", "Format"), ("location", "Location"), ("notes", "Notes"),
                           ("directors", "Director"), ("writers", "Writers"),
                           ("composers", "Music"), ("cast_list", "Cast"),
                           ("genres", "Genre"), ("countries", "Country"),
                           ("languages", "Language"), ("runtime", "Runtime")]:
            val = r[key]
            if val:
                self.details.insert("end", f"{label}: ", "k")
                self.details.insert("end", f"{val}{' min' if key == 'runtime' else ''}\n")
        if r["extract"]:
            self.details.insert("end", "\n" + r["extract"] + "\n")
        if r["wiki_url"]:
            self.details.insert("end", "\n")
            self.details.insert("end", "Open on Wikipedia", "link")
            self._link = r["wiki_url"]
        else:
            self.details.insert("end", "\nManual entry (no Wikipedia link)", "k")
        self.details.config(state="disabled")

    def _open_link(self, _e=None):
        if getattr(self, "_link", None):
            open_path(self._link)

    # ----- adding / fetching ------------------------------------------------ #
    def add_films(self):
        lines = [l.strip() for l in self.url_text.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            messagebox.showinfo(APP_NAME, "Paste at least one Wikipedia link.")
            return
        fmt, loc = self.fmt_var.get().strip(), self.loc_var.get().strip()
        if not fmt:
            messagebox.showwarning(APP_NAME, "Please choose a format.")
            return
        bad = [l for l in lines if article_key(l) is None]
        if bad:
            messagebox.showwarning(APP_NAME, "These lines aren't Wikipedia article links:\n\n"
                                   + "\n".join(bad[:15]))
            return

        existing = {}
        for r in self.db.all():
            existing.setdefault(article_key(r["wiki_url"] or ""), []).append(r)
        dupes = [l for l in lines if article_key(l) in existing]
        if dupes:
            desc = "\n".join(
                f"• {existing[article_key(l)][0]['title']} - "
                f"{', '.join((x['format'] or '?') + ' @ ' + (x['location'] or '?') for x in existing[article_key(l)])}"
                for l in dupes[:15])
            if not messagebox.askyesno(APP_NAME, "Already in your collection:\n\n" + desc +
                                       "\n\nAdd them again anyway?\n(No = skip these)"):
                lines = [l for l in lines if l not in dupes]
                if not lines:
                    return

        self.db.set_setting("last_format", fmt)
        self.db.set_setting("last_location", loc)
        self.url_text.delete("1.0", "end")
        self._run_worker([((fmt, loc), l) for l in dict.fromkeys(lines)], "added")

    def refetch_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        rows = [self.db.get(fid) for fid in ids]
        linked = [r for r in rows if r["wiki_url"]]
        if not linked:
            messagebox.showinfo(APP_NAME, "The selected film(s) have no Wikipedia link.\n"
                                          "Add one with Edit… if an article exists.")
            return
        skipped = len(rows) - len(linked)
        answer = messagebox.askyesnocancel(
            APP_NAME,
            f"Re-download data for {len(linked)} film(s) from Wikipedia."
            + (f"\n({skipped} without a link will be skipped.)" if skipped else "")
            + "\n\nYes = replace all downloaded fields (your edits to them are lost)"
              "\nNo = only fill in fields that are currently empty"
              "\n\nFormat, location and notes are never changed.")
        if answer is None:
            return
        mode = "replace" if answer else "fill"
        self._run_worker([((r["id"], mode), r["wiki_url"]) for r in linked], "updated")

    def _run_worker(self, jobs, kind):
        def work():
            for i, (ctx, url) in enumerate(jobs, 1):
                self.q.put(("progress", i, len(jobs), url))
                try:
                    self.q.put((kind, ctx, fetch_film(url), url))
                except Exception as e:  # noqa: BLE001 - report everything to the user
                    self.q.put(("error", ctx, friendly_error(e), url))
            self.q.put(("done", kind, len(jobs)))

        self.busy += 1
        self.errors = []
        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, i, n, url = msg
                    self.status.set(f"Downloading {i}/{n}: {urllib.parse.unquote(url)}")
                elif kind == "wait":
                    _, secs, why = msg
                    self.status.set(f"{why} - waiting {secs:.0f} s, then retrying…")
                elif kind == "added":
                    _, (fmt, loc), film, _url = msg
                    fid = self.db.add(film, fmt, loc)
                    self.refresh_list(select=[fid])
                elif kind == "updated":
                    _, (fid, mode), film, _url = msg
                    current = self.db.get(fid)
                    if current is not None:
                        fields = {k: film.get(k) for k in META_FIELDS}
                        if mode == "fill":
                            fields = {k: v for k, v in fields.items()
                                      if v not in (None, "") and current[k] in (None, "")}
                        if fields:
                            self.db.update(fid, fields)
                    self.refresh_list()
                elif kind == "error":
                    _, _ctx, err, url = msg
                    self.errors.append(f"{urllib.parse.unquote(url)}\n   → {err}")
                elif kind == "done":
                    self.busy -= 1
                    _, what, n = msg
                    ok = n - len(self.errors)
                    self.refresh_list()
                    self.status.set(f"{ok} of {n} film(s) {what}."
                                    + (f" {len(self.errors)} failed." if self.errors else ""))
                    if self.errors:
                        if what == "added":
                            self.url_text.insert("end", "\n".join(
                                e.split("\n")[0] for e in self.errors))
                        messagebox.showwarning(
                            APP_NAME, "Some links could not be processed"
                            + (" (they were put back in the box)" if what == "added" else "")
                            + ":\n\n" + "\n".join(self.errors[:12]))
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ----- edit / delete --------------------------------------------------- #
    def edit_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            self.film_editor(self.db.get(ids[0]))
            return
        multi = True
        row = self.db.get(ids[0])
        dlg = tk.Toplevel(self)
        dlg.title(f"Edit {len(ids)} films")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        r = 0
        if multi:
            ttk.Label(frm, foreground="#666",
                      text="Leave a field empty to keep each film's current value.").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(0, 8))
            r += 1
        fields = [("format", "Format"), ("location", "Location")]
        if not multi:
            fields = [("title", "Title")] + fields + [("notes", "Notes")]
        vars_ = {}
        for key, label in fields:
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            v = tk.StringVar(value="" if multi else (row[key] or ""))
            if key == "format":
                w = ttk.Combobox(frm, textvariable=v, values=self._format_values(), width=40)
            elif key == "location":
                w = ttk.Combobox(frm, textvariable=v, values=self.db.distinct("location"), width=40)
            else:
                w = ttk.Entry(frm, textvariable=v, width=43)
            w.grid(row=r, column=1, sticky="ew", pady=3)
            vars_[key] = v
            r += 1

        def save(_e=None):
            changes = {k: v.get().strip() for k, v in vars_.items()}
            if multi:
                changes = {k: v for k, v in changes.items() if v}
            elif not changes["title"]:
                messagebox.showwarning(APP_NAME, "Title can't be empty.", parent=dlg)
                return
            if changes:
                for fid in ids:
                    self.db.update(fid, changes)
            dlg.destroy()
            self.refresh_list(select=ids)

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=save).pack(side="right", padx=6)
        dlg.bind("<Return>", save)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    # ----- full editor / manual entry -------------------------------------- #
    EDIT_FIELDS = [
        ("title", "Title *"), ("original_title", "Original title"), ("year", "Year"),
        ("runtime", "Runtime (min)"), ("directors", "Director(s)"), ("writers", "Writers"),
        ("composers", "Music"), ("cast_list", "Cast"), ("genres", "Genres"),
        ("countries", "Countries"), ("languages", "Languages"), ("format", "Format *"),
        ("location", "Location"), ("notes", "Notes"), ("wiki_url", "Wikipedia link"),
    ]

    def film_editor(self, row):
        """Edit every field of one film, or create a manual entry (row=None)."""
        new = row is None
        dlg = tk.Toplevel(self)
        dlg.title("New manual entry" if new else f"Edit - {row['title']}")
        dlg.transient(self)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        defaults = {"format": self.fmt_var.get(), "location": self.loc_var.get()} if new else {}
        vars_ = {}
        r = 0
        for key, label in self.EDIT_FIELDS:
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 10))
            val = defaults.get(key, "") if new else row[key]
            v = tk.StringVar(value="" if val is None else str(val))
            if key == "format":
                w = ttk.Combobox(frm, textvariable=v, values=self._format_values())
            elif key == "location":
                w = ttk.Combobox(frm, textvariable=v, values=self.db.distinct("location"))
            elif key in ("year", "runtime"):
                w = ttk.Entry(frm, textvariable=v, width=8)
            else:
                w = ttk.Entry(frm, textvariable=v, width=60)
            w.grid(row=r, column=1, sticky="w" if key in ("year", "runtime") else "ew", pady=2)
            if key == "title":
                w.focus_set()
            vars_[key] = v
            r += 1
        ttk.Label(frm, foreground="#666", text="Separate several names with commas.").grid(
            row=r, column=1, sticky="w")
        r += 1

        ttk.Label(frm, text="Summary").grid(row=r, column=0, sticky="nw", pady=(6, 2))
        summary = tk.Text(frm, height=7, width=60, wrap="word", undo=True)
        summary.grid(row=r, column=1, sticky="nsew", pady=(6, 2))
        if not new and row["extract"]:
            summary.insert("1.0", row["extract"])
        frm.rowconfigure(r, weight=1)
        r += 1

        # poster
        poster = {"path": None if new else row["poster_path"], "new_file": None}
        ttk.Label(frm, text="Poster").grid(row=r, column=0, sticky="w", pady=(6, 2))
        pf = ttk.Frame(frm)
        pf.grid(row=r, column=1, sticky="w", pady=(6, 2))
        plabel = ttk.Label(pf, foreground="#666")
        plabel.pack(side="left")

        def show_poster():
            name = poster["new_file"] or poster["path"]
            plabel.config(text=Path(name).name if name else "(none)")

        def choose_poster():
            f = filedialog.askopenfilename(parent=dlg, filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.gif *.webp"), ("All files", "*")])
            if f:
                poster["new_file"] = f
                show_poster()

        def remove_poster():
            poster["path"] = poster["new_file"] = None
            show_poster()

        ttk.Button(pf, text="Choose…", command=choose_poster).pack(side="left", padx=(10, 4))
        ttk.Button(pf, text="Remove", command=remove_poster).pack(side="left")
        show_poster()
        r += 1

        def save(_e=None):
            data = {k: v.get().strip() for k, v in vars_.items()}
            if not data["title"]:
                messagebox.showwarning(APP_NAME, "Title can't be empty.", parent=dlg)
                return
            if not data["format"]:
                messagebox.showwarning(APP_NAME, "Please choose a format.", parent=dlg)
                return
            for key, lo, hi, label in (("year", 1870, 2100, "Year"),
                                       ("runtime", 1, 2000, "Runtime")):
                if data[key]:
                    try:
                        n = int(data[key])
                        assert lo <= n <= hi
                        data[key] = n
                    except (ValueError, AssertionError):
                        messagebox.showwarning(APP_NAME, f"{label} must be a whole number "
                                                         f"between {lo} and {hi}.", parent=dlg)
                        return
            if data["wiki_url"]:
                try:
                    data["lang"] = parse_wiki_url(data["wiki_url"])[0]
                except ValueError:
                    messagebox.showwarning(APP_NAME, "The Wikipedia link doesn't look like a "
                                                     "Wikipedia article address.", parent=dlg)
                    return
            data = {k: (v if v != "" else None) for k, v in data.items()}
            data["extract"] = summary.get("1.0", "end").strip() or None

            old_poster = None if new else row["poster_path"]
            if poster["new_file"]:
                src = Path(poster["new_file"])
                ext = src.suffix.lower() if src.suffix.lower() in (
                    ".jpg", ".jpeg", ".png", ".gif", ".webp") else ".jpg"
                POSTER_DIR.mkdir(parents=True, exist_ok=True)
                dest = POSTER_DIR / f"custom_{datetime.now():%Y%m%d%H%M%S%f}{ext}"
                try:
                    shutil.copyfile(src, dest)
                except OSError as e:
                    messagebox.showerror(APP_NAME, f"Could not copy the image:\n{e}", parent=dlg)
                    return
                data["poster_path"] = dest.relative_to(DATA_DIR).as_posix()
            else:
                data["poster_path"] = poster["path"]

            had_link = bool(row["wiki_url"]) if not new else False
            if new:
                fmt, loc, notes = data.pop("format"), data.pop("location"), data.pop("notes")
                fid = self.db.add(data, fmt, loc, notes or "")
            else:
                fid = row["id"]
                self.db.update(fid, data)
            if old_poster and old_poster != data["poster_path"] \
                    and not self.db.poster_in_use(old_poster):
                try:
                    (DATA_DIR / old_poster).unlink()
                except OSError:
                    pass
            dlg.destroy()
            self.refresh_list(select=[fid])
            if data.get("wiki_url") and not had_link and messagebox.askyesno(
                    APP_NAME, "Fill in the empty fields from the Wikipedia article now?\n"
                              "(Anything you typed is kept.)"):
                self._run_worker([((fid, "fill"), data["wiki_url"])], "updated")

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="Add film" if new else "Save", command=save).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Control-Return>", save)
        dlg.grab_set()

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        names = [self.db.get(i)["title"] for i in ids]
        txt = names[0] if len(ids) == 1 else f"these {len(ids)} films"
        if not messagebox.askyesno(APP_NAME, f"Delete {txt} from the catalogue?"):
            return
        for fid in ids:
            poster = self.db.get(fid)["poster_path"]
            self.db.delete(fid)
            if poster and not self.db.poster_in_use(poster):
                try:
                    (DATA_DIR / poster).unlink()
                except OSError:
                    pass
        self.refresh_list(select=[])

    # ----- export ---------------------------------------------------------- #
    def _rows_for_export(self, only_visible):
        return list(self._visible_rows) if only_visible else self.db.all()

    def export_dialog(self):
        if not self.db.all():
            messagebox.showinfo(APP_NAME, "The catalogue is empty.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Export printable catalogue")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack()
        group = tk.StringVar(value=self.db.get_setting("export_group", "location"))
        ttk.Label(frm, text="Arrange", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        for val, lbl in [("location", "By storage location (find what's on each shelf)"),
                         ("none", "A–Z by title, with location (find where a film is)"),
                         ("format", "By format")]:
            ttk.Radiobutton(frm, text=lbl, value=val, variable=group).pack(anchor="w")
        posters = tk.BooleanVar(value=False)
        details = tk.BooleanVar(value=True)
        visible = tk.BooleanVar(value=False)
        ttk.Label(frm, text="Include", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(frm, text="Poster thumbnails", variable=posters).pack(anchor="w")
        ttk.Checkbutton(frm, text="Genre, cast and notes line", variable=details).pack(anchor="w")
        ttk.Checkbutton(frm, text=f"Only the {len(self._visible_rows)} films currently shown "
                                  "(search/filters applied)", variable=visible).pack(anchor="w")

        def go():
            rows = self._rows_for_export(visible.get())
            if not rows:
                messagebox.showinfo(APP_NAME, "No films to export.", parent=dlg)
                return
            path = filedialog.asksaveasfilename(
                parent=dlg, defaultextension=".html", filetypes=[("Web page", "*.html")],
                initialfile=f"film_catalogue_{datetime.now():%Y-%m-%d}.html")
            if not path:
                return
            self.db.set_setting("export_group", group.get())
            Path(path).write_text(build_catalogue_html(rows, group.get(), posters.get(),
                                                       details.get()), encoding="utf-8")
            dlg.destroy()
            open_path(path)
            self.status.set(f"Exported {len(rows)} films to {path}")

        b = ttk.Frame(frm)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(b, text="Export & open", command=go).pack(side="right", padx=6)
        dlg.grab_set()

    def export_csv(self):
        rows = self.db.all()
        if not rows:
            messagebox.showinfo(APP_NAME, "The catalogue is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"film_catalogue_{datetime.now():%Y-%m-%d}.csv")
        if path:
            write_csv(rows, path)
            self.status.set(f"Exported {len(rows)} films to {path}")

    # ----- covers / stickers ---------------------------------------------- #
    def _films_for_action(self, what):
        ids = self._selected_ids()
        if ids:
            return [dict(self.db.get(i)) for i in ids]
        if not self._visible_rows:
            messagebox.showinfo(APP_NAME, "There are no films to use.")
            return None
        if messagebox.askyesno(APP_NAME, f"No films selected. Make {what} for all "
                                         f"{len(self._visible_rows)} films currently shown?"):
            return [dict(r) for r in sorted(self._visible_rows, key=lambda r: title_key(r["title"]))]
        return None

    def _covers_module(self):
        try:
            import covers
            return covers
        except ImportError as e:
            messagebox.showerror(APP_NAME, "Cover printing needs covers.py next to this file "
                                           f"and the reportlab package.\n\n({e})")
            return None

    def cover_dialog(self):
        cv = self._covers_module()
        films = cv and self._films_for_action("covers")
        if films:
            cv.open_cover_dialog(self, films, self.db, DATA_DIR, self._open_and_report)

    def sticker_dialog(self):
        cv = self._covers_module()
        films = cv and self._films_for_action("QR stickers")
        if films:
            cv.open_sticker_dialog(self, films, self.db, DATA_DIR, self._open_and_report)

    def _open_and_report(self, path):
        self.status.set(f"Saved {path}")
        open_path(path)

    def show_stats(self):
        import stats
        stats.open_stats_window(self, self._visible_rows, len(self.db.all()))

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                APP_NAME, "Films are still downloading. Quit anyway?"):
            return
        self.db.con.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
