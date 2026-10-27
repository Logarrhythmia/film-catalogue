# Film Catalogue

Subjectively the best way to organize your film collection!

Paste a Wikipedia link, choose the format and storage location, and the app automagically fills in the blanks, (title, year, director, cast, genres, runtime, summary, poster) from Wikipedia and Wikidata.

Everything is stored locally in SQLite, and all this with a snazzy GUI straight from the turn of the millenium.

## Features

- **Minimal data entry**: one Wikipedia link per film (any language edition); paste a whole shelf at once
- **Manual entries and full editing** for films without a (good) Wikipedia article
- **Re-download** from Wikipedia, either replacing all data or only filling empty fields
- **Search and filter** by location and format; sortable list; right-click menu
- **Printable catalogue** (HTML → print/PDF), grouped by location, A–Z or format; CSV export
- **DVD covers**: if you prefer that library look... black & white, text-only standard DVD case inserts (273 × 183 mm) on A4,
  with a Wikipedia QR code on the back, selectable fonts per element and cut/fold marks
  ([example](docs/sample_covers.pdf))
- **QR stickers** linking to each film's Wikipedia article, for films that already have covers, but you want info right away
- **Statistics**: formats, genres, decades, directors, actors, runtime totals and records
- **Want list** in its own tab and database: priority, preferred format and notes per film,
  printable shopping list, and a "Got it!" button that moves a bought film into the collection

## Files

| File | Purpose |
|---|---|
| `film_catalogue.py` | Main -- this is the file you run |
| `covers.py` | DVD covers and QR stickers |
| `stats.py` | Statistics window |
| `wantlist.py` | Want list tab |
