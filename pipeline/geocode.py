"""Step 2: geocode firm addresses.

  python3 -m pipeline.geocode prepare   # match against addresscleaner/db.sqlite3,
                                        # write Census batch files for the rest
  python3 -m pipeline.geocode_submit    # (on a machine with internet) upload batches
  python3 -m pipeline.geocode import    # load Census results back into db.sqlite3
  python3 -m pipeline.geocode rebuild-batches  # rewrite batch files that have no results yet

All three steps are safe to re-run: prepare only adds addresses that are not yet
in the database or in an earlier batch, submit skips batches that already have
results, and import skips addresses already in the database.

Address keys use the same format as the addresscleaner RawAddress.address_key:
    "<street>[ <street2>], <city>, <state> <zip>"   (lower case)
"""
from __future__ import annotations

import csv
import shutil
import sqlite3
import sys
from datetime import date, datetime

import pandas as pd

from pipeline import config
from pipeline.clean import is_po_box

# Census returns these values in the "match" column.
CENSUS_MATCH = "Match"

US_STATES_AND_TERRITORIES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split()
)


# ---------------------------------------------------------------------------
# Building addresses and keys
# ---------------------------------------------------------------------------

def firm_addresses(m: pd.DataFrame) -> pd.DataFrame:
    """One address per firm: business address, or mailing address if business is blank."""
    use_mail = m["busn_addr1"].isna() & m["mail_addr1"].notna()
    pick = lambda f: m[f"busn_{f}"].where(~use_mail, m[f"mail_{f}"])  # noqa: E731
    a = pd.DataFrame({
        "cust_id": m["cust_id"],
        "addr_source": pd.Series("business", index=m.index).where(~use_mail, "mailing"),
        "street": pick("addr1"), "street2": pick("addr2"),
        "city": pick("city"), "state": pick("state"),
        "postal": pick("postal"), "country": pick("country"),
    })
    a["zip5"] = a["postal"].str.extract(r"^(\d{5})", expand=False)
    a["address_key"] = make_key(a["street"], a["street2"], a["city"], a["state"], a["postal"])
    a["address_key_alt"] = make_key(a["street"], a["street2"], a["city"], a["state"],
                                    a["postal"].str.lstrip("0"))
    a["is_us"] = a["state"].isin(US_STATES_AND_TERRITORIES) & a["country"].fillna("USA").eq("USA")
    a["is_po_box"] = is_po_box(a["street"]) | is_po_box(a["street2"]) & a["street"].str.match(r"^\D")
    return a


def make_key(street, street2, city, state, postal) -> pd.Series:
    st = street.fillna("") + (" " + street2).where(street2.notna(), "")
    key = st + ", " + city.fillna("") + ", " + state.fillna("") + " " + postal.fillna("")
    return key.str.strip().str.lower()


def census_street(street: pd.Series, street2: pd.Series) -> pd.Series:
    """Street line to send to Census: prefer the line that starts with a house number."""
    s1_num = street.str.match(r"^\d").fillna(False)
    s2_num = street2.str.match(r"^\d").fillna(False)
    return street.where(s1_num | ~s2_num, street2)


def census_field(s: pd.Series) -> pd.Series:
    """Text safe for the Census batch CSV: plain ASCII letters, digits and a few
    address characters (space # & ' / . - ( )); everything else becomes a space.

    The Census batch parser is not a full CSV parser: a backslash escapes the next
    comma (`STE 115\\,MARIETTA` -> "Malformed input file", HTTP 400 for the whole
    batch), and quoted fields are risky too. Stray symbols (| ~ ` ! = * $ > : ;)
    carry no address information, so they are dropped.
    """
    s = s.astype("string").fillna("")
    s = s.str.normalize("NFKD").str.encode("ascii", "ignore").str.decode("ascii")
    s = s.str.replace(r"[^A-Za-z0-9 #&'/.()\-]", " ", regex=True)
    return s.str.replace(r"\s+", " ", regex=True).str.strip()


