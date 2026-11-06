"""Upload Census batch files and save the results (needs internet access).

Run from the repository root on a computer with normal internet access:

    python3 -m pipeline.geocode_submit                 # every batch without results
    python3 -m pipeline.geocode_submit --only 2 5      # just batches 2 and 5
    python3 -m pipeline.geocode_submit --only 2 --split
        # send batch 2 in 1,000-row pieces; if the Census rejects a piece (HTTP 400)
        # it is halved until the offending rows are found. Good rows are saved as the
        # normal result file; rejected rows go to data/geocode/rejected_rows.csv.

Uses only the Python standard library. Each batch of ~10,000 addresses usually
takes 3-20 minutes at the Census Bureau. Results are saved to
data/geocode/results/result_NNNN.csv, so the script can be stopped and re-run.
Everything printed is also appended to data/geocode/submit.log.
Afterwards run:  python3 -m pipeline.geocode import

Manual alternative: upload a batch file at
https://geocoding.geo.census.gov/geocoder/geographies/addressbatch?form
(Benchmark "Public_AR_Current", Vintage "Current_Current") and save the
download as data/geocode/results/result_NNNN.csv.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEO_DIR = ROOT / "data" / "geocode"
BATCH_DIR = GEO_DIR / "batches"
RESULT_DIR = GEO_DIR / "results"
LOG_PATH = GEO_DIR / "submit.log"
REJECTED_PATH = GEO_DIR / "rejected_rows.csv"
# Overridable so the script can be tested against a local fake server.
URL = os.environ.get("CENSUS_BATCH_URL",
                     "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch")
FORM = {"benchmark": "Public_AR_Current", "vintage": "Current_Current"}
RETRY_WAIT = int(os.environ.get("CENSUS_RETRY_WAIT", "60"))  # seconds, multiplied by attempt

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class CensusError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status = status
        self.detail = detail


def multipart(fields: dict, file_name: str, data: bytes):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; '
                 f'filename="{file_name}"\r\nContent-Type: text/csv\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post(file_name: str, rows: list[bytes]) -> str:
    """One upload. Returns the result CSV text or raises CensusError."""
    body, ctype = multipart(FORM, file_name, b"\n".join(rows) + b"\n")
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": ctype,
                                                          "User-Agent": "irs-ecds-browser"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", errors="replace").strip()
        raise CensusError(e.code, detail or f"(empty body; {e.reason})") from None
    n_out = len([ln for ln in text.splitlines() if ln.strip()])
    if text.lstrip().startswith("<") or n_out < max(1, int(len(rows) * 0.9)):
        raise CensusError(0, f"unexpected response ({n_out} lines for {len(rows)} rows): {text[:200]!r}")
    return text if text.endswith("\n") else text + "\n"


def post_with_retry(file_name: str, rows: list[bytes], retries: int, retry_400: bool) -> str:
    for attempt in range(1, retries + 1):
        try:
            return post(file_name, rows)
        except (CensusError, OSError) as e:
            if isinstance(e, CensusError) and e.status == 400 and not retry_400:
                raise
            if attempt == retries:
                raise
            wait = RETRY_WAIT * attempt
            log(f"{file_name}: attempt {attempt} failed: {e} -- retrying in {wait}s")
            time.sleep(wait)


def result_path(batch: Path) -> Path:
    return RESULT_DIR / batch.name.replace("batch_", "result_")


def save(out: Path, text: str) -> None:
    tmp = out.with_suffix(".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)


def read_rows(batch: Path) -> list[bytes]:
    return [ln for ln in batch.read_bytes().splitlines() if ln.strip()]


def submit(batch: Path, retries: int = 4) -> str:
    out = result_path(batch)
    if out.exists() and out.stat().st_size > 0:
        return f"{batch.name}: already done"
    rows = read_rows(batch)
    t0 = time.time()
    log(f"{batch.name}: uploading {len(rows):,} rows")
    try:
        text = post_with_retry(batch.name, rows, retries, retry_400=True)
    except (CensusError, OSError) as e:
        return (f"{batch.name}: FAILED after {retries} attempts ({e}). Re-run later, or try "
                f"`--only {int(batch.stem[-4:])} --split` to find rows the Census rejects.")
    save(out, text)
    return (f"{batch.name}: {len(text.splitlines()):,} rows, {text.count(chr(34) + 'Match' + chr(34)):,} "
            f"matched in {time.time() - t0:.0f}s")


def submit_split(batch: Path, chunk_rows: int) -> str:
    """Send a batch in pieces, halving any piece that gets HTTP 400 to isolate bad rows."""
    out = result_path(batch)
    if out.exists() and out.stat().st_size > 0:
        return f"{batch.name}: already done"
    rows = read_rows(batch)
    pieces: list[str] = []
    rejected: list[bytes] = []

    def go(rs: list[bytes], label: str):
        try:
            pieces.append(post_with_retry(f"{batch.stem}_{label}.csv", rs, 3, retry_400=False))
            log(f"{batch.name} rows {label}: ok")
        except CensusError as e:
            if e.status != 400:
                raise
            if len(rs) == 1:
                time.sleep(5)  # one more try in case the 400 was transient
                try:
                    pieces.append(post(f"{batch.stem}_{label}.csv", rs))
                    return
                except CensusError:
                    pass
                rejected.append(rs[0])
                log(f"{batch.name}: Census rejects row: {rs[0].decode(errors='replace')}  ({e})")
                return
            log(f"{batch.name} rows {label}: HTTP 400 -> splitting")
            mid = len(rs) // 2
            lo, hi = (int(x) for x in label.split("-"))
            go(rs[:mid], f"{lo}-{lo + mid - 1}")
            go(rs[mid:], f"{lo + mid}-{hi}")

    log(f"{batch.name}: split mode, {len(rows):,} rows in pieces of {chunk_rows:,}")
    for i in range(0, len(rows), chunk_rows):
        piece = rows[i:i + chunk_rows]
        go(piece, f"{i + 1}-{i + len(piece)}")
    save(out, "".join(pieces))
    if rejected:
        with REJECTED_PATH.open("ab") as f:
            f.write(b"\n".join(rejected) + b"\n")
    return (f"{batch.name}: split mode done, {len(rows) - len(rejected):,} rows saved, "
            f"{len(rejected)} rejected" + (f" (see {REJECTED_PATH.name})" if rejected else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel uploads (default 1; the Census may refuse simultaneous uploads)")
    ap.add_argument("--only", type=int, nargs="+", metavar="N", help="batch numbers to send")
    ap.add_argument("--split", action="store_true", help="send in pieces and isolate rejected rows")
    ap.add_argument("--chunk-rows", type=int, default=1000, help="piece size for --split")
    args = ap.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    batches = sorted(BATCH_DIR.glob("batch_*.csv"))
    if args.only:
        batches = [b for b in batches if int(b.stem[-4:]) in set(args.only)]
    todo = [b for b in batches if not (result_path(b).exists() and result_path(b).stat().st_size > 0)]
    log(f"{len(batches)} batch files selected, {len(todo)} without results; sending to {URL}")
    if args.workers > 1:
        log("note: running uploads in parallel; if you see HTTP 400 errors, use --workers 1")

    if args.split:
        for b in todo:
            log(submit_split(b, args.chunk_rows))
        return
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for msg in pool.map(submit, todo):
            log(msg)


if __name__ == "__main__":
    main()
