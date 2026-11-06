"""Step 3: combine cleaned data + geocodes into one firm-level file.

Output: data/output/ecds_firms_<RELEASE>.csv  (one row per CUST-ID)
        data/output/data_dictionary.md

Locations come from addresscleaner/db.sqlite3. Firms whose address is not in
the db (yet) get the average location of geocoded addresses in their ZIP code.
Re-run this after `python3 -m pipeline.geocode import` to pick up new matches.

Run:  python3 -m pipeline.export
"""
from __future__ import annotations

import pandas as pd

from pipeline import clean, config, geocode

DESCRIPTIONS = {
    "cust_id": "IRS customer id (one per e-file application / office)",
    "legal_name": "Legal name of the firm",
    "dba_name": "Doing-business-as name",
    "supercompany": "Consolidated brand (H&R Block, Jackson Hewitt, manual list in old/consolidated_firms.xlsx), else cleaned DBA name",
    "supercompany_id": "Lowest cust_id in the supercompany group",
    "is_filer": "TRUE if the firm transmitted any returns year-to-date",
    "is_filer_prv1": "TRUE if the firm transmitted returns in the prior year",
    "is_filer_prv2": "TRUE if the firm transmitted returns two years ago",
    "firm_size": "tiny (<100 YTD returns, incl. 0), small (100-999), medium (1,000-9,999), large (10,000+)",
    "geo_lat": "Latitude", "geo_lon": "Longitude",
    "geo_precision": "address = matched street address; zip = ZIP-code centroid fallback; none = no location",
    "geo_address_source": "Which address was geocoded: business, or mailing when business is blank",
    "geo_matched_address": "Address as standardized by the geocoder",
    "geo_state_fips": "State FIPS code (address matches only)",
    "geo_county_fips": "County FIPS code (address matches only)",
    "geo_county_name": "County name (address matches only)",
    "ytd_trnsmt_ret": "Returns transmitted year-to-date", "ytd_act_ret": "Returns accepted YTD",
    "ytd_rej_ret": "Returns rejected YTD", "ytd_bal_due": "Balance-due returns YTD", "ytd_eft": "EFT returns YTD",
    "ytd_reject_rate": "ytd_rej_ret / ytd_trnsmt_ret",
    "reject_rate_change": "ytd_reject_rate - prv1_reject_rate",
    "trnsmit_rate_change": "(ytd - prv1 transmitted) / prv1, capped at +/-1",
    "partners_total": "Distinct people listed in the partner file",
    "partners_licensed": "Partners with LICENSED-IND = Y",
    "partner_names": "Partners, '; '-separated, with professional type (A=Attorney, C=CPA, E=Enrolled Agent; others are raw IRS codes) and licensed flag",
    "contacts_total": "Contacts listed in the contact file",
    "contact_names": "Contacts, '; '-separated, with phone; primary contact first",
    "primary_contact_name": "Contact with PRIMARY-FLAG = Y",
    "primary_contact_phone": "Primary contact phone (digits)",
    "phone": "Firm phone (digits)", "area_code": "First 3 digits of a 10-digit firm phone",
}


