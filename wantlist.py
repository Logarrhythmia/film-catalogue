"""
wantlist.py - the "Want list" tab for Film Catalogue.

Wanted films live in their own database file (wantlist.db, in the data folder),
completely separate from the collection. Shared helpers (Wikipedia download,
sorting, export styling, ...) come from the main module, passed in as `core`.
"""

import csv
import html
import sqlite3
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

PRIORITIES = ["High", "Normal", "Low"]
PRIO_RANK = {p: i for i, p in enumerate(PRIORITIES)}
ANY_FORMAT = "Any"
ALL_PRIO = "All priorities"

WANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS wants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_url TEXT, lang TEXT, wikidata_id TEXT,
    title TEXT NOT NULL, original_title TEXT, year INTEGER,
    directors TEXT, writers TEXT, composers TEXT, cast_list TEXT, genres TEXT,
    countries TEXT, languages TEXT, runtime INTEGER,
    description TEXT, extract TEXT, image_url TEXT, poster_path TEXT,
    pref_format TEXT, priority TEXT, notes TEXT,
    added_at TEXT, updated_at TEXT
);
"""

TREE_COLS = [("priority", "Priority", 70), ("title", "Title", 290), ("year", "Year", 55),
             ("directors", "Director", 160), ("pref_format", "Preferred format", 120),
             ("notes", "Notes", 200)]

EDIT_FIELDS = [
    ("title", "Title *"), ("original_title", "Original title"), ("year", "Year"),
    ("runtime", "Runtime (min)"), ("directors", "Director(s)"), ("writers", "Writers"),
    ("composers", "Music"), ("cast_list", "Cast"), ("genres", "Genres"),
    ("countries", "Countries"), ("languages", "Languages"),
    ("pref_format", "Preferred format"), ("priority", "Priority"), ("notes", "Notes"),
    ("wiki_url", "Wikipedia link"),
]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class WantDatabase:
    def __init__(self, path, core):
        self.core = core
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(WANT_SCHEMA)

    def add(self, film, pref_format, priority, notes=""):
        meta = self.core.META_FIELDS
        cols = meta + ["pref_format", "priority", "notes", "added_at", "updated_at"]
        now = self.core.now_iso()
        vals = [film.get(c) for c in meta] + [pref_format, priority, notes, now, now]
        cur = self.con.execute(
            f"INSERT INTO wants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self.con.commit()
        return cur.lastrowid

    def update(self, wid, fields):
        fields = dict(fields, updated_at=self.core.now_iso())
        sets = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE wants SET {sets} WHERE id=?", [*fields.values(), wid])
        self.con.commit()

    def delete(self, wid):
        self.con.execute("DELETE FROM wants WHERE id=?", (wid,))
        self.con.commit()

    def get(self, wid):
        return self.con.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()

    def all(self):
        return self.con.execute("SELECT * FROM wants").fetchall()

    def poster_in_use(self, rel_path):
        return self.con.execute("SELECT COUNT(*) FROM wants WHERE poster_path=?",
                                (rel_path,)).fetchone()[0] > 0


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
WANT_CSS = """
td.c { width: 1.6em; font-size: 12pt; color: #333; }
td.f { white-space: nowrap; }
td.no { color: #444; font-size: 9pt; }
"""


def build_want_html(rows, core):
    esc = lambda v: html.escape(str(v)) if v not in (None, "") else ""
    groups = {p: [] for p in PRIORITIES}
    for r in rows:
        groups.setdefault(r["priority"] or "Normal", []).append(r)
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Want list</title>",
           f"<style>{core.CATALOGUE_CSS}{WANT_CSS}</style></head><body>",
           "<div class='hint'>Press <b>Ctrl+P</b> (<b>Cmd+P</b> on Mac) to print or save as PDF.</div>",
           f"<header><h1>Want list</h1><p>{len(rows)} films &middot; "
           f"generated {datetime.now():%Y-%m-%d}</p></header>"]
    for prio, items in groups.items():
        if not items:
            continue
        items.sort(key=lambda r: core.title_key(r["title"]))
        out.append(f"<h2>{esc(prio)} priority <small>({len(items)})</small></h2>")
        out.append("<table><thead><tr><th></th><th>Title</th><th>Year</th><th>Director</th>"
                   "<th>Format</th><th>Notes</th></tr></thead><tbody>")
        for r in items:
            t = f"<b>{esc(r['title'])}</b>"
            if r["original_title"] and r["original_title"] != r["title"]:
                t += f" <span class='o'>({esc(r['original_title'])})</span>"
            fmt = r["pref_format"] if r["pref_format"] and r["pref_format"] != ANY_FORMAT else ""
            out.append(f"<tr><td class='c'>&#9744;</td><td class='t'>{t}</td>"
                       f"<td class='y'>{esc(r['year'])}</td><td>{esc(r['directors'])}</td>"
                       f"<td class='f'>{esc(fmt)}</td><td class='no'>{esc(r['notes'])}</td></tr>")
        out.append("</tbody></table>")
    out.append("</body></html>")
    return "\n".join(out)


CSV_COLUMNS = [("priority", "Priority"), ("title", "Title"), ("original_title", "Original title"),
               ("year", "Year"), ("directors", "Director"), ("cast_list", "Cast"),
               ("genres", "Genres"), ("runtime", "Runtime (min)"),
               ("pref_format", "Preferred format"), ("notes", "Notes"),
               ("wiki_url", "Wikipedia"), ("added_at", "Added")]


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #
class WantListTab(ttk.Frame):
    def __init__(self, master, app, core):
        super().__init__(master)
        self.app, self.core = app, core
        self.db = WantDatabase(core.DATA_DIR / "wantlist.db", core)
        self.sort_col, self.sort_rev = "priority", False
        self.visible = []
        self._poster_img = None
        self._link = None
        self._build()
        self.refresh()

    # ----- layout ---------------------------------------------------------- #
    def _format_values(self):
        return [ANY_FORMAT] + self.app._format_values()

    def _build(self):
        core, app = self.core, self.app
        add = ttk.LabelFrame(self, text="Add wanted films", padding=8)
        add.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(add, text="Wikipedia link(s) - one per line:").grid(row=0, column=0, sticky="w")
        self.url_text = tk.Text(add, height=3, width=70, wrap="none", undo=True)
        self.url_text.grid(row=1, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        self.url_text.bind("<Control-Return>", lambda e: (self.add_films(), "break")[1])

        ttk.Label(add, text="Preferred format").grid(row=0, column=1, sticky="w")
        self.fmt_var = tk.StringVar(value=app.db.get_setting("want_last_format", ANY_FORMAT))
        self.fmt_box = ttk.Combobox(add, textvariable=self.fmt_var, width=16,
                                    values=self._format_values())
        self.fmt_box.grid(row=1, column=1, sticky="ew")
        ttk.Label(add, text="Priority").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.prio_var = tk.StringVar(value=app.db.get_setting("want_last_priority", "Normal"))
        ttk.Combobox(add, textvariable=self.prio_var, values=PRIORITIES, state="readonly",
                     width=10).grid(row=1, column=2, sticky="ew", padx=(8, 0))
        ttk.Button(add, text="Add  (Ctrl+Enter)", command=self.add_films).grid(
            row=1, column=3, sticky="ew", padx=(8, 0))
        ttk.Label(add, foreground="#666", text="Bought one? Select it and press “Got it!”."
                  ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(add, text="Manual entry…", command=lambda: self.editor(None)).grid(
            row=2, column=3, sticky="ew", padx=(8, 0), pady=(4, 0))
        add.columnconfigure(0, weight=1)

        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh())
        ttk.Entry(bar, textvariable=self.search_var, width=26).pack(side="left", padx=(4, 10))
        self.prio_filter = ttk.Combobox(bar, state="readonly", width=14,
                                        values=[ALL_PRIO] + PRIORITIES)
        self.prio_filter.set(ALL_PRIO)
        self.prio_filter.pack(side="left")
        self.prio_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        act = ttk.Frame(self, padding=(10, 0, 10, 4))
        act.pack(fill="x")
        ttk.Button(act, text="Edit…", command=self.edit_selected).pack(side="left")
        ttk.Button(act, text="Delete", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(act, text="Re-download", command=self.refetch_selected).pack(side="left")
        ttk.Separator(act, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(act, text="Got it! → Collection…", command=self.got_it).pack(side="left")
        exp = ttk.Menubutton(act, text="Export ▾")
        menu = tk.Menu(exp, tearoff=False)
        menu.add_command(label="Printable want list (HTML/PDF)…", command=self.export_html)
        menu.add_command(label="Spreadsheet (CSV)…", command=self.export_csv)
        exp["menu"] = menu
        exp.pack(side="left", padx=4)

        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="Got it! → Collection…", command=self.got_it)
        self.ctx.add_separator()
        self.ctx.add_command(label="Edit…", command=self.edit_selected)
        self.ctx.add_command(label="Re-download from Wikipedia", command=self.refetch_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Delete", command=self.delete_selected)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=4)
        left = ttk.Frame(pane)
        self.tree = ttk.Treeview(left, columns=[c for c, _, _ in TREE_COLS],
                                 show="headings", selectmode="extended")
        for col, head, width in TREE_COLS:
            self.tree.heading(col, text=head, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor="w",
                             stretch=col in ("title", "directors", "notes"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<F2>", lambda e: self.edit_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<Button-3>", self._context_menu)
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        self.poster_lbl = ttk.Label(right, anchor="center")
        self.poster_lbl.pack(fill="x", pady=(0, 6))
        self.details = tk.Text(right, wrap="word", width=42, relief="flat",
                               background=app.cget("background"), padx=4, pady=4)
        self.details.tag_configure("h", font=("TkDefaultFont", 13, "bold"))
        self.details.tag_configure("k", foreground="#666")
        self.details.tag_configure("link", foreground="#1a5fb4", underline=True)
        self.details.tag_bind("link", "<Button-1>",
                              lambda e: self._link and core.open_path(self._link))
        self.details.tag_bind("link", "<Enter>", lambda e: self.details.config(cursor="hand2"))
        self.details.tag_bind("link", "<Leave>", lambda e: self.details.config(cursor=""))
        self.details.pack(fill="both", expand=True)
        self.details.config(state="disabled")
        pane.add(right, weight=2)

    # ----- list ------------------------------------------------------------ #
    def keys(self):
        """Article keys of every wanted film (for duplicate checks)."""
        return {self.core.article_key(r["wiki_url"]) for r in self.db.all() if r["wiki_url"]}

    def _filtered(self):
        fold = self.core.fold
        terms = fold(self.search_var.get()).split()
        prio = self.prio_filter.get()
        out = []
        for r in self.db.all():
            if prio not in ("", ALL_PRIO) and (r["priority"] or "Normal") != prio:
                continue
            if terms:
                hay = fold(" ".join(str(r[k] or "") for k in (
                    "title", "original_title", "year", "directors", "cast_list", "genres",
                    "notes", "pref_format")))
                if not all(t in hay for t in terms):
                    continue
            out.append(r)
        return out

    def _sort_key(self, r):
        core, c = self.core, self.sort_col
        if c == "priority":
            return (PRIO_RANK.get(r["priority"] or "Normal", 1), core.title_key(r["title"]))
        if c == "title":
            return core.title_key(r["title"])
        if c == "year":
            return (r["year"] is None, r["year"] or 0)
        return core.natural_key(core.fold(r[c] or "~"))

    def _sort_by(self, col):
        self.sort_rev = not self.sort_rev if self.sort_col == col else False
        self.sort_col = col
        self.refresh()

    def refresh(self, select=None):
        select = select if select is not None else self.selected_ids()
        self.fmt_box["values"] = self._format_values()
        rows = sorted(self._filtered(), key=self._sort_key, reverse=self.sort_rev)
        self.visible = rows
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", iid=str(r["id"]), values=(
                r["priority"] or "Normal", r["title"], r["year"] or "", r["directors"] or "",
                r["pref_format"] or ANY_FORMAT, r["notes"] or ""))
        for col, head, _ in TREE_COLS:
            arrow = (" ▼" if self.sort_rev else " ▲") if col == self.sort_col else ""
            self.tree.heading(col, text=head + arrow)
        keep = [str(i) for i in select if self.tree.exists(str(i))]
        if keep:
            self.tree.selection_set(keep)
            self.tree.see(keep[0])
        try:
            self.master.tab(self, text=f"Want list ({len(self.db.all())})")
        except tk.TclError:
            pass  # not added to the notebook yet
        self.show_details()

    def selected_ids(self):
        return [int(i) for i in self.tree.selection()] if hasattr(self, "tree") else []

    def _context_menu(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self.ctx.tk_popup(e.x_root, e.y_root)

    def show_details(self):
        core = self.core
        ids = self.selected_ids()
        d = self.details
        d.config(state="normal")
        d.delete("1.0", "end")
        self.poster_lbl.config(image="", text="")
        self._poster_img = None
        self._link = None
        if not ids:
            d.insert("end", "Select a film to see its details.", "k")
            d.config(state="disabled")
            return
        r = self.db.get(ids[0])
        if len(ids) > 1:
            d.insert("end", f"{len(ids)} films selected\n\n", "k")
        if r["poster_path"] and core.HAVE_PIL_TK:
            p = core.DATA_DIR / r["poster_path"]
            if p.exists():
                try:
                    img = core.Image.open(p)
                    img.thumbnail((230, 330))
                    self._poster_img = core.ImageTk.PhotoImage(img)
                    self.poster_lbl.config(image=self._poster_img)
                except Exception:  # noqa: BLE001
                    pass
        d.insert("end", r["title"] + (f" ({r['year']})" if r["year"] else "") + "\n", "h")
        if r["original_title"] and r["original_title"] != r["title"]:
            d.insert("end", r["original_title"] + "\n", "k")
        d.insert("end", "\n")
        for key, label in [("priority", "Priority"), ("pref_format", "Preferred format"),
                           ("notes", "Notes"), ("directors", "Director"), ("cast_list", "Cast"),
                           ("genres", "Genre"), ("countries", "Country"), ("runtime", "Runtime")]:
            if r[key]:
                d.insert("end", f"{label}: ", "k")
                d.insert("end", f"{r[key]}{' min' if key == 'runtime' else ''}\n")
        if r["extract"]:
            d.insert("end", "\n" + r["extract"] + "\n")
        if r["wiki_url"]:
            d.insert("end", "\n")
            d.insert("end", "Open on Wikipedia", "link")
            self._link = r["wiki_url"]
        d.config(state="disabled")

    # ----- adding / downloading -------------------------------------------- #
    def add_films(self):
        core, app = self.core, self.app
        lines = [l.strip() for l in self.url_text.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            messagebox.showinfo(core.APP_NAME, "Paste at least one Wikipedia link.")
            return
        bad = [l for l in lines if core.article_key(l) is None]
        if bad:
            messagebox.showwarning(core.APP_NAME, "These lines aren't Wikipedia article links:\n\n"
                                   + "\n".join(bad[:15]))
            return
        owned = {core.article_key(r["wiki_url"]): r for r in app.db.all() if r["wiki_url"]}
        wanted = self.keys()
        in_coll = [l for l in lines if core.article_key(l) in owned]
        in_want = [l for l in lines if core.article_key(l) in wanted]
        if in_coll or in_want:
            msg = ""
            if in_coll:
                msg += "Already in your collection:\n" + "\n".join(
                    f"• {owned[core.article_key(l)]['title']}" for l in in_coll[:10]) + "\n\n"
            if in_want:
                msg += "Already on your want list:\n" + "\n".join(
                    f"• {urllib_unquote(l)}" for l in in_want[:10]) + "\n\n"
            if not messagebox.askyesno(core.APP_NAME, msg + "Add them anyway?\n(No = skip these)"):
                lines = [l for l in lines if l not in in_coll and l not in in_want]
                if not lines:
                    return
        pref, prio = self.fmt_var.get().strip() or ANY_FORMAT, self.prio_var.get() or "Normal"
        app.db.set_setting("want_last_format", pref)
        app.db.set_setting("want_last_priority", prio)
        self.url_text.delete("1.0", "end")
        app._run_worker([((pref, prio), l) for l in dict.fromkeys(lines)], "want_added")

    def refetch_selected(self):
        rows = [self.db.get(i) for i in self.selected_ids()]
        linked = [r for r in rows if r["wiki_url"]]
        if not rows:
            return
        if not linked:
            messagebox.showinfo(self.core.APP_NAME, "The selected film(s) have no Wikipedia link.")
            return
        answer = messagebox.askyesnocancel(
            self.core.APP_NAME,
            f"Re-download data for {len(linked)} wanted film(s) from Wikipedia."
            "\n\nYes = replace all downloaded fields (your edits to them are lost)"
            "\nNo = only fill in fields that are currently empty"
            "\n\nPriority, preferred format and notes are never changed.")
        if answer is None:
            return
        mode = "replace" if answer else "fill"
        self.app._run_worker([((r["id"], mode), r["wiki_url"]) for r in linked], "want_updated")

    def handle_result(self, kind, ctx, film):
        """Called by the main window's queue poller for want_* results."""
        if kind == "want_added":
            pref, prio = ctx
            wid = self.db.add(film, pref, prio)
            self.refresh(select=[wid])
        elif kind == "want_updated":
            wid, mode = ctx
            current = self.db.get(wid)
            if current is None:
                return
            fields = {k: film.get(k) for k in self.core.META_FIELDS}
            if mode == "fill":
                fields = {k: v for k, v in fields.items()
                          if v not in (None, "") and current[k] in (None, "")}
            if fields:
                self.db.update(wid, fields)
            self.refresh()

    def on_done(self, what, n, errors):
        self.refresh()
        ok = n - len(errors)
        verb = "added to the want list" if what == "want_added" else "updated"
        self.app.status.set(f"{ok} of {n} film(s) {verb}."
                            + (f" {len(errors)} failed." if errors else ""))
        if errors:
            if what == "want_added":
                self.url_text.insert("end", "\n".join(e.split("\n")[0] for e in errors))
            messagebox.showwarning(
                self.core.APP_NAME, "Some links could not be processed"
                + (" (they were put back in the box)" if what == "want_added" else "")
                + ":\n\n" + "\n".join(errors[:12]))

    # ----- removing -------------------------------------------------------- #
    def _delete_rows(self, ids):
        for wid in ids:
            row = self.db.get(wid)
            if row is None:
                continue
            self.db.delete(wid)
            poster = row["poster_path"]
            if poster and not self.app.poster_in_use(poster):
                try:
                    (self.core.DATA_DIR / poster).unlink()
                except OSError:
                    pass

    def remove_by_key(self, key):
        """Remove wanted films matching an article key (used when the film gets bought)."""
        ids = [r["id"] for r in self.db.all()
               if r["wiki_url"] and self.core.article_key(r["wiki_url"]) == key]
        self._delete_rows(ids)
        self.refresh()
        return len(ids)

    def delete_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        txt = self.db.get(ids[0])["title"] if len(ids) == 1 else f"these {len(ids)} films"
        if messagebox.askyesno(self.core.APP_NAME, f"Remove {txt} from the want list?"):
            self._delete_rows(ids)
            self.refresh(select=[])

    # ----- "Got it!" -------------------------------------------------------- #
    def got_it(self):
        core, app = self.core, self.app
        ids = self.selected_ids()
        if not ids:
            messagebox.showinfo(core.APP_NAME, "Select the film(s) you got first.")
            return
        rows = [self.db.get(i) for i in ids]
        owned = {core.article_key(r["wiki_url"]): r for r in app.db.all() if r["wiki_url"]}
        dupes = [r for r in rows if r["wiki_url"] and core.article_key(r["wiki_url"]) in owned]
        if dupes and not messagebox.askyesno(
                core.APP_NAME, "Already in your collection:\n" +
                "\n".join(f"• {r['title']}" for r in dupes[:10]) +
                "\n\nAdd another copy anyway?"):
            return

        prefs = {r["pref_format"] for r in rows if r["pref_format"] and r["pref_format"] != ANY_FORMAT}
        dlg = tk.Toplevel(self)
        dlg.title("Move to collection")
        dlg.transient(app)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack()
        what = rows[0]["title"] if len(rows) == 1 else f"{len(rows)} films"
        ttk.Label(frm, text=f"Moving {what} to your collection.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frm, text="Format").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        fmt = tk.StringVar(value=prefs.pop() if len(prefs) == 1 else app.fmt_var.get())
        ttk.Combobox(frm, textvariable=fmt, values=app._format_values(), width=30).grid(
            row=1, column=1, pady=3)
        ttk.Label(frm, text="Storage location").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        loc = tk.StringVar(value=app.loc_var.get())
        ttk.Combobox(frm, textvariable=loc, values=app.db.distinct("location"), width=30).grid(
            row=2, column=1, pady=3)

        def move(_e=None):
            f, l = fmt.get().strip(), loc.get().strip()
            if not f:
                messagebox.showwarning(core.APP_NAME, "Please choose a format.", parent=dlg)
                return
            new_ids = []
            for r in rows:
                meta = {k: r[k] for k in core.META_FIELDS}
                new_ids.append(app.db.add(meta, f, l, ""))
                self.db.delete(r["id"])  # poster now belongs to the collection entry
            dlg.destroy()
            app.refresh_list(select=new_ids)
            self.refresh(select=[])
            app.status.set(f"Moved {len(rows)} film(s) to the collection ({f}, {l or 'no location'}).")

        b = ttk.Frame(frm)
        b.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(b, text="Move to collection", command=move).pack(side="right", padx=6)
        dlg.bind("<Return>", move)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    # ----- editing --------------------------------------------------------- #
    def edit_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            self.editor(self.db.get(ids[0]))
            return
        # several films: only priority / preferred format / notes
        dlg = tk.Toplevel(self)
        dlg.title(f"Edit {len(ids)} wanted films")
        dlg.transient(self.app)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack()
        ttk.Label(frm, foreground="#666",
                  text="Leave a field empty to keep each film's current value.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        vars_ = {}
        for r, (key, label, values, state) in enumerate([
                ("priority", "Priority", [""] + PRIORITIES, "readonly"),
                ("pref_format", "Preferred format", self._format_values(), "normal"),
                ("notes", "Notes", None, None)], start=1):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            v = tk.StringVar()
            w = ttk.Combobox(frm, textvariable=v, values=values, state=state, width=38) \
                if values else ttk.Entry(frm, textvariable=v, width=41)
            w.grid(row=r, column=1, pady=3)
            vars_[key] = v

        def save(_e=None):
            changes = {k: v.get().strip() for k, v in vars_.items() if v.get().strip()}
            for wid in ids:
                if changes:
                    self.db.update(wid, changes)
            dlg.destroy()
            self.refresh(select=ids)

        b = ttk.Frame(frm)
        b.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=save).pack(side="right", padx=6)
        dlg.bind("<Return>", save)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    def editor(self, row):
        """Edit all fields of a wanted film, or create a manual entry (row=None)."""
        core, app = self.core, self.app
        new = row is None
        dlg = tk.Toplevel(self)
        dlg.title("New wanted film" if new else f"Edit - {row['title']}")
        dlg.transient(app)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        defaults = {"pref_format": self.fmt_var.get() or ANY_FORMAT,
                    "priority": self.prio_var.get() or "Normal"}
        vars_ = {}
        r = 0
        for key, label in EDIT_FIELDS:
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(0, 10))
            val = defaults.get(key, "") if new else row[key]
            if not new and key == "priority" and not val:
                val = "Normal"
            v = tk.StringVar(value="" if val is None else str(val))
            if key == "pref_format":
                w = ttk.Combobox(frm, textvariable=v, values=self._format_values())
            elif key == "priority":
                w = ttk.Combobox(frm, textvariable=v, values=PRIORITIES, state="readonly", width=12)
            elif key in ("year", "runtime"):
                w = ttk.Entry(frm, textvariable=v, width=8)
            else:
                w = ttk.Entry(frm, textvariable=v, width=60)
            narrow = key in ("year", "runtime", "priority")
            w.grid(row=r, column=1, sticky="w" if narrow else "ew", pady=2)
            if key == "title":
                w.focus_set()
            vars_[key] = v
            r += 1
        ttk.Label(frm, foreground="#666", text="Separate several names with commas.").grid(
            row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Summary").grid(row=r, column=0, sticky="nw", pady=(6, 2))
        summary = tk.Text(frm, height=6, width=60, wrap="word", undo=True)
        summary.grid(row=r, column=1, sticky="nsew", pady=(6, 2))
        if not new and row["extract"]:
            summary.insert("1.0", row["extract"])
        frm.rowconfigure(r, weight=1)
        r += 1

        def save(_e=None):
            data = {k: v.get().strip() for k, v in vars_.items()}
            if not data["title"]:
                messagebox.showwarning(core.APP_NAME, "Title can't be empty.", parent=dlg)
                return
            for key, lo, hi, label in (("year", 1870, 2100, "Year"),
                                       ("runtime", 1, 2000, "Runtime")):
                if data[key]:
                    try:
                        n = int(data[key])
                        assert lo <= n <= hi
                        data[key] = n
                    except (ValueError, AssertionError):
                        messagebox.showwarning(core.APP_NAME, f"{label} must be a whole number "
                                                              f"between {lo} and {hi}.", parent=dlg)
                        return
            if data["wiki_url"]:
                try:
                    data["lang"] = core.parse_wiki_url(data["wiki_url"])[0]
                except ValueError:
                    messagebox.showwarning(core.APP_NAME, "The Wikipedia link doesn't look like "
                                                          "a Wikipedia article address.", parent=dlg)
                    return
            data = {k: (v if v != "" else None) for k, v in data.items()}
            data["pref_format"] = data["pref_format"] or ANY_FORMAT
            data["priority"] = data["priority"] or "Normal"
            data["extract"] = summary.get("1.0", "end").strip() or None
            had_link = bool(row["wiki_url"]) if not new else False
            if new:
                pref, prio, notes = data.pop("pref_format"), data.pop("priority"), data.pop("notes")
                wid = self.db.add(data, pref, prio, notes or "")
            else:
                wid = row["id"]
                self.db.update(wid, data)
            dlg.destroy()
            self.refresh(select=[wid])
            if data.get("wiki_url") and not had_link and messagebox.askyesno(
                    core.APP_NAME, "Fill in the empty fields from the Wikipedia article now?\n"
                                   "(Anything you typed is kept.)"):
                app._run_worker([((wid, "fill"), data["wiki_url"])], "want_updated")

        b = ttk.Frame(frm)
        b.grid(row=r, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(b, text="Add to want list" if new else "Save", command=save).pack(
            side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Control-Return>", save)
        dlg.grab_set()

    # ----- export ---------------------------------------------------------- #
    def export_html(self):
        rows = self.visible or []
        if not rows:
            messagebox.showinfo(self.core.APP_NAME, "No wanted films to export.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".html", filetypes=[("Web page", "*.html")],
            initialfile=f"want_list_{datetime.now():%Y-%m-%d}.html")
        if not path:
            return
        Path(path).write_text(build_want_html(rows, self.core), encoding="utf-8")
        self.core.open_path(path)
        self.app.status.set(f"Exported {len(rows)} wanted films to {path}")

    def export_csv(self):
        rows = self.visible or []
        if not rows:
            messagebox.showinfo(self.core.APP_NAME, "No wanted films to export.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"want_list_{datetime.now():%Y-%m-%d}.csv")
        if not path:
            return
        rows = sorted(rows, key=lambda r: (PRIO_RANK.get(r["priority"] or "Normal", 1),
                                           self.core.title_key(r["title"])))
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([h for _, h in CSV_COLUMNS])
            for r in rows:
                w.writerow(["" if r[c] is None else r[c] for c, _ in CSV_COLUMNS])
        self.app.status.set(f"Exported {len(rows)} wanted films to {path}")


def urllib_unquote(s):
    from urllib.parse import unquote
    return unquote(s)
