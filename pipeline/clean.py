"""Step 1: load and clean the raw IRS ECDS FOIA files.

Ports the cleaning logic from old/data_load_functions.R to pandas.

Outputs (pickles in data/interim/):
  master.pkl    one row per firm (CUST-ID), cleaned
  partners.pkl  one row per person per firm (titles / professions collapsed)
  contacts.pkl  one row per contact per firm

Run:  python3 -m pipeline.clean
"""
from __future__ import annotations

import csv
import io
import re

import numpy as np
import pandas as pd

from pipeline import config

# Professional type codes in the partner file. A/C/E are certain; the rest
# are kept as raw codes because the IRS does not publish a key.
PROF_TYPE_LABELS = {"A": "Attorney", "C": "CPA", "E": "Enrolled Agent"}

# Role prefixes in the master file -> output names.
ROLES = {
    "ERO": "ero",          # Electronic Return Originator
    "TRMT": "transmitter",
    "SWDV": "software_developer",
    "ISP": "isp",          # Intermediate Service Provider
    "RA": "reporting_agent",
    "OLF": "online_filer",
    "LGTX": "large_taxpayer",
    "ACA": "aca",          # Affordable Care Act information returns
}

RETURN_FIELDS = ["TRNSMT-RET", "ACT-RET", "REJ-RET", "BAL-DUE", "EFT"]
PERIODS = {"YTD": "ytd", "PRV1": "prv1", "PRV2": "prv2"}

COMPANY_SUFFIX_RE = re.compile(r"[\s,]+(inc|llc|llp|pllc|ltd|pa|pc|corp|co)$")

# Name variants the old R code mapped to the two big franchise brands.
HRB_DBAS = {
    "hrb eastern enterprises", "hrb international llc", "hrb tax group inc",
    "hr block", "h and r block", "dba h&r block",
}
JH_DBAS = {
    "jackson hewitt", "thomas m bonelli dba jackson hewitt tax",
    "beaches inc dba jackson hewitt tax servi", "dba/ jackson hewitt",
}


# ---------------------------------------------------------------------------
# Small helpers (unit tested in tests/test_clean.py)
# ---------------------------------------------------------------------------

def squish(s: pd.Series) -> pd.Series:
    """Trim, collapse internal whitespace, and turn empty strings into NA."""
    s = s.astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
    return s.mask(s == "")


def digits_only(s: pd.Series) -> pd.Series:
    out = s.astype("string").str.replace(r"\D", "", regex=True)
    return out.mask(out == "")


def smart_title(s: pd.Series) -> pd.Series:
    """Title-case names that are ALL CAPS or all lower; leave mixed case alone."""
    s = s.astype("string")
    needs = s.notna() & ((s == s.str.upper()) | (s == s.str.lower()))
    out = s.copy()
    out[needs] = s[needs].str.title()
    return out


