# IRS ECDS e-File Provider Browser

Cleans the IRS Electronic Filing (ECDS) FOIA extract, exports one firm-level file,
and publishes a static map of every e-file firm on GitHub Pages (`docs/`).

Replaces the R code in `old/` (`data_load_functions.R`). Gender, PTIN, and ACS
steps from the R code are not ported.

## Layout

| Path | What |
|---|---|
| `raw/` | IRS FOIA files `FOIA-MASTER`, `FOIA-PARTNER`, `FOIA-CONTCT` (not in git) |
| `pipeline/config.py` | Paths, release date, firm-size thresholds |
| `pipeline/clean.py` | Step 1 – load and clean the three files |
| `pipeline/geocode.py` | Step 2 – match addresses to `addresscleaner/db.sqlite3`, write Census batches, import results |
| `pipeline/geocode_submit.py` | Upload batches to the Census geocoder (needs internet; standard library only) |
| `pipeline/export.py` | Step 3 – `data/output/ecds_firms_<release>.csv` + `data_dictionary.md` |
| `pipeline/build_site.py` | Step 4 – data files for the map in `docs/data/` |
| `docs/` | The static website (GitHub Pages) |
| `serve.js` | Local preview server for `docs/` (`node serve.js`) |
| `tests/` | `python3 -m unittest discover -s tests -t .` |

## Run

```bash
pip install -r requirements.txt
./run_pipeline.sh                 # or run the steps one at a time, see the script
```

### Preview the site locally

```bash
node serve.js          # http://127.0.0.1:8080/   (node serve.js 3000 for another port)
```

`serve.js` serves `docs/` with no dependencies (Node 18+). Opening `docs/index.html`
straight from disk won't work, because browsers block `fetch()` of the `data/` files
over `file://`.

### Geocoding

1. `python3 -m pipeline.geocode prepare` looks up each firm's business address
   (mailing address if business is blank) in `addresscleaner/db.sqlite3`, using the
   same `address_key` format as that project. Addresses not found are written to
   `data/geocode/batches/batch_NNNN.csv` (≤ 9,999 rows each; Census limit is 10,000).
   PO boxes and non-US addresses are skipped.
2. `python3 -m pipeline.geocode_submit` uploads the batches one at a time to
   <https://geocoding.geo.census.gov/geocoder/geographies/addressbatch> and saves
   `data/geocode/results/result_NNNN.csv`. Each batch takes ~3–20 minutes; stop and
   re-run any time, finished batches are skipped. Output is also logged to
   `data/geocode/submit.log`. (You can also upload a batch by hand through the
   Census web form and save the download under the same name.)

   If a batch keeps failing with HTTP 400:
   - Make sure only one upload runs at a time (don't use `--workers 2`).
   - `python3 -m pipeline.geocode_submit --only N --split` sends batch N in
     1,000-row pieces and halves any piece the Census rejects until the bad rows
     are found. Good rows are saved as the normal result; rejected rows are listed
     in `data/geocode/rejected_rows.csv`.
   - Batch files contain no quotes or commas inside fields (the Census parser is
     not a full CSV parser). `python3 -m pipeline.geocode rebuild-batches`
     rewrites any batch without results from `batch_keys.csv`, keeping the ids.
3. `python3 -m pipeline.geocode import` copies `db.sqlite3` to
   `db.sqlite3.bak-YYYYMMDD` (once per day), then adds every result to the database:
   matches become `CleanAddress` + `CleanAddressCounty` rows linked from a new
   `RawAddress` (`status='ok'`, score 100 = exact, 90 = non-exact); no-matches are
   stored as `RawAddress` with `status='error'` so they are not re-sent.
4. Re-run `export` and `build_site`. Firms still without a street-level match are
   placed at the average location of geocoded addresses in their ZIP code
   (`geo_precision = zip`).

### Publish on GitHub Pages

```bash
git add -A && git commit -m "Build site for <release>"
git remote add origin https://github.com/<you>/irs-ecds-browser.git   # first time
git push -u origin main
```

Then in the GitHub repository: **Settings → Pages → Build and deployment → Deploy
from a branch → `main` / `/docs`**. The map loads MapLibre from `docs/vendor/` and
basemap tiles from OpenFreeMap (no API key).

`docs/data/` is about 180 MB, within GitHub's limits, but each rebuild adds that much
to the repo history. What a visitor downloads (GitHub Pages gzips JSON on the fly):

| File | Gzipped | When |
|---|---|---|
| `points.json` | 0.7 MB | on page load; enough to draw the map and run the filters |
| `names.json` | 4.1 MB | right after the map appears; used by search and the firm lists |
| `details/<N>.json` | ~45 KB each (988 files) | when a popup opens; nearby firms share a file |

Firms are stored in map order with delta-encoded coordinates, which keeps these small
(see the notes at the top of `pipeline/build_site.py`). All three carry the same build
id, so a visitor whose page loaded before a redeploy is asked to reload rather than
shown another firm's details.

## New FOIA release

Drop the new files into `raw/`, change `RELEASE` in `pipeline/config.py`, and run
`./run_pipeline.sh` plus the geocoding upload. Only addresses not already in the
database are sent to the Census Bureau.

## Notes on the data

- Files are Windows-1252 encoded, not UTF-8.
- The partner file has stray `"` characters in `LICENSED-IND` (`...,",C`), so it is read with quoting off.
- One contact record is split across two lines; it is re-joined.
- Partner professional type codes: A = Attorney, C = CPA, E = Enrolled Agent; B, O, P, R, T, X are kept as codes.
- `firm_size` follows the R code: tiny < 100 returns (including 0), small 100+, medium 1,000+, large 10,000+. `is_filer` marks firms with any returns transmitted this year.
