#!/usr/bin/env python3
"""One-time conversion of Apple Health lab results (Health Records, FHIR JSON) to a flat CSV.

Input: the `clinical-records` folder from the Health app's "Export All Health Data", a zip of it,
or the whole export.zip. Output columns: date,test,loinc,value,unit,low,high — the format
garmin_daily_report.py reads from reports/labs/.

    python3 scripts/labs_to_csv.py <folder-or-zip> [...] -o reports/labs/apple-health-labs.csv
"""
import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_daily_report import _walk_fhir, _lab_key  # noqa: E402


def observations(path: Path):
    if path.is_dir():
        for f in sorted(path.rglob("*.json")):
            try:
                yield from _walk_fhir(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".json") and ("clinical" in n.lower() or "/" not in n.strip("/")):
                    try:
                        yield from _walk_fhir(json.loads(z.read(n)))
                    except ValueError:
                        continue
    elif path.suffix.lower() == ".json":
        yield from _walk_fhir(json.loads(path.read_text()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("reports/labs/apple-health-labs.csv"))
    ap.add_argument("--all", action="store_true", help="keep tests the report doesn't recognise too")
    a = ap.parse_args()
    rows, seen = [], set()
    for p in a.inputs:
        for o in observations(p):
            if not a.all and not _lab_key(o["codes"], o["name"]):
                continue
            loinc = next(iter(sorted(c for c in o["codes"] if c)), "")
            key = (o["d"], loinc or o["name"].lower(), o["value"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(date=o["d"], test=o["name"], loinc=loinc, value=o["value"], unit=o["unit"],
                             low="" if o["low"] is None else o["low"], high="" if o["high"] is None else o["high"]))
    rows.sort(key=lambda r: (r["test"].lower(), r["date"]))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "test", "loinc", "value", "unit", "low", "high"])
        w.writeheader()
        w.writerows(rows)
    tests = sorted({r["test"] for r in rows})
    years = sorted({r["date"][:4] for r in rows})
    print(f"{len(rows)} results, {len(tests)} tests, {years[0] + '-' + years[-1] if years else 'no dates'} -> {a.out}")


if __name__ == "__main__":
    main()
