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

The report also estimates target paces (threshold and zone 2) from recent run laps,
spells out today's workout with paces and recoveries, reviews the last interval
session against the previous one of the same kind, and shows weekly mileage, load,
run heart-rate zones (with the 80% easy-running check), VO2 max trend, sleep
consistency, the Garmin sign-in expiry, and how VO2 max, resting heart
rate, steps, sleep and BMI compare with men your age (sourced norms), plus a 13-week body-composition chart from the Index
scale (read from Garmin's weight service with the CLI's stored sign-in). `--json` includes a one-line `headline`
for phone notifications.

Requires a signed-in `garmin-pp-cli` (`garmin-pp-cli auth login --email ...`).
Exit code 4 means the Garmin sign-in has expired. Reports are git-ignored because
they contain personal health data.