def parse_yyyymmdd(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce").dt.date


def to_int(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def safe_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    num = num.astype("float64")
    den = den.astype("float64")
    return (num / den.where(den != 0)).astype("float64")


def firm_size(ytd: pd.Series) -> pd.Series:
    """Same buckets as the R code; firms with 0 returns fall into 'tiny'."""
    y = ytd.fillna(0).astype("int64")
    return pd.Series(
        np.select(
            [y >= config.LARGE_FIRM_SIZE, y >= config.MEDIUM_FIRM_SIZE, y >= config.SMALL_FIRM_SIZE],
            ["large", "medium", "small"],
            default="tiny",
        ),
        index=ytd.index,
    )


def capped_change(cur: pd.Series, prev: pd.Series, cap: float = config.ABS_CHANGE_THRESHOLD) -> pd.Series:
    """(cur - prev) / prev, capped to +/- cap. prev == 0 & cur > 0 -> +cap (R gave Inf, then capped)."""
    cur = cur.astype("float64")
    prev = prev.astype("float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        chg = (cur - prev) / prev
    chg = chg.replace([np.inf, -np.inf], np.nan).where(~((prev == 0) & (cur > 0)), cap)
    return chg.clip(-cap, cap)


def normalize_company(name: pd.Series) -> pd.Series:
    """Lowercase, drop periods/commas at the end, strip trailing entity suffixes."""
    s = name.astype("string").str.lower().str.replace(".", "", regex=False).str.strip()
    for _ in range(2):  # handle "foo, inc llc"
        s = s.str.replace(COMPANY_SUFFIX_RE, "", regex=True).str.strip(" ,")
    return s


def brand_from_dba(dba_lower: pd.Series) -> pd.Series:
    """Map H&R Block and Jackson Hewitt variants to a single brand (R logic)."""
    s = dba_lower.str.replace("h & r block", "h&r block", regex=False)
    hrb_names = set(normalize_company(pd.Series(sorted(HRB_DBAS))))
    jh_names = set(normalize_company(pd.Series(sorted(JH_DBAS))))
    hrb = (s.str.startswith("h&rblock") | s.str.startswith("h&r block")
           | s.isin(hrb_names))
    jh = s.str.startswith("jackson hewitt") | s.isin(jh_names)
    out = pd.Series(pd.NA, index=s.index, dtype="string")
    out[hrb.fillna(False)] = "h&r block"
    out[jh.fillna(False)] = "jackson hewitt"
    return out


def is_po_box(street: pd.Series) -> pd.Series:
    s = street.astype("string").str.lower().str.replace(r"[\s.,]", "", regex=True)
    return s.str.contains(r"^(?:pobox|postofficebox|pob\d|box\d|pmb\d|po\d)", regex=True).fillna(False)


# ---------------------------------------------------------------------------
# Master
# ---------------------------------------------------------------------------

def load_master(path=config.MASTER_PATH) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype=str, encoding=config.RAW_ENCODING,
                      encoding_errors="replace", keep_default_na=False)
    raw = raw.apply(squish)

    df = pd.DataFrame({"cust_id": to_int(raw["CUST-ID"])})
    df["legal_name"] = raw["LEGAL-NAME"]
    df["dba_name"] = raw["DBA-NAME"]

    # Collapse the ~140 Y/blank role columns into: role flag, effective date, form list.
    for prefix, name in ROLES.items():
        opt = raw.get(f"{prefix}-OPT")
        df[f"is_{name}"] = opt.notna() if opt is not None else False
        df[f"{name}_effdt"] = parse_yyyymmdd(raw[f"{prefix}-EFFDT"])
        form_cols = [c for c in raw.columns
                     if c.startswith(prefix + "-") and c not in (f"{prefix}-OPT", f"{prefix}-EFFDT")]
        forms = pd.Series("", index=raw.index, dtype="object")
        for c in form_cols:
            forms = forms + np.where(raw[c].eq("Y").fillna(False), c[len(prefix) + 1:] + ";", "")
        forms = forms.str.rstrip(";")
        df[f"{name}_forms"] = forms.mask(forms == "").astype("string")

    for kind in ("busn", "mail"):
        K = kind.upper()
        df[f"{kind}_addr1"] = raw[f"{K}-ADDR1"]
        df[f"{kind}_addr2"] = raw[f"{K}-ADDR2"]
        df[f"{kind}_addr3"] = raw[f"{K}-ADDR3"]
        df[f"{kind}_city"] = raw[f"{K}-CITY"]
        df[f"{kind}_state"] = raw[f"{K}-STATE"].str.upper()
        df[f"{kind}_postal"] = raw[f"{K}-POSTAL"]
        df[f"{kind}_country"] = raw[f"{K}-COUNTRY"].str.upper()
    df["busn_zip5"] = df["busn_postal"].str.extract(r"^(\d{5})", expand=False)

    df["country_cd"] = raw["COUNTRY-CD"]
    df["phone"] = digits_only(raw["PHONE"])
    df["area_code"] = df["phone"].str[:3].where(df["phone"].str.len() >= 10)
    df["app_submit_date"] = pd.to_datetime(raw["APP-SBMIT-DTTM"].str[:10], errors="coerce").dt.date
    df["app_revise_date"] = pd.to_datetime(raw["APP-REVIS-DT"], errors="coerce").dt.date
    df["ulc"] = raw["ULC"]
    df["cust_type"] = raw["CUST-TYPE"]
    df["partner_qty"] = to_int(raw["PARTNER-QTY"])
    df["app_profit_ind"] = raw["APP-PROFIT-IND"]
    df["state_ind"] = raw["STATE-IND"]

    for P, p in PERIODS.items():
        for f in RETURN_FIELDS:
            df[f"{p}_{f.lower().replace('-', '_')}"] = to_int(raw[f"{P}-{f}"])

    # Derived metrics (same definitions as the R code)
    for p in PERIODS.values():
        df[f"{p}_reject_rate"] = safe_ratio(df[f"{p}_rej_ret"], df[f"{p}_trnsmt_ret"])
    df["reject_rate_change"] = df["ytd_reject_rate"] - df["prv1_reject_rate"]
    df["trnsmit_rate_change"] = capped_change(df["ytd_trnsmt_ret"], df["prv1_trnsmt_ret"])
    df["firm_size"] = firm_size(df["ytd_trnsmt_ret"])

    # Filer flags: did the firm transmit any returns in the period?
    df["is_filer"] = df["ytd_trnsmt_ret"].fillna(0) > 0
    df["is_filer_prv1"] = df["prv1_trnsmt_ret"].fillna(0) > 0
    df["is_filer_prv2"] = df["prv2_trnsmt_ret"].fillna(0) > 0

    df = add_supercompany(df)
    return df.sort_values("ytd_trnsmt_ret", ascending=False, na_position="last").reset_index(drop=True)


def load_manual_supercompanies(path=config.CONSOLIDATED_FIRMS_XLSX) -> pd.DataFrame:
    x = pd.read_excel(path, sheet_name="large_firms", usecols=["cust_id", "Supercompany"])
    x = x.rename(columns={"Supercompany": "supercompany"}).dropna()
    x["supercompany"] = x["supercompany"].astype(str).str.strip()
    x = x[x["supercompany"] != ""]
    x["cust_id"] = x["cust_id"].astype("int64")
    return x


def add_supercompany(df: pd.DataFrame) -> pd.DataFrame:
    """Group related offices under one 'supercompany' (port of get_supercompanies_into_new_master)."""
    manual = load_manual_supercompanies()
    manual["supercompany"] = manual["supercompany"].str.lower()

    rest = df.loc[~df["cust_id"].isin(manual["cust_id"]), ["cust_id", "dba_name"]].copy()
    rest["supercompany"] = brand_from_dba(normalize_company(rest["dba_name"]))
    rest = rest.dropna(subset=["supercompany"])[["cust_id", "supercompany"]]

    groups = pd.concat([manual, rest], ignore_index=True)
    groups["supercompany_id"] = groups.groupby("supercompany")["cust_id"].transform("min")
    groups["cust_id"] = groups["cust_id"].astype("Int64")

    df = df.merge(groups, on="cust_id", how="left")
    name = df["supercompany"].fillna(df["dba_name"]).fillna(df["legal_name"])
    df["supercompany"] = smart_title(normalize_company(name).str.replace(r"\s+", " ", regex=True))
    df["supercompany_id"] = df["supercompany_id"].fillna(df["cust_id"]).astype("Int64")
    return df


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

def load_partners(path=config.PARTNER_PATH) -> pd.DataFrame:
    # Some rows end in `,",C` (stray quote in LICENSED-IND), so quoting is disabled
    # just like the R code's quote = "``".
    raw = pd.read_csv(path, dtype=str, encoding=config.RAW_ENCODING, encoding_errors="replace",
                      quoting=csv.QUOTE_NONE, keep_default_na=False)
    raw = raw.apply(squish)
    raw["LICENSED-IND"] = raw["LICENSED-IND"].where(raw["LICENSED-IND"].isin(["Y", "N"]))
    raw["PROF-TYPE"] = raw["PROF-TYPE"].str.upper()
    raw["cust_id"] = to_int(raw["CUST-ID"])
    raw_rows = len(raw)

    key = ["cust_id", "FIRST-NAME", "MIDDLE-NAME", "LAST-NAME", "NAME-SUFFIX"]
    raw[key[1:]] = raw[key[1:]].fillna("")
    join = lambda s: ";".join(sorted(set(s.dropna()))) or pd.NA  # noqa: E731
    p = (raw.groupby(key, sort=False)
            .agg(titles=("TITLE", join),
                 prof_types=("PROF-TYPE", join),
                 licensed=("LICENSED-IND", lambda s: (s == "Y").any()))
            .reset_index())
    p = p.rename(columns={"FIRST-NAME": "first_name", "MIDDLE-NAME": "middle_name",
                          "LAST-NAME": "last_name", "NAME-SUFFIX": "name_suffix"})
    parts = [smart_title(p[c].replace("", pd.NA)) for c in ("first_name", "middle_name", "last_name")]
    p["partner_name"] = squish(parts[0].fillna("") + " " + parts[1].fillna("") + " " +
                               parts[2].fillna("") + " " + p["name_suffix"].fillna(""))
    p.attrs["raw_rows"] = raw_rows
    return p


def summarize_partners(p: pd.DataFrame) -> pd.DataFrame:
    def label(r):
        codes = r.prof_types.split(";") if isinstance(r.prof_types, str) else []
        bits = [PROF_TYPE_LABELS.get(c, f"type {c}") for c in codes]
        if r.licensed:
            bits.append("licensed")
        return r.partner_name + (f" ({', '.join(bits)})" if bits else "")

    p = p.assign(_label=[label(r) for r in p.itertuples()])
    g = p.groupby("cust_id")
    out = pd.DataFrame({
        "partners_total": g.size(),
        "partners_licensed": g["licensed"].sum(),
        "partner_names": g["_label"].agg("; ".join),
    })
    types = p.assign(t=p["prof_types"].str.split(";")).explode("t").dropna(subset=["t"])
    counts = types.groupby(["cust_id", "t"]).size().unstack(fill_value=0)
    counts.columns = [f"partners_prof_{c.lower()}" for c in counts.columns]
    out = out.join(counts).reset_index()
    for c in out.columns:
        if c.startswith("partners_"):
            out[c] = out[c].fillna(0).astype("int64") if c != "partner_names" else out[c]
    return out


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def repair_split_lines(text: str, n_fields: int) -> str:
    """Join physical lines that were broken by a stray newline inside a record."""
    out, buf = [], ""
    for line in text.splitlines():
        buf = buf + line if buf else line
        if buf.count(",") + 1 >= n_fields:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out) + "\n"


def load_contacts(path=config.CONTACT_PATH) -> pd.DataFrame:
    text = path.read_bytes().decode(config.RAW_ENCODING, errors="replace")
    raw = pd.read_csv(io.StringIO(repair_split_lines(text, 7)), dtype=str,
                      quoting=csv.QUOTE_NONE, keep_default_na=False)
    raw = raw.apply(squish)
    c = pd.DataFrame({
        "cust_id": to_int(raw["CUST-ID"]),
        "is_primary": raw["PRIMARY-FLAG"].eq("Y").fillna(False),
        "first_name": raw["FIRST-NAME"],
        "middle_name": raw["MIDDLE-NAME"],
        "last_name": raw["LAST-NAME"],
        "country_code": raw["COUNTRY-CODE"],
        "phone": digits_only(raw["PHONE"]),
    })
    c["contact_name"] = squish(smart_title(c["first_name"]).fillna("") + " " +
                               smart_title(c["middle_name"]).fillna("") + " " +
                               smart_title(c["last_name"]).fillna(""))
    c = c.drop_duplicates()
    return c


def format_phone(s: pd.Series) -> pd.Series:
    ten = s.str.len() == 10
    out = s.copy()
    out[ten] = s[ten].str[:3] + "-" + s[ten].str[3:6] + "-" + s[ten].str[6:]
    return out


def summarize_contacts(c: pd.DataFrame) -> pd.DataFrame:
    c = c.sort_values(["cust_id", "is_primary"], ascending=[True, False])
    c = c.assign(_label=c["contact_name"].fillna("?") +
                 format_phone(c["phone"]).map(lambda x: f" ({x})" if isinstance(x, str) else ""))
    g = c.groupby("cust_id")
    prim = c[c["is_primary"]].drop_duplicates("cust_id").set_index("cust_id")
    out = pd.DataFrame({
        "contacts_total": g.size(),
        "contact_names": g["_label"].agg("; ".join),
    })
    out["primary_contact_name"] = prim["contact_name"]
    out["primary_contact_phone"] = prim["phone"]
    return out.reset_index()


# ---------------------------------------------------------------------------

def main():
    config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
    m = load_master()
    print(f"master:   {len(m):,} firms ({m['is_filer'].sum():,} filers this year)")
    p = load_partners()
    print(f"partners: {p.attrs['raw_rows']:,} raw rows -> {len(p):,} people at {p['cust_id'].nunique():,} firms")
    c = load_contacts()
    print(f"contacts: {len(c):,} rows at {c['cust_id'].nunique():,} firms")
    m.to_pickle(config.INTERIM_DIR / "master.pkl")
    p.to_pickle(config.INTERIM_DIR / "partners.pkl")
    c.to_pickle(config.INTERIM_DIR / "contacts.pkl")
    print("firm sizes:", m["firm_size"].value_counts().to_dict())


if __name__ == "__main__":
    main()
