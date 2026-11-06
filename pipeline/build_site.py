"""Step 4: write the data files for the static map site in docs/.

Firms are written in map order (a Z-order curve), so neighbouring firms sit next to
each other in every file. Coordinates can then be stored as small differences, and
the text columns compress far better (GitHub Pages gzips JSON on the fly).

  docs/data/points.json        what the map needs to draw and filter: delta-encoded
                               coordinates, packed size/filer/approximate flags, state
  docs/data/names.json         name, supercompany, city, returns; loaded after the map
                               appears and used by search and the firm lists
  docs/data/details/<N>.json   popup details for rows N*ROWS_PER_SHARD onwards

All three carry the same build id ("v") so the page never mixes files from two builds.

Run:  python3 -m pipeline.build_site   (after pipeline.export)
"""
from __future__ import annotations

import gzip
import json
import math
import shutil
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline import clean, config

SIZES = ["tiny", "small", "medium", "large"]
ROWS_PER_SHARD = 400   # ~1,000 detail files of ~45 KB gzipped; neighbours share a file
COORD_SCALE = 100_000  # coordinates stored as whole 1e-5 degrees (about 1 m)


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, date):
        return v.isoformat()
    return v


def dump(obj, path):
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def _spread_bits(x: np.ndarray) -> np.ndarray:
    """Put a zero bit between each of the low 16 bits of x."""
    x = x.astype(np.uint64) & 0xFFFF
    x = (x | (x << 8)) & 0x00FF00FF
    x = (x | (x << 4)) & 0x0F0F0F0F
    x = (x | (x << 2)) & 0x33333333
    x = (x | (x << 1)) & 0x55555555
    return x


def map_order(lat, lon) -> np.ndarray:
    """Z-order (Morton) code on a 0.01-degree grid: nearby points get nearby codes."""
    gx = np.clip(((np.asarray(lon, dtype=float) + 180) * 100).astype(np.int64), 0, 0xFFFF)
    gy = np.clip(((np.asarray(lat, dtype=float) + 90) * 100).astype(np.int64), 0, 0xFFFF)
    return _spread_bits(gx) | (_spread_bits(gy) << 1)


def delta(values) -> list[int]:
    """First value, then the difference from the previous one (undone by a running sum)."""
    return np.diff(np.asarray(values, dtype=np.int64), prepend=0).tolist()


def drop_redundant_brand(brands, names) -> list[str]:
    """Supercompany only helps search when the firm name doesn't already contain it."""
    return ["" if not b or b.lower() in n.lower() else b for b, n in zip(brands, names)]


