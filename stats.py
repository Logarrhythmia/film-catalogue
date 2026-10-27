"""
stats.py - a "just for fun" statistics window for Film Catalogue.
Standard library only.
"""

import statistics
from collections import Counter

import tkinter as tk
from tkinter import ttk, font as tkfont

BAR_COLOR = "#4a7bd0"
TOP_N = 10


def split_list(s):
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def fmt_duration(minutes):
    if not minutes:
        return "–"
    days, rest = divmod(int(minutes), 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} h")
    if mins and not days:
        parts.append(f"{mins} min")
    return " ".join(parts)


def compute(rows):
    rows = [dict(r) for r in rows]
    years = [r["year"] for r in rows if r.get("year")]
    runtimes = [r["runtime"] for r in rows if r.get("runtime")]
    titles = {(r.get("wiki_url") or r["title"]) for r in rows}

    def count(field, multi=False, limit=None):
        c = Counter()
        for r in rows:
            vals = split_list(r.get(field)) if multi else [r.get(field) or "(not set)"]
            if limit:
                vals = vals[:limit]
            c.update(vals)
        return c

    decades = Counter(f"{y // 10 * 10}s" for y in years)
    with_year = [r for r in rows if r.get("year")]
    with_rt = [r for r in rows if r.get("runtime")]

    def label(r, extra):
        return f"{r['title']} ({extra})"

    records = []
    if with_year:
        o = min(with_year, key=lambda r: r["year"])
        n = max(with_year, key=lambda r: r["year"])
        records += [("Oldest", label(o, o["year"])), ("Newest", label(n, n["year"]))]
    if with_rt:
        lo = max(with_rt, key=lambda r: r["runtime"])
        sh = min(with_rt, key=lambda r: r["runtime"])
        records += [("Longest", label(lo, f"{lo['runtime']} min")),
                    ("Shortest", label(sh, f"{sh['runtime']} min"))]
    if rows:
        latest = max(rows, key=lambda r: r.get("added_at") or "")
        records.append(("Latest addition", latest["title"]))

    return {
        "cards": [
            (str(len(rows)), "films"),
            (str(len(titles)), "different titles"),
            (fmt_duration(sum(runtimes)), "of non-stop watching"),
            (f"{statistics.mean(runtimes):.0f} min" if runtimes else "–", "average runtime"),
            (str(round(statistics.median(years))) if years else "–", "median year"),
            (str(len(count("directors", True))), "different directors"),
        ],
        "records": records,
        "charts": [
            ("Formats", count("format"), False),
            ("Storage locations", count("location"), False),
            ("Decades", decades, True),
            ("Genres", count("genres", True), False),
            ("Directors", count("directors", True), False),
            ("Actors (top-billed 8 per film)", count("cast_list", True), False),
            ("Countries", count("countries", True), False),
            ("Original languages", count("languages", True), False),
        ],
        "total": len(rows),
    }


class BarChart(tk.Canvas):
    """Horizontal bars: label | bar | count (percent)."""

    def __init__(self, master, counter, total, chronological=False, **kw):
        items = sorted(counter.items()) if chronological else \
            sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
        self.more = 0
        if not chronological and len(items) > TOP_N:
            self.more = len(items) - TOP_N
            items = items[:TOP_N]
        self.items, self.total = items, total
        self.row_h = 22
        super().__init__(master, height=max(1, len(items)) * self.row_h + 4,
                         highlightthickness=0, background=master.winfo_toplevel().cget("background"),
                         **kw)
        self.f = tkfont.nametofont("TkDefaultFont")
        self.bind("<Configure>", lambda e: self.draw())

    def draw(self):
        self.delete("all")
        if not self.items:
            self.create_text(4, 10, text="No data", anchor="w", fill="#888")
            return
        width = self.winfo_width()
        label_w = min(max(self.f.measure(str(k)) for k, _ in self.items) + 12, int(width * 0.4))
        num_w = 90
        bar_max = max(20, width - label_w - num_w)
        top = max(v for _, v in self.items)
        for i, (k, v) in enumerate(self.items):
            y = i * self.row_h + 2
            name = str(k)
            if self.f.measure(name) > label_w - 12:
                while name and self.f.measure(name + "…") > label_w - 12:
                    name = name[:-1]
                name = name.rstrip() + "…"
            self.create_text(label_w - 8, y + self.row_h / 2, text=name, anchor="e")
            w = max(2, bar_max * v / top)
            self.create_rectangle(label_w, y + 4, label_w + w, y + self.row_h - 4,
                                  fill=BAR_COLOR, outline="")
            pct = f"  ({100 * v / self.total:.0f}%)" if self.total else ""
            self.create_text(label_w + w + 6, y + self.row_h / 2, text=f"{v}{pct}",
                             anchor="w", fill="#444")


def open_stats_window(parent, rows, total_in_db):
    data = compute(rows)
    win = tk.Toplevel(parent)
    scope = "" if data["total"] == total_in_db else f"  ({data['total']} of {total_in_db} films - filters applied)"
    win.title("Collection statistics" + scope)
    win.geometry("760x780")

    outer = ttk.Frame(win)
    outer.pack(fill="both", expand=True)
    cv = tk.Canvas(outer, highlightthickness=0, background=win.cget("background"))
    sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
    cv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    cv.pack(side="left", fill="both", expand=True)
    body = ttk.Frame(cv, padding=16)
    body_id = cv.create_window(0, 0, window=body, anchor="nw")
    body.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.bind("<Configure>", lambda e: cv.itemconfigure(body_id, width=e.width))

    def wheel(e):
        step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        cv.yview_scroll(step * 3, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        win.bind(seq, wheel)  # toplevel binding catches wheel events over all its children

    if not data["total"]:
        ttk.Label(body, text="No films yet - add some first!").pack()
        return

    base = tkfont.nametofont("TkDefaultFont")
    big = base.copy()
    big.configure(size=max(base.cget("size"), 10) + 10, weight="bold")
    head = base.copy()
    head.configure(size=max(base.cget("size"), 10) + 2, weight="bold")
    win._fonts = (big, head)  # keep references

    cards = ttk.Frame(body)
    cards.pack(fill="x")
    for i, (value, caption) in enumerate(data["cards"]):
        card = ttk.Frame(cards, padding=10, relief="groove")
        card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=4, pady=4)
        ttk.Label(card, text=value, font=big).pack(anchor="w")
        ttk.Label(card, text=caption, foreground="#666").pack(anchor="w")
    for c in range(3):
        cards.columnconfigure(c, weight=1)

    if data["records"]:
        rec = ttk.Frame(body, padding=(4, 12, 4, 0))
        rec.pack(fill="x")
        ttk.Label(rec, text="Records", font=head).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for i, (k, v) in enumerate(data["records"], 1):
            ttk.Label(rec, text=k, foreground="#666").grid(row=i, column=0, sticky="w", padx=(0, 12))
            ttk.Label(rec, text=v).grid(row=i, column=1, sticky="w")

    for title, counter, chrono in data["charts"]:
        if not counter:
            continue
        sec = ttk.Frame(body, padding=(4, 16, 4, 0))
        sec.pack(fill="x")
        ttk.Label(sec, text=title, font=head).pack(anchor="w", pady=(0, 4))
        chart = BarChart(sec, counter, data["total"], chrono)
        chart.pack(fill="x")
        if chart.more:
            ttk.Label(sec, text=f"+ {chart.more} more", foreground="#888").pack(anchor="w")
