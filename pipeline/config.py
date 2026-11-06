"""Shared paths and settings for the ECDS pipeline.

Update RELEASE when a new FOIA extract is dropped into raw/.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Source files --------------------------------------------------------
RELEASE = "2026-06-22"
RAW_DIR = ROOT / "raw"
MASTER_PATH = RAW_DIR / f"FOIA-MASTER.DT{RELEASE}.TXT"
PARTNER_PATH = RAW_DIR / f"FOIA-PARTNER.DT{RELEASE}.TXT"
CONTACT_PATH = RAW_DIR / f"FOIA-CONTCT.DT{RELEASE}.TXT"
CONSOLIDATED_FIRMS_XLSX = ROOT / "old" / "consolidated_firms.xlsx"

# The IRS files are Windows-1252, not UTF-8 (e.g. 0xB2 = "²", 0xDD = "Ý").
RAW_ENCODING = "cp1252"

# --- Geocoding database (the Django addresscleaner project) -------------
GEOCODE_DB = ROOT / "addresscleaner" / "db.sqlite3"
COUNTY_FIPS_CSV = ROOT / "addresscleaner" / "data_cleanaddresscounty" / "all-geocodes-v2023.csv"
GEOCODE_DIR = ROOT / "data" / "geocode"
BATCH_DIR = GEOCODE_DIR / "batches"          # CSVs to upload to the Census geocoder
RESULT_DIR = GEOCODE_DIR / "results"         # CSVs returned by the Census geocoder
BATCH_KEYS = GEOCODE_DIR / "batch_keys.csv"  # batch row id -> address_key
CENSUS_BATCH_SIZE = 9999                     # Census limit is 10,000 rows per file

# --- Outputs -------------------------------------------------------------
INTERIM_DIR = ROOT / "data" / "interim"
OUTPUT_DIR = ROOT / "data" / "output"
EXPORT_CSV = OUTPUT_DIR / f"ecds_firms_{RELEASE}.csv"
DICTIONARY_MD = OUTPUT_DIR / "data_dictionary.md"
SITE_DIR = ROOT / "docs"                     # GitHub Pages serves /docs on main
SITE_DATA_DIR = SITE_DIR / "data"

# --- Firm size buckets (YTD transmitted returns), same as the old R code -
LARGE_FIRM_SIZE = 10_000
MEDIUM_FIRM_SIZE = 1_000
SMALL_FIRM_SIZE = 100

# Cap on year-over-year change in transmitted returns (+/- 100%).
ABS_CHANGE_THRESHOLD = 1.0
