"""Tests for the ECDS pipeline.  Run:  python3 -m unittest discover -s tests -t ."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline import build_site, clean, config, geocode


class CleanHelpers(unittest.TestCase):
    def test_squish_and_digits(self):
        s = pd.Series(["  a   b ", "", None, "603/867-5390              "])
        self.assertEqual(clean.squish(s).tolist()[0], "a b")
        self.assertTrue(pd.isna(clean.squish(s)[1]))
        self.assertEqual(clean.digits_only(s)[3], "6038675390")

    def test_smart_title(self):
        s = pd.Series(["JOHN SMITH", "mary jones", "McDonald"])
        self.assertEqual(clean.smart_title(s).tolist(), ["John Smith", "Mary Jones", "McDonald"])

    def test_firm_size_buckets(self):
        s = pd.Series([0, 99, 100, 999, 1000, 9999, 10000], dtype="Int64")
        self.assertEqual(clean.firm_size(s).tolist(),
                         ["tiny", "tiny", "small", "small", "medium", "medium", "large"])

    def test_capped_change(self):
        cur = pd.Series([150, 0, 10, 0, 500], dtype="Int64")
        prev = pd.Series([100, 100, 0, 0, 100], dtype="Int64")
        out = clean.capped_change(cur, prev).tolist()
        self.assertAlmostEqual(out[0], 0.5)
        self.assertEqual(out[1], -1.0)
        self.assertEqual(out[2], 1.0)     # new filer
        self.assertTrue(np.isnan(out[3]))  # 0 -> 0
        self.assertEqual(out[4], 1.0)     # capped

    def test_reject_rate_zero_denominator(self):
        r = clean.safe_ratio(pd.Series([1, 0], dtype="Int64"), pd.Series([4, 0], dtype="Int64"))
        self.assertEqual(r[0], 0.25)
        self.assertTrue(np.isnan(r[1]))

    def test_brand_and_suffix(self):
        names = pd.Series(["H & R Block #123", "Jackson Hewitt Tax Service", "HRB Tax Group, Inc.",
                           "Smith CPA LLC", "DBA/ JACKSON HEWITT"])
        norm = clean.normalize_company(names)
        self.assertEqual(norm[3], "smith cpa")
        b = clean.brand_from_dba(norm).tolist()
        self.assertEqual(b[:3], ["h&r block", "jackson hewitt", "h&r block"])
        self.assertTrue(pd.isna(b[3]))
        self.assertEqual(b[4], "jackson hewitt")

    def test_po_box(self):
        s = pd.Series(["P.O. Box 12", "PO BOX 5", "123 Boxwood Ln", "Box 44", None])
        self.assertEqual(clean.is_po_box(s).tolist(), [True, True, False, True, False])

    def test_repair_split_lines(self):
        text = "A,B,C\n1,2,3\n4,\n5,6\n7,8,9\n"
        self.assertEqual(clean.repair_split_lines(text, 3), "A,B,C\n1,2,3\n4,5,6\n7,8,9\n")


class GeocodeKeys(unittest.TestCase):
    def test_key_matches_addresscleaner_format(self):
        k = geocode.make_key(pd.Series(["1499 W Palmetto Park RD"]), pd.Series(["Suite 107"]),
                             pd.Series(["Boca Raton"]), pd.Series(["FL"]), pd.Series(["33486"]))
        self.assertEqual(k[0], "1499 w palmetto park rd suite 107, boca raton, fl 33486")

    def test_census_field_removes_quotes_and_commas(self):
        s = geocode.census_field(pd.Series(['2881 EL CAMINO REAL SUITE "G"', "12 Main St, Ste 4", "Café  Rd", None,
                                            "2470 WINDY HILL RD STE 115\\", "|6314 Corina Dr", "317 W 6th St. Suite:102",
                                            "58 East 100 North (83-13)", "O'Fallon"]))
        self.assertEqual(s.tolist(), ["2881 EL CAMINO REAL SUITE G", "12 Main St Ste 4", "Cafe Rd", "",
                                      "2470 WINDY HILL RD STE 115", "6314 Corina Dr", "317 W 6th St. Suite 102",
                                      "58 East 100 North (83-13)", "O'Fallon"])

    def test_census_street_prefers_house_number(self):
        s = geocode.census_street(pd.Series(["Suite 5", "12 Main St"]), pd.Series(["100 Oak Ave", "Ste 2"]))
        self.assertEqual(s.tolist(), ["100 Oak Ave", "12 Main St"])


class GeocodeImport(unittest.TestCase):
    """Import synthetic Census results into an empty copy of the db schema."""

    def test_import_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "db.sqlite3"
        with sqlite3.connect(f"file:{config.GEOCODE_DB}?mode=ro", uri=True) as src, sqlite3.connect(db) as dst:
            for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE 'geocode_%'"):
                dst.execute(sql)
        old = (config.RESULT_DIR, config.BATCH_KEYS, config.GEOCODE_DB)
        try:
            config.RESULT_DIR = tmp / "results"
            config.RESULT_DIR.mkdir()
            config.BATCH_KEYS = tmp / "keys.csv"
            pd.DataFrame({
                "id": ["1", "2"], "batch": ["1", "1"],
                "address_key": ["4600 silver hill rd, washington, dc 20233", "1 nowhere, x, dc 00000"],
                "street": ["4600 Silver Hill Rd", "1 Nowhere"], "street2": ["", ""],
                "city": ["Washington", "X"], "state": ["DC", "DC"], "postal": ["20233", "00000"],
            }).to_csv(config.BATCH_KEYS, index=False)
            (config.RESULT_DIR / "result_0001.csv").write_text(
                '"1","4600 Silver Hill Rd, Washington, DC, 20233","Match","Exact",'
                '"4600 SILVER HILL RD, WASHINGTON, DC, 20233","-76.92748,38.84601","76355984","L",'
                '"11","001","980000","1034"\n'
                '"2","1 Nowhere, X, DC, 00000","No_Match"\n')
            geocode.import_results(db_path=db)
            geocode.import_results(db_path=db)  # second run must not duplicate
            with sqlite3.connect(db) as con:
                rows = con.execute("SELECT address_key, status, clean_address_id FROM geocode_rawaddress "
                                   "ORDER BY id").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][1], "ok")
                self.assertEqual(rows[1][1], "error")
                x, y, staddr, sub = con.execute(
                    "SELECT x, y, staddr, subregion FROM geocode_cleanaddress").fetchone()
                self.assertAlmostEqual(x, -76.92748)
                self.assertEqual(staddr, "4600 SILVER HILL RD")
                self.assertEqual(sub, "District of Columbia")
                lk = geocode.db_lookup(con)
                self.assertEqual(lk.loc[lk.raw_status == "ok", "county_fips"].iloc[0], "001")
        finally:
            config.RESULT_DIR, config.BATCH_KEYS, config.GEOCODE_DB = old


class BuildSite(unittest.TestCase):
    @staticmethod
    def _firms():
        n = 6
        return pd.DataFrame({
            "cust_id": pd.array([10, 11, 12, 13, 14, 15], dtype="Int64"),
            "geo_precision": ["address", "zip", "address", "none", "address", "zip"],
            "geo_lat": [39.62950, 39.62950, 34.05220, np.nan, 39.62951, 38.90000],
            "geo_lon": [-79.95590, -79.95590, -118.24370, np.nan, -79.95591, -77.03000],
            "dba_name": pd.array(["ACME TAX", None, "LA TAX", "GONE", "H&R BLOCK 123", "DC CPA"], dtype="string"),
            "legal_name": ["ACME TAX LLC", "BETA CPA", "LA TAX INC", "GONE", "HRB GROUP", "DC CPA PC"],
            "supercompany": pd.array(["Acme Tax", "Beta", "Someone Else", "", "H&R Block", None], dtype="string"),
            "busn_city": ["MORGANTOWN", "MORGANTOWN", "LOS ANGELES", "X", "MORGANTOWN", None],
            "mail_city": [None, None, None, None, None, "WASHINGTON"],
            "busn_state": ["WV", "WV", "CA", "WV", "WV", "DC"],
            "busn_country": ["USA"] * n,
            "busn_addr1": ["1 MAIN ST", "2 HIGH ST", "3 SUNSET BLVD", "X", "5 MAIN ST", "6 K ST"],
            "busn_addr2": [None] * n,
            "busn_postal": ["26505", "26505", "90012", "26505", "26505", "20001"],
            "ytd_trnsmt_ret": pd.array([50, 5000, 20000, 1, None, 0], dtype="Int64"),
            "firm_size": ["tiny", "medium", "large", "tiny", "tiny", "tiny"],
            "is_filer": pd.array([True, True, True, True, False, False], dtype="boolean"),
            "is_filer_prv1": [True] * n,
            "is_ero": [True, False, True, False, True, False],
            "geo_matched_address": [None] * n, "geo_county_name": ["Monongalia County"] * n,
            "phone": ["3045550100"] * n, "ero_effdt": [date(2014, 12, 23)] * n,
            "ytd_act_ret": [0] * n, "ytd_rej_ret": [0] * n,
            "prv1_trnsmt_ret": [0] * n, "prv2_trnsmt_ret": [0] * n,
            "ytd_reject_rate": [np.nan] * n, "prv1_reject_rate": [0.12346] * n,
            "partners_total": [1] * n, "partners_licensed": [0] * n,
            "partner_names": ["Jane Doe (CPA)"] * n, "contact_names": [None] * n,
        })

    def test_brand_dropped_when_in_name(self):
        self.assertEqual(build_site.drop_redundant_brand(["Acme", "H&R Block", "Other", ""],
                                                         ["Acme Tax", "H&R Block 123", "Beta", "X"]),
                         ["", "", "Other", ""])

    def test_build_round_trip(self):
        src = self._firms()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "firms.json").write_text("{}")          # leftovers from an older build
            (out / "details").mkdir()
            (out / "details" / "999.json").write_text("{}")
            info = build_site.build(src, out, rows_per_shard=2, build_id="test")
            P = json.loads((out / "points.json").read_text())
            N_ = json.loads((out / "names.json").read_text())
            shards = sorted((out / "details").glob("*.json"), key=lambda p: int(p.stem))

            self.assertFalse((out / "firms.json").exists())
            self.assertEqual([p.name for p in shards], ["0.json", "1.json", "2.json"])
            self.assertEqual(info, {"firms": 5, "shards": 3, "v": "test"})
            self.assertEqual(P["v"], "test")
            self.assertEqual(N_["v"], "test")

            rows = [r for p in shards for r in self._shard(p)]
            ids = [r["id"] for r in rows]
            self.assertEqual(sorted(ids), [10, 11, 12, 14, 15])  # 13 has no location
            lat = np.cumsum(P["lat"]) / P["scale"]
            lon = np.cumsum(P["lon"]) / P["scale"]
            by_id = src.set_index(src["cust_id"].astype(int))
            for k, cid in enumerate(ids):
                s = by_id.loc[cid]
                self.assertAlmostEqual(lat[k], s.geo_lat, places=5)
                self.assertAlmostEqual(lon[k], s.geo_lon, places=5)
                a = P["a"][k]
                self.assertEqual(P["sizes"][a & 3], s.firm_size)
                self.assertEqual((a >> 2) & 1, int(s.is_filer))
                self.assertEqual((a >> 3) & 1, int(s.geo_precision == "zip"))
                self.assertEqual(N_["r"][k], 0 if pd.isna(s.ytd_trnsmt_ret) else int(s.ytd_trnsmt_ret))
            k = ids.index(11)
            self.assertEqual((N_["n"][k], N_["c"][k]), ("Beta Cpa", "Morgantown"))
            self.assertEqual(P["states"][P["s"][k]], "WV")
            self.assertEqual(N_["c"][ids.index(15)], "Washington")
            self.assertEqual(N_["sc"][ids.index(10)], "")              # "Acme Tax" is in the name
            self.assertEqual(N_["sc"][ids.index(12)], "Someone Else")
            # Same spot: busier firm first; the Morgantown firms are neighbours.
            self.assertEqual(ids.index(11) + 1, ids.index(10))
            self.assertEqual(abs(ids.index(14) - ids.index(10)), 1)
            d = rows[ids.index(10)]
            self.assertEqual((d["ero"], d["roles"], d["rr1"]), ("2014-12-23", "ero", 0.1235))
            self.assertNotIn("rr", d)
            self.assertNotIn("contacts", d)

    @staticmethod
    def _shard(path):
        data = json.loads(path.read_text())
        assert data["v"] == "test"
        return data["rows"]


if __name__ == "__main__":
    unittest.main()