def write_batch(chunk: pd.DataFrame, path) -> None:
    """Census format: Unique ID, Street address, City, State, ZIP  (no header, no quoting)."""
    out = pd.DataFrame({
        "id": chunk["id"].astype(str),
        "street": census_field(census_street(chunk["street"], chunk["street2"])),
        "city": census_field(chunk["city"]),
        "state": census_field(chunk["state"]),
        "zip": chunk["postal"].astype("string").str.extract(r"^(\d{5})", expand=False).fillna(""),
    })
    out.to_csv(path, header=False, index=False, quoting=csv.QUOTE_NONE, lineterminator="\n")


def rebuild_batches():
    """Rewrite batch files that have no results yet from batch_keys.csv (ids unchanged)."""
    keys = pd.read_csv(config.BATCH_KEYS, dtype=str, keep_default_na=False)
    for b, chunk in keys.groupby(keys["batch"].astype(int)):
        if (config.RESULT_DIR / f"result_{b:04d}.csv").exists():
            print(f"  batch {b:04d}: has results, left as is")
            continue
        path = config.BATCH_DIR / f"batch_{b:04d}.csv"
        write_batch(chunk, path)
        print(f"  rewrote {path.relative_to(config.ROOT)} ({len(chunk):,} rows)")


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def connect(readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{config.GEOCODE_DB}?mode=ro", uri=True)
    con = sqlite3.connect(config.GEOCODE_DB)
    # Keep the rollback journal in memory (the db is backed up before writing).
    con.execute("PRAGMA journal_mode=MEMORY")
    return con


def db_lookup(con) -> pd.DataFrame:
    """All address keys in the db with their status and coordinates (if any)."""
    q = """
        SELECT r.address_key, r.status AS raw_status, c.id AS clean_address_id,
               c.x AS lon, c.y AS lat, c.longlabel AS matched_address, c.score,
               cc.state_fips, cc.county_fips, cc.county_name
        FROM geocode_rawaddress r
        LEFT JOIN geocode_cleanaddress c ON c.id = r.clean_address_id
        LEFT JOIN (SELECT clean_address_id, MIN(state_fips) state_fips, MIN(county_fips) county_fips,
                          MIN(county_name) county_name
                   FROM geocode_cleanaddresscounty GROUP BY clean_address_id) cc
               ON cc.clean_address_id = c.id
    """
    return pd.read_sql(q, con)


def zip_centroids(con) -> pd.DataFrame:
    """Average location of every geocoded address in each ZIP code (fallback location)."""
    q = """SELECT substr(zip, 1, 5) AS zip5, AVG(x) AS lon, AVG(y) AS lat, COUNT(*) AS n
           FROM geocode_cleanaddress
           WHERE x IS NOT NULL AND y IS NOT NULL AND (country = 'USA' OR country IS NULL)
             AND length(zip) >= 5
           GROUP BY 1"""
    return pd.read_sql(q, con)


def match_firms(a: pd.DataFrame, lookup: pd.DataFrame) -> pd.DataFrame:
    """Attach db coordinates to firm addresses using the key, then the zero-stripped key."""
    ok = lookup[lookup["lon"].notna() & lookup["lat"].notna()].drop(columns="raw_status")
    out = a.merge(ok, on="address_key", how="left")
    miss = out["lon"].isna()
    alt = a.loc[miss.values, ["address_key_alt"]].merge(
        ok.rename(columns={"address_key": "address_key_alt"}), on="address_key_alt", how="left")
    for c in ok.columns.drop("address_key"):
        out.loc[miss, c] = alt[c].values
    return out


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def prepare():
    m = pd.read_pickle(config.INTERIM_DIR / "master.pkl")
    a = firm_addresses(m)
    with connect() as con:
        lookup = db_lookup(con)
    matched = match_firms(a, lookup)
    have = matched["lon"].notna()
    print(f"firms: {len(a):,}; already geocoded in db: {have.sum():,} ({have.mean():.1%})")

    todo = matched[~have & matched["is_us"] & ~matched["is_po_box"] & matched["street"].notna()]
    print(f"  not US: {(~a['is_us']).sum():,}; PO box: {a['is_po_box'].sum():,}; "
          f"no street: {a['street'].isna().sum():,}")
    todo = todo.drop_duplicates("address_key")

    config.BATCH_DIR.mkdir(parents=True, exist_ok=True)
    if config.BATCH_KEYS.exists():
        done = pd.read_csv(config.BATCH_KEYS, dtype=str, keep_default_na=False)
    else:
        done = pd.DataFrame(columns=["id", "batch", "address_key", "street", "street2",
                                     "city", "state", "postal"])
    # Keys already in the db *with* coordinates were matched above; keys in the db
    # without coordinates (old ESRI errors / unprocessed) are worth a Census try.
    known = set(done["address_key"]) | set(lookup.loc[lookup["lon"].notna(), "address_key"])
    todo = todo[~todo["address_key"].isin(known)]
    print(f"new unique addresses to send to Census: {len(todo):,}")
    if todo.empty:
        return

    next_id = int(done["id"].astype(int).max()) + 1 if len(done) else 1
    next_batch = int(done["batch"].astype(int).max()) + 1 if len(done) else 1
    todo = todo.reset_index(drop=True)
    todo["id"] = range(next_id, next_id + len(todo))
    todo["batch"] = next_batch + todo.index // config.CENSUS_BATCH_SIZE

    for b, chunk in todo.groupby("batch"):
        path = config.BATCH_DIR / f"batch_{b:04d}.csv"
        write_batch(chunk, path)
        print(f"  wrote {path.relative_to(config.ROOT)} ({len(chunk):,} rows)")

    cols = ["id", "batch", "address_key", "street", "street2", "city", "state", "postal"]
    pd.concat([done, todo[cols].astype("string").fillna("")], ignore_index=True).to_csv(config.BATCH_KEYS, index=False)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

RESULT_COLS = ["id", "input_address", "match", "match_type", "matched_address", "coordinates",
               "tiger_line_id", "side", "state_fips", "county_fips", "tract", "block"]


def read_results() -> pd.DataFrame:
    frames = []
    for path in sorted(config.RESULT_DIR.glob("result_*.csv")):
        rows = [r + [""] * (12 - len(r)) for r in csv.reader(path.open(encoding="utf-8", errors="replace"))]
        frames.append(pd.DataFrame([r[:12] for r in rows if r], columns=RESULT_COLS))
    if not frames:
        return pd.DataFrame(columns=RESULT_COLS)
    r = pd.concat(frames, ignore_index=True).drop_duplicates("id", keep="last")
    xy = r["coordinates"].str.split(",", expand=True)
    r["lon"] = pd.to_numeric(xy[0], errors="coerce") if 0 in xy else None
    r["lat"] = pd.to_numeric(xy[1], errors="coerce") if 1 in xy else None
    return r


def county_names() -> dict:
    """(state_fips, county_fips) -> (county name, state name) from the Census FIPS list."""
    g = pd.read_csv(config.COUNTY_FIPS_CSV, skiprows=4, dtype=str, encoding="utf-8-sig")
    g.columns = ["level", "state", "county", "subdiv", "place", "city", "name"]
    states = g[g["level"] == "040"].set_index("state")["name"].to_dict()
    counties = g[g["level"] == "050"]
    return {(r.state, r.county): (r.name, states.get(r.state, "")) for r in counties.itertuples()}


def backup_db():
    dst = config.GEOCODE_DB.with_name(f"db.sqlite3.bak-{date.today():%Y%m%d}")
    if not dst.exists():
        print(f"backing up database to {dst.name} ...")
        shutil.copy2(config.GEOCODE_DB, dst)


def split_matched(addr: str):
    """'4600 SILVER HILL RD, WASHINGTON, DC, 20233' -> (street, city, state, zip)."""
    parts = [p.strip() for p in (addr or "").split(",")]
    if len(parts) >= 4:
        return ", ".join(parts[:-3]), parts[-3], parts[-2], parts[-1]
    return addr, None, None, None


def import_results(db_path=None):
    if db_path is not None:
        config.GEOCODE_DB = db_path
    r = read_results()
    if r.empty:
        print("no result files in", config.RESULT_DIR)
        return
    keys = pd.read_csv(config.BATCH_KEYS, dtype=str, keep_default_na=False)
    r = r.merge(keys, on="id", how="inner")
    print(f"results: {len(r):,} rows; matched {(r['match'] == CENSUS_MATCH).sum():,}")

    if db_path is None:
        backup_db()
    fips = county_names()
    con = connect(readonly=False)
    cur = con.cursor()
    existing = dict(cur.execute("SELECT address_key, clean_address_id FROM geocode_rawaddress"))
    clean_ids = {}  # longlabel -> clean_address id (dedupe census addresses within this import)
    now = datetime.now().isoformat(sep=" ")
    added_ok = added_err = skipped = 0

    for row in r.itertuples():
        if row.address_key in existing and existing[row.address_key] is not None:
            skipped += 1
            continue
        update_existing = row.address_key in existing
        clean_id = None
        status = "error"
        if row.match == CENSUS_MATCH and pd.notna(row.lon) and pd.notna(row.lat):
            status = "ok"
            label = row.matched_address
            clean_id = clean_ids.get(label)
            if clean_id is None:
                street, city, st, zp = split_matched(label)
                county, state_name = fips.get((row.state_fips, row.county_fips), ("", ""))
                cur.execute(
                    """INSERT INTO geocode_cleanaddress
                       (city, state, zip, zip_plus_four, score, x, y, created_at, updated_at,
                        shortlabel, staddr, longlabel, status, country, metroarea, region,
                        region_abbr, subregion)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (city, st, zp, None, 100.0 if row.match_type == "Exact" else 90.0,
                     row.lon, row.lat, now, now, street, street, label, "ok", "USA", "",
                     state_name, st, county))
                clean_id = cur.lastrowid
                clean_ids[label] = clean_id
                if row.state_fips and row.county_fips:
                    cur.execute(
                        """INSERT INTO geocode_cleanaddresscounty
                           (county_name, county_fips, state_name, state_fips, created_at,
                            updated_at, clean_address_id) VALUES (?,?,?,?,?,?,?)""",
                        (county, row.county_fips, state_name, row.state_fips, now, now, clean_id))
        zip_full = row.postal or ""
        if update_existing:
            if status == "ok":
                cur.execute("UPDATE geocode_rawaddress SET status=?, clean_address_id=?, updated_at=? "
                            "WHERE address_key=?", (status, clean_id, now, row.address_key))
                existing[row.address_key] = clean_id
                added_ok += 1
            else:
                skipped += 1
            continue
        cur.execute(
            """INSERT INTO geocode_rawaddress
               (address_key, raw_street, raw_street2, raw_city, raw_state, raw_zip,
                raw_zip_plus_four, status, created_at, updated_at, clean_address_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (row.address_key, row.street or None, row.street2 or None, row.city or None,
             row.state or None, zip_full[:10] or None, None, status, now, now, clean_id))
        existing[row.address_key] = clean_id
        if status == "ok":
            added_ok += 1
        else:
            added_err += 1
    con.commit()
    con.close()
    print(f"added {added_ok:,} matched and {added_err:,} unmatched addresses; "
          f"skipped {skipped:,} already in db")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        prepare()
    elif cmd == "rebuild-batches":
        rebuild_batches()
    elif cmd == "import":
        import_results()
    else:
        print(__doc__)