def build(firms: pd.DataFrame, out_dir: Path, rows_per_shard: int = ROWS_PER_SHARD,
          build_id: str | None = None) -> dict:
    """Write points.json, names.json and details/ for `firms` into `out_dir`."""
    build_id = build_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    df = firms[(firms["geo_precision"] != "none") & firms["geo_lat"].notna() & firms["geo_lon"].notna()].copy()
    df["name"] = clean.smart_title(df["dba_name"].fillna(df["legal_name"]).fillna("(no name)"))
    df["city"] = clean.smart_title(df["busn_city"].fillna(df["mail_city"]).fillna(""))
    df["st"] = df["busn_state"].fillna("").where(df["busn_country"].fillna("USA").eq("USA"), "")
    df["ret"] = df["ytd_trnsmt_ret"].fillna(0).astype(int)
    df["lat_i"] = (df["geo_lat"] * COORD_SCALE).round().astype(int)
    df["lon_i"] = (df["geo_lon"] * COORD_SCALE).round().astype(int)
    df["morton"] = map_order(df["geo_lat"], df["geo_lon"])
    # Map order; at one spot (same building or ZIP center) the busiest firm comes first.
    df = df.sort_values(["morton", "ret", "cust_id"], ascending=[True, False, True],
                        kind="stable").reset_index(drop=True)

    states = sorted(s for s in df["st"].unique() if s)
    st_idx = {s: i for i, s in enumerate(states)}
    size = df["firm_size"].map({s: i for i, s in enumerate(SIZES)}).astype(int).to_numpy()
    filer = df["is_filer"].fillna(False).astype(int).to_numpy()
    approx = (df["geo_precision"] == "zip").astype(int).to_numpy()
    names = df["name"].astype(str).tolist()

    points = {
        "v": build_id,
        "release": config.RELEASE,
        "built": date.today().isoformat(),
        "rows_per_shard": rows_per_shard,
        "scale": COORD_SCALE,
        "states": states,
        "sizes": SIZES,
        "lat": delta(df["lat_i"]),
        "lon": delta(df["lon_i"]),
        "a": (size + 4 * filer + 8 * approx).tolist(),  # bits 0-1 size, bit 2 filer, bit 3 ZIP-center location
        "s": [st_idx.get(s, -1) for s in df["st"]],
    }
    name_data = {
        "v": build_id,
        "n": names,
        "sc": drop_redundant_brand(df["supercompany"].fillna("").astype(str).tolist(), names),
        "c": df["city"].astype(str).tolist(),
        "r": df["ret"].tolist(),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "firms.json").unlink(missing_ok=True)  # single data file from older builds
    dump(points, out_dir / "points.json")
    dump(name_data, out_dir / "names.json")

    det_dir = out_dir / "details"
    if det_dir.exists():
        shutil.rmtree(det_dir)
    det_dir.mkdir()
    addr = (df[["busn_addr1", "busn_addr2"]].fillna("").agg(" ".join, axis=1).str.strip() + ", " +
            df["city"] + ", " + df["busn_state"].fillna("") + " " + df["busn_postal"].fillna(""))
    det = pd.DataFrame({
        "id": df["cust_id"].astype(int),
        "legal": df["legal_name"], "dba": df["dba_name"], "sup": df["supercompany"],
        "addr": addr, "geo": df["geo_matched_address"], "prec": df["geo_precision"],
        "county": df["geo_county_name"], "phone": df["phone"],
        "ero": df["ero_effdt"], "roles": df[[c for c in df.columns if c.startswith("is_") and
                                            not c.startswith("is_filer")]].apply(
            lambda r: ", ".join(c[3:].replace("_", " ") for c, v in r.items() if v), axis=1),
        "ytd": df["ytd_trnsmt_ret"], "ytd_acc": df["ytd_act_ret"], "ytd_rej": df["ytd_rej_ret"],
        "prv1": df["prv1_trnsmt_ret"], "prv2": df["prv2_trnsmt_ret"],
        "rr": df["ytd_reject_rate"].round(4), "rr1": df["prv1_reject_rate"].round(4),
        "pt": df["partners_total"], "pl": df["partners_licensed"], "partners": df["partner_names"],
        "contacts": df["contact_names"],
    })
    records = det.to_dict("records")
    shards = 0
    for start in range(0, len(records), rows_per_shard):
        rows = [{k: c for k, v in r.items() if (c := _clean(v)) not in (None, "")}
                for r in records[start:start + rows_per_shard]]
        dump({"v": build_id, "rows": rows}, det_dir / f"{shards}.json")
        shards += 1
    return {"firms": len(df), "shards": shards, "v": build_id}


def _size(path: Path) -> str:
    raw = path.read_bytes()
    return f"{len(raw) / 1e6:.1f} MB ({len(gzip.compress(raw, 6)) / 1e6:.2f} MB gzipped)"


def main():
    df = pd.read_pickle(config.INTERIM_DIR / "firms.pkl")
    info = build(df, config.SITE_DATA_DIR)
    d = config.SITE_DATA_DIR
    det = sorted((d / "details").glob("*.json"), key=lambda p: p.stat().st_size)
    print(f"{info['firms']:,} firms, build {info['v']}")
    print(f"  points.json {_size(d / 'points.json')}")
    print(f"  names.json  {_size(d / 'names.json')}")
    print(f"  details/    {info['shards']} files, largest {_size(det[-1])}")


if __name__ == "__main__":
    main()
