#!/usr/bin/env python3
"""One-time conversion of Apple Health lab results (Health Records, FHIR JSON) to a flat CSV.

Input: the `clinical-records` folder from the Health app's "Export All Health Data", a zip of it,
the whole export.zip, or the Health Records PDF (or its text as .txt). Output columns: date,test,loinc,value,unit,low,high — the format
garmin_daily_report.py reads from reports/labs/.

    python3 scripts/labs_to_csv.py <folder-or-zip> [...] -o reports/labs/apple-health-labs.csv
"""
import argparse
import csv
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_daily_report import _walk_fhir, _lab_key  # noqa: E402


DATE_HDR = re.compile(r"^([A-Z][a-z]{2} \d{1,2}, \d{4}) TST CURRNT")
ROW = re.compile(r"^(?P<name>.+?)\s+(?P<val>[<>]?-?\d+(?:\.\d+)?)"
                 r"(?:\s+(?P<unit>(?!to\b)[A-Za-z%\u00b5][^\s]*))?"
                 r"(?:\s+(?P<lo>-?\d+(?:\.\d+)?)\s+to\s+(?P<hi>-?\d+(?:\.\d+)?))?"
                 r"(?:\s+(?P<flag>OUT OF RANG\w*|HIGH|LOW|H|L))?\s*$")


def text_observations(text: str):
    """Lab tables from the Health app's 'Health Records' PDF (as text): a 'Mon D, YYYY TST CURRNT ...' header,
    then one 'Test value unit low to high [flag]' row per result."""
    from datetime import datetime
    day = None
    text = re.sub("[\ue000-\uf8ff]", "", text)  # the PDF draws some letters with private-use glyphs
    for line in text.splitlines():
        line = line.strip()
        m = DATE_HDR.match(line)
        if m:
            day = datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
            continue
        if line in ("Lab Results",) or not line:
            continue
        if line.startswith(("Medication Records", "Clinical Vitals", "Immunizations", "Conditions", "Clinical Notes", "Allergies")):
            day = None
            continue
        if not day:
            continue
        r = ROW.match(line)
        if not r or len(r.group("name")) > 60:
            continue
        num = lambda x: float(x) if x is not None else None
        yield dict(d=day, codes=set(), name=r.group("name").strip(), value=float(r.group("val").lstrip("<>")),
                   unit=r.group("unit") or "", low=num(r.group("lo")), high=num(r.group("hi")))


def observations(path: Path):
    if path.suffix.lower() in (".txt", ".pdf"):
        if path.suffix.lower() == ".pdf":
            import subprocess
            text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True).stdout
        else:
            text = path.read_text()
        yield from text_observations(text)
        return
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
