# JeremyGarmin

Garmin Connect health reporting with [garmin-pp-cli](https://printingpress.dev).

## Daily morning report

`scripts/garmin_daily_report.py` refreshes the local Garmin archive, compares the
last 24 hours (last night's sleep, this morning's readiness, yesterday's daytime
totals) with the 7-day and 30-day averages, writes recommendations for metrics
moving the wrong way, and renders `reports/garmin-daily.html` with inline charts.

```bash
scripts/garmin_daily_report.py                 # today (America/Los_Angeles)
scripts/garmin_daily_report.py --date 2026-10-02 --no-sync
```

Requires a signed-in `garmin-pp-cli` (`garmin-pp-cli auth login --email ...`).
Exit code 4 means the Garmin sign-in has expired. Reports are git-ignored because
they contain personal health data.