def build() -> pd.DataFrame:
    m = pd.read_pickle(config.INTERIM_DIR / "master.pkl")
    p = clean.summarize_partners(pd.read_pickle(config.INTERIM_DIR / "partners.pkl"))
    c = clean.summarize_contacts(pd.read_pickle(config.INTERIM_DIR / "contacts.pkl"))

    a = geocode.firm_addresses(m)
    with geocode.connect() as con:
        g = geocode.match_firms(a, geocode.db_lookup(con))
        zips = geocode.zip_centroids(con)
    g["geo_precision"] = "address"
    g.loc[g["lon"].isna(), "geo_precision"] = pd.NA
    z = g[["zip5"]].merge(zips, on="zip5", how="left")
    use_zip = g["lon"].isna() & z["lon"].notna().values & g["is_us"]
    g.loc[use_zip, ["lon", "lat"]] = z.loc[use_zip.values, ["lon", "lat"]].values
    g.loc[use_zip, "geo_precision"] = "zip"
    g["geo_precision"] = g["geo_precision"].fillna("none")
    geo = g[["cust_id", "lat", "lon", "geo_precision", "addr_source", "matched_address",
             "state_fips", "county_fips", "county_name"]].rename(columns={
        "lat": "geo_lat", "lon": "geo_lon", "addr_source": "geo_address_source",
        "matched_address": "geo_matched_address", "state_fips": "geo_state_fips",
        "county_fips": "geo_county_fips", "county_name": "geo_county_name"})
    geo[["geo_lat", "geo_lon"]] = geo[["geo_lat", "geo_lon"]].astype(float).round(6)

    df = (m.merge(geo, on="cust_id", how="left")
           .merge(c, on="cust_id", how="left")
           .merge(p, on="cust_id", how="left"))
    for col in df.columns:
        if col.startswith("partners_") or col == "contacts_total":
            df[col] = df[col].fillna(0).astype("int64")

    first = ["cust_id", "legal_name", "dba_name", "supercompany", "supercompany_id",
             "is_filer", "is_filer_prv1", "is_filer_prv2", "firm_size",
             "busn_addr1", "busn_addr2", "busn_addr3", "busn_city", "busn_state", "busn_postal",
             "busn_zip5", "busn_country",
             "geo_lat", "geo_lon", "geo_precision", "geo_address_source", "geo_matched_address",
             "geo_state_fips", "geo_county_fips", "geo_county_name", "phone", "area_code"]
    returns = [c for c in df.columns if c.split("_")[0] in ("ytd", "prv1", "prv2")]
    returns += ["reject_rate_change", "trnsmit_rate_change"]
    people = (["primary_contact_name", "primary_contact_phone", "contacts_total", "contact_names",
               "partner_qty", "partners_total", "partners_licensed", "partner_names"]
              + sorted(c for c in df.columns if c.startswith("partners_prof_")))
    rest = [c for c in df.columns if c not in first + returns + people]
    return df[first + returns + people + rest]


def write_dictionary(df: pd.DataFrame):
    lines = [f"# ECDS firm file — data dictionary\n",
             f"Source: IRS e-file (ECDS) FOIA extract dated {config.RELEASE}. "
             f"{len(df):,} rows, one per `cust_id`.\n",
             "| column | type | non-blank | description |", "|---|---|---|---|"]
    for col in df.columns:
        desc = DESCRIPTIONS.get(col, "")
        if not desc and col.endswith("_forms"):
            desc = "Form types the firm is approved for in this role (';'-separated)"
        elif not desc and col.endswith("_effdt"):
            desc = "Effective date for this role"
        elif not desc and col.startswith("is_"):
            desc = "Firm is registered for this e-file role"
        elif not desc and col.startswith("partners_prof_"):
            desc = f"Partners with professional type code {col[-1].upper()}"
        elif not desc and col.startswith(("prv1_", "prv2_")):
            desc = DESCRIPTIONS.get("ytd_" + col.split("_", 1)[1], "").replace("YTD", "").replace(
                "year-to-date", "") + (" (prior year)" if col.startswith("prv1") else " (two years ago)")
        lines.append(f"| `{col}` | {df[col].dtype} | {df[col].notna().mean():.0%} | {desc} |")
    config.DICTIONARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df = build()
    df.to_csv(config.EXPORT_CSV, index=False, encoding="utf-8")
    df.to_pickle(config.INTERIM_DIR / "firms.pkl")
    write_dictionary(df)
    print(f"wrote {config.EXPORT_CSV.relative_to(config.ROOT)}: {len(df):,} rows x {df.shape[1]} columns")
    print("geo_precision:", df["geo_precision"].value_counts().to_dict())
    print("filers:", df["is_filer"].value_counts().to_dict())


if __name__ == "__main__":
    main()
