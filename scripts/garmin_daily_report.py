#!/usr/bin/env python3
"""Daily Garmin executive summary.

Refreshes the local garmin-pp-cli archive, compares the last 24 hours against
the 7-day and 30-day averages, writes recommendations for metrics moving the
wrong way, and renders a self-contained HTML report with inline SVG charts.

Usage:
  scripts/garmin_daily_report.py [--date YYYY-MM-DD] [--out PATH] [--no-sync]

The report date is the morning being summarised: last night's sleep and this
morning's readiness belong to it, daytime totals (steps, stress, training)
come from the day before.

Exit codes: 0 ok, 4 Garmin sign-in needed, 1 anything else.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import math
import os
import statistics as st
import subprocess
import sys
from pathlib import Path

CLI = os.environ.get("GARMIN_CLI", "garmin-pp-cli")
LOOKBACK = 45  # days pulled from the archive (30-day window + chart padding)
# Workout suggestions only ever use these activities.
ACTIVITIES = ("walking", "strength", "running")
HARD_LABELS = {"VO2MAX", "THRESHOLD", "ANAEROBIC_CAPACITY", "SPEED"}


# ---------------------------------------------------------------- data access

def run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run([CLI, *args], capture_output=True, text=True, timeout=timeout)


def sql(query: str) -> list[dict]:
    p = run(["sql", query, "--csv"], timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"sql failed: {p.stderr.strip()}")
    rows = list(csv.DictReader(io.StringIO(p.stdout)))
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if v in ("", None):
                clean[k] = None
            else:
                try:
                    clean[k] = float(v)
                except ValueError:
                    clean[k] = v
        out.append(clean)
    return out


def sync() -> None:
    p = run(["history"], timeout=1800)
    if p.returncode == 4 or "credentials" in p.stderr.lower() and p.returncode != 0:
        raise PermissionError(p.stderr.strip() or "Garmin sign-in needed")
    if p.returncode != 0:
        raise RuntimeError(f"history sync failed: {p.stderr.strip()[-500:]}")


def check_auth() -> None:
    p = run(["auth", "status"], timeout=60)
    if p.returncode != 0 or "Not signed in" in p.stdout:
        raise PermissionError("Garmin sign-in needed")


def load(report: dt.date) -> dict[str, dict]:
    start = (report - dt.timedelta(days=LOOKBACK)).isoformat()
    end = report.isoformat()
    days: dict[str, dict] = {}

    def put(rows, key="d"):
        for r in rows:
            d = r.pop(key)
            if isinstance(d, str):
                days.setdefault(d[:10], {}).update({k: v for k, v in r.items() if v is not None})

    S = "json_extract(data,'$.dailySleepDTO.{}')"
    put(sql(f"""SELECT r.id d,
        {S.format('sleepTimeSeconds')}/3600.0 sleep_h,
        {S.format('sleepScores.overall.value')} sleep_score,
        {S.format('deepSleepSeconds')}/60.0 deep_min,
        {S.format('lightSleepSeconds')}/60.0 light_min,
        {S.format('remSleepSeconds')}/60.0 rem_min,
        {S.format('awakeSleepSeconds')}/60.0 awake_min,
        {S.format('avgSleepStress')} sleep_stress,
        {S.format('sleepStartTimestampLocal')} bed_ms,
        {S.format('sleepEndTimestampLocal')} wake_ms,
        {S.format('sleepNeed.actual')} sleep_need_min,
        {S.format('averageSpO2Value')} sleep_spo2,
        {S.format('averageRespirationValue')} sleep_resp,
        json_extract(data,'$.avgOvernightHrv') hrv,
        json_extract(data,'$.hrvStatus') hrv_status,
        json_extract(data,'$.restingHeartRate') sleep_rhr,
        json_extract(data,'$.avgSkinTempDeviationC') skin_temp_dev
      FROM resources r WHERE resource_type='sleep_detail' AND r.id BETWEEN '{start}' AND '{end}'"""))

    # Several readiness rows per day; keep the latest by timestamp.
    put(sql(f"""SELECT d, score readiness, acute acute_load, rec recovery_h, fb readiness_feedback FROM (
        SELECT substr(r.id,1,10) d, json_extract(data,'$.score') score,
          json_extract(data,'$.acuteLoad') acute,
          json_extract(data,'$.recoveryTime')/60.0 rec,
          json_extract(data,'$.feedbackShort') fb,
          row_number() OVER (PARTITION BY substr(r.id,1,10) ORDER BY json_extract(data,'$.timestamp') DESC) rn
        FROM resources r WHERE resource_type='training_readiness' AND substr(r.id,1,10) BETWEEN '{start}' AND '{end}')
      WHERE rn=1"""))

    put(sql(f"""SELECT r.id d,
        json_extract(data,'$.totalSteps') steps,
        json_extract(data,'$.dailyStepGoal') step_goal,
        NULLIF(json_extract(data,'$.averageStressLevel'),-1) stress,
        json_extract(data,'$.highStressDuration')/60.0 high_stress_min,
        json_extract(data,'$.bodyBatteryAtWakeTime') bb_wake,
        json_extract(data,'$.bodyBatteryLowestValue') bb_low,
        json_extract(data,'$.bodyBatteryDuringSleep') bb_charge,
        json_extract(data,'$.restingHeartRate') rhr,
        json_extract(data,'$.averageSpo2') spo2,
        json_extract(data,'$.avgWakingRespirationValue') resp
      FROM resources r WHERE resource_type='daily_summary' AND r.id BETWEEN '{start}' AND '{end}'"""))

    acts = sql(f"""SELECT substr(json_extract(data,'$.startTimeLocal'),1,10) d,
        json_extract(data,'$.startTimeLocal') start,
        json_extract(data,'$.activityName') name,
        json_extract(data,'$.activityType.typeKey') type,
        json_extract(data,'$.duration')/60.0 dur_min,
        json_extract(data,'$.activityTrainingLoad') load,
        json_extract(data,'$.averageHR') avg_hr,
        json_extract(data,'$.aerobicTrainingEffect') te,
        json_extract(data,'$.trainingEffectLabel') te_label
      FROM resources WHERE resource_type='activities'
        AND substr(json_extract(data,'$.startTimeLocal'),1,10) BETWEEN '{start}' AND '{end}'
      ORDER BY start""")
    for a in acts:
        d = days.setdefault(a["d"], {})
        d.setdefault("activities", []).append(a)
        d["train_load"] = d.get("train_load", 0) + (a["load"] or 0)
        d["train_min"] = d.get("train_min", 0) + (a["dur_min"] or 0)
    # Days without activities trained zero, as long as the watch recorded the day.
    for k, d in days.items():
        if "steps" in d:
            d.setdefault("train_load", 0.0)
            d.setdefault("train_min", 0.0)
    fill_live(days, report)
    return days


def live(args: list[str]):
    p = run([*args, "--agent", "--data-source", "live"], timeout=120)
    if p.returncode != 0:
        return None
    try:
        r = json.loads(p.stdout).get("results")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(r, list):
        r = max(r, key=lambda x: str(x.get("timestamp", "")), default=None) if r else None
    return r if isinstance(r, dict) else None


def dig(obj, path):
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def fill_live(days, report):
    """`history` treats per-day series as complete only through yesterday, so this
    morning's sleep, readiness and summary are fetched live when the archive lacks them."""
    d = report.isoformat()
    rec = days.setdefault(d, {})

    def take(src, mapping):
        if not src:
            return
        for key, (path, scale) in mapping.items():
            v = dig(src, path)
            if isinstance(v, (int, float)) and not (key in ("stress",) and v < 0):
                rec.setdefault(key, v * scale)
            elif isinstance(v, str) and key == "hrv_status":
                rec.setdefault(key, v)

    if "sleep_h" not in rec:
        take(live(["sleep", "night", "x", "--date", d]), {
            "sleep_h": ("dailySleepDTO.sleepTimeSeconds", 1 / 3600), "sleep_score": ("dailySleepDTO.sleepScores.overall.value", 1),
            "deep_min": ("dailySleepDTO.deepSleepSeconds", 1 / 60), "light_min": ("dailySleepDTO.lightSleepSeconds", 1 / 60),
            "rem_min": ("dailySleepDTO.remSleepSeconds", 1 / 60), "awake_min": ("dailySleepDTO.awakeSleepSeconds", 1 / 60),
            "sleep_stress": ("dailySleepDTO.avgSleepStress", 1), "bed_ms": ("dailySleepDTO.sleepStartTimestampLocal", 1),
            "wake_ms": ("dailySleepDTO.sleepEndTimestampLocal", 1), "sleep_need_min": ("dailySleepDTO.sleepNeed.actual", 1),
            "sleep_spo2": ("dailySleepDTO.averageSpO2Value", 1), "sleep_resp": ("dailySleepDTO.averageRespirationValue", 1),
            "hrv": ("avgOvernightHrv", 1), "hrv_status": ("hrvStatus", 1), "sleep_rhr": ("restingHeartRate", 1),
            "skin_temp_dev": ("avgSkinTempDeviationC", 1)})
    if "readiness" not in rec:
        take(live(["training", "readiness", "x", "--date", d]), {
            "readiness": ("score", 1), "acute_load": ("acuteLoad", 1), "recovery_h": ("recoveryTime", 1 / 60)})
    # The daily-summary path is keyed by the account's display name.
    name = dig(live(["account", "social-profile"]), "displayName") or "x"
    if "bb_wake" not in rec:
        take(live(["wellness", "daily-summary", name, "--calendar-date", d]), {
            "bb_wake": ("bodyBatteryAtWakeTime", 1), "bb_charge": ("bodyBatteryDuringSleep", 1)})

    # `history` can archive yesterday's summary from an early-morning snapshot and
    # never refresh it, so yesterday's daytime totals always come from the live call.
    y = (report - dt.timedelta(days=1)).isoformat()
    summary = live(["wellness", "daily-summary", name, "--calendar-date", y])
    if summary and isinstance(summary.get("totalSteps"), (int, float)):
        yrec = days.setdefault(y, {})
        for key, path, scale in (("steps", "totalSteps", 1), ("step_goal", "dailyStepGoal", 1),
                                 ("stress", "averageStressLevel", 1), ("high_stress_min", "highStressDuration", 1 / 60),
                                 ("bb_low", "bodyBatteryLowestValue", 1), ("rhr", "restingHeartRate", 1),
                                 ("spo2", "averageSpo2", 1), ("resp", "avgWakingRespirationValue", 1)):
            v = summary.get(path)
            if isinstance(v, (int, float)) and v >= 0:
                yrec[key] = v * scale


# ---------------------------------------------------------------- metrics

# key, label, unit, better ('up'|'down'|None), anchor (0 = report morning, 1 = previous day),
# decimals, minimum change that counts, group
METRICS = [
    ("readiness", "Training readiness", "", "up", 0, 0, 5, "Recovery"),
    ("hrv", "Overnight HRV", "ms", "up", 0, 0, 2, "Recovery"),
    ("sleep_rhr", "Resting heart rate", "bpm", "down", 0, 0, 1.5, "Recovery"),
    ("bb_wake", "Body Battery at wake", "", "up", 0, 0, 5, "Recovery"),
    ("bb_charge", "Body Battery recharge", "", "up", 0, 0, 5, "Recovery"),
    ("sleep_h", "Sleep duration", "h", "up", 0, 1, 0.25, "Sleep"),
    ("sleep_score", "Sleep score", "", "up", 0, 0, 4, "Sleep"),
    ("deep_min", "Deep sleep", "min", "up", 0, 0, 8, "Sleep"),
    ("rem_min", "REM sleep", "min", "up", 0, 0, 10, "Sleep"),
    ("awake_min", "Awake in bed", "min", "down", 0, 0, 8, "Sleep"),
    ("sleep_stress", "Overnight stress", "", "down", 0, 0, 3, "Sleep"),
    ("sleep_resp", "Sleep breathing rate", "brpm", "down", 0, 1, 1, "Sleep"),
    ("stress", "Daytime stress (avg)", "", "down", 1, 0, 3, "Day"),
    ("high_stress_min", "High-stress time", "min", "down", 1, 0, 10, "Day"),
    ("bb_low", "Body Battery low", "", "up", 1, 0, 5, "Day"),
    ("steps", "Steps", "", "up", 1, 0, 1500, "Day"),
    ("train_load", "Training load", "", None, 1, 0, 30, "Day"),
    ("train_min", "Training time", "min", None, 1, 0, 20, "Day"),
]


def mean(xs):
    return st.mean(xs) if xs else None


def window(days, report, key, anchor, n):
    end = report - dt.timedelta(days=anchor)
    vals = []
    for i in range(1, n + 1):
        v = days.get((end - dt.timedelta(days=i)).isoformat(), {}).get(key)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def assess(days, report):
    out = []
    for key, label, unit, better, anchor, dec, min_abs, group in METRICS:
        day = (report - dt.timedelta(days=anchor)).isoformat()
        cur = days.get(day, {}).get(key)
        w7, w30 = window(days, report, key, anchor, 7), window(days, report, key, anchor, 30)
        a7, a30 = mean(w7), mean(w30)
        sd = st.pstdev(w30) if len(w30) >= 5 else None
        m = dict(key=key, label=label, unit=unit, better=better, day=day, dec=dec,
                 group=group, cur=cur, a7=a7, a30=a30, sd=sd, n30=len(w30))
        status = "nodata"
        if isinstance(cur, (int, float)) and a30 is not None:
            d30, d7 = cur - a30, (cur - a7) if a7 is not None else 0
            thr = max(min_abs, 0.5 * sd if sd else 0)
            if better is None:
                status = "info"
            else:
                sign = 1 if better == "up" else -1
                g30, g7 = sign * d30, sign * d7
                if g30 <= -max(min_abs, sd or 0) and g7 < 0:
                    status = "alert"
                elif g30 <= -thr or (g7 <= -thr and g30 <= -thr / 2):
                    status = "watch"
                elif g30 >= thr:
                    status = "better"
                else:
                    status = "steady"
            m.update(d30=d30, d7=d7,
                     p30=(d30 / a30 * 100) if a30 else None,
                     p7=(d7 / a7 * 100) if a7 else None)
        m["status"] = status
        out.append(m)
    return out


def fmt(v, dec=0, unit=""):
    if not isinstance(v, (int, float)):
        return "–"
    if abs(v) >= 1000:
        s = f"{v:,.0f}"
    else:
        s = f"{v:.{dec}f}"
    return f"{s} {unit}".strip() if unit and unit not in ("",) else s


def hhmm(ms):
    if not isinstance(ms, (int, float)):
        return None
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return t.hour * 60 + t.minute


def clock(mins):
    mins = int(round(mins)) % (24 * 60)
    h, m = divmod(mins, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


# ---------------------------------------------------------------- recommendations

def recommendations(ms, days, report):
    by = {m["key"]: m for m in ms}
    bad = {k for k, m in by.items() if m["status"] in ("watch", "alert")}
    today = days.get(report.isoformat(), {})
    yday = days.get((report - dt.timedelta(days=1)).isoformat(), {})
    recs = []

    def add(priority, title, body, keys):
        recs.append(dict(priority=priority, title=title, body=body, keys=keys))

    # Typical wake time and sleep need over the last 30 nights.
    wakes, beds = [], []
    for i in range(0, 30):
        d = days.get((report - dt.timedelta(days=i)).isoformat(), {})
        w, b = hhmm(d.get("wake_ms")), hhmm(d.get("bed_ms"))
        if w is not None:
            wakes.append(w)
        if b is not None:
            beds.append(b if b > 12 * 60 else b + 24 * 60)
    need_h = (today.get("sleep_need_min") or 0) / 60 or (by["sleep_h"]["a30"] or 8)
    target_h = max(need_h, by["sleep_h"]["a30"] or 0)
    wake = st.median(wakes) if wakes else 6 * 60
    lights_out = wake - target_h * 60 - 15
    usual_bed = st.median(beds) if beds else None

    readiness = by["readiness"]["cur"]
    hrv_bad = "hrv" in bad
    rhr_bad = "sleep_rhr" in bad
    recovery_flags = sum(k in bad for k in ("readiness", "hrv", "sleep_rhr", "bb_wake"))

    if recovery_flags >= 2 or (isinstance(readiness, (int, float)) and readiness < 40):
        lvl = ("a rest day or 30–40 min of easy zone 1–2" if (readiness or 0) < 30
               else "an easy session at zone 2 or below, under 60 min")
        add(1, "Make today an easy day",
            f"Readiness is {fmt(readiness)} and {recovery_flags} recovery markers are below your averages. "
            f"Swap any planned threshold or interval work for {lvl}. Move the hard session to the first morning readiness is back above 60.",
            ["readiness", "hrv", "sleep_rhr", "bb_wake"])
    elif hrv_bad:
        m = by["hrv"]
        add(2, "Hold intensity until HRV recovers",
            f"HRV is {fmt(m['cur'])} ms vs {fmt(m['a30'])} ms (30-day). If you train today, cap it at moderate effort "
            f"and skip a second session. Drink 500 ml of water with electrolytes this morning.", ["hrv"])

    if rhr_bad:
        m = by["sleep_rhr"]
        extra = ""
        st_dev = today.get("skin_temp_dev")
        if isinstance(st_dev, (int, float)) and st_dev >= 0.5:
            extra = f" Skin temperature is also up {st_dev:.1f} °C, a common early sign of illness, so take a full rest day if you feel off."
        add(2, "Watch for fatigue or illness",
            f"Resting heart rate is {fmt(m['cur'])} bpm, {fmt(m['d30'], 0)} above your 30-day average. "
            f"Hydrate early, skip alcohol tonight, and check again tomorrow; two high mornings in a row means rest.{extra}",
            ["sleep_rhr"])

    if "sleep_h" in bad or "sleep_score" in bad:
        m = by["sleep_h"]
        late = ""
        if usual_bed is not None and isinstance(today.get("bed_ms"), (int, float)):
            last_bed = hhmm(today["bed_ms"])
            last_bed = last_bed if last_bed > 12 * 60 else last_bed + 24 * 60
            if last_bed - usual_bed >= 30:
                late = f" You fell asleep at {clock(last_bed)}, {int(last_bed - usual_bed)} min later than usual."
        add(1 if "sleep_h" in bad else 2, "Get to bed earlier tonight",
            f"You slept {fmt(m['cur'], 1)} h vs {fmt(m['a30'], 1)} h on average.{late} "
            f"To get {target_h:.1f} h before your usual {clock(wake)} wake-up, have lights out by {clock(lights_out)}. "
            f"Start winding down (screens off, dim lights) 30 min before that.", ["sleep_h", "sleep_score"])

    if "deep_min" in bad:
        m = by["deep_min"]
        late_act = ""
        for a in yday.get("activities", []):
            try:
                end = dt.datetime.fromisoformat(str(a["start"]).replace(" ", "T")) + dt.timedelta(minutes=a["dur_min"] or 0)
                end_m = end.hour * 60 + end.minute
                if usual_bed and usual_bed - end_m < 180 and (a["load"] or 0) > 60:
                    late_act = f" Yesterday's {a['name']} finished at {clock(end_m)}, within 3 h of bedtime; schedule hard sessions earlier."
            except (ValueError, TypeError, KeyError):
                pass
        add(2, "Protect deep sleep",
            f"Deep sleep was {fmt(m['cur'])} min vs {fmt(m['a30'])} min (30-day).{late_act} "
            f"No alcohol and no big meal in the 3 hours before bed, and keep the bedroom cool (60–67 °F).", ["deep_min"])

    if "rem_min" in bad:
        m = by["rem_min"]
        add(3, "Keep your wake time steady for REM",
            f"REM was {fmt(m['cur'])} min vs {fmt(m['a30'])} min. Most REM comes in the last 2 hours of sleep, "
            f"so avoid cutting the night short with an early alarm and keep a consistent {clock(wake)} wake time.", ["rem_min"])

    if "awake_min" in bad:
        m = by["awake_min"]
        add(3, "Cut night-time wake-ups",
            f"You were awake {fmt(m['cur'])} min in bed vs {fmt(m['a30'])} min usually. "
            f"No caffeine after 1 PM, limit fluids in the last 90 minutes before bed, and keep the room dark.", ["awake_min"])

    if "bb_wake" in bad or "bb_charge" in bad:
        m = by["bb_wake"]
        add(3, "Recharge Body Battery",
            f"Body Battery at wake was {fmt(m['cur'])} vs {fmt(m['a30'])} usually. Plan a 10–20 min low-stress break "
            f"this afternoon (walk outside or rest), and keep the evening calm so tonight's recharge is fuller.", ["bb_wake", "bb_charge"])

    if "stress" in bad or "high_stress_min" in bad:
        m, h = by["stress"], by["high_stress_min"]
        add(2, "Bring daytime stress down",
            f"Yesterday's average stress was {fmt(m['cur'])} vs {fmt(m['a30'])}, with {fmt(h['cur'])} min in high stress "
            f"(usually {fmt(h['a30'])}). Block two 5-minute breathing breaks (Breathwork on the watch) around your busiest "
            f"meetings, and take a 10-minute walk after lunch.", ["stress", "high_stress_min"])

    if "bb_low" in bad:
        m = by["bb_low"]
        add(3, "Avoid running the battery flat",
            f"Body Battery dropped to {fmt(m['cur'])} yesterday vs a usual low of {fmt(m['a30'])}. "
            f"Space hard sessions and demanding work so they don't land on the same afternoon.", ["bb_low"])

    if "steps" in bad:
        m = by["steps"]
        gap = (m["a7"] or m["a30"] or 0) - (m["cur"] or 0)
        walk = max(10, int(round(gap / 100 / 5) * 5))
        add(3, "Add a walk",
            f"Yesterday was {fmt(m['cur'])} steps vs {fmt(m['a7'])} over the last week. "
            f"A {walk}-minute walk (about {gap:,.0f} steps) closes the gap; on an easy day it doubles as recovery.", ["steps"])

    if "sleep_resp" in bad:
        m = by["sleep_resp"]
        both = " Resting heart rate is up too, so treat today as a recovery day." if rhr_bad else ""
        add(3, "Keep an eye on breathing rate",
            f"Breathing rate during sleep was {fmt(m['cur'], 1)} vs {fmt(m['a30'], 1)} brpm. A rise that lasts several nights, "
            f"together with a higher resting heart rate, is often an early sign of illness or overreaching.{both}", ["sleep_resp"])

    load = by["train_load"]
    if isinstance(load["cur"], (int, float)) and load["a30"] and load["cur"] > 2.0 * load["a30"] and load["cur"] > 150:
        add(3, "Follow a big day with an easy one",
            f"Yesterday's training load was {fmt(load['cur'])}, more than double your daily average of {fmt(load['a30'])}. "
            f"Plan today as recovery or easy aerobic work regardless of how you feel.", ["train_load"])

    recs.sort(key=lambda r: r["priority"])
    return recs[:6], dict(lights_out=clock(lights_out), wake=clock(wake), target_h=target_h)


def headline(ms, recs):
    by = {m["key"]: m for m in ms}
    r = by["readiness"]["cur"]
    alerts = [m for m in ms if m["status"] == "alert"]
    watch = [m for m in ms if m["status"] == "watch"]
    # Only recovery and sleep metrics decide the day's state; steps or stress alone don't.
    rec_alerts = [m for m in alerts if m["group"] in ("Recovery", "Sleep")]
    rec_watch = [m for m in watch if m["group"] in ("Recovery", "Sleep")]
    better = [m for m in ms if m["status"] == "better"]
    if not isinstance(r, (int, float)) and not isinstance(by["sleep_h"]["cur"], (int, float)):
        state, tone = "Waiting for sync", "warning"
    elif (isinstance(r, (int, float)) and r < 40) or sum(m["group"] == "Recovery" for m in rec_alerts) >= 2:
        state, tone = "Recover", "critical"
    elif (isinstance(r, (int, float)) and r < 65) or rec_alerts or len(rec_watch) >= 3:
        state, tone = "Train with care", "warning"
    else:
        state, tone = "Ready to train", "good"
    parts = []
    if isinstance(r, (int, float)):
        parts.append(f"Readiness {r:.0f}")
    s = by["sleep_h"]
    if isinstance(s["cur"], (int, float)):
        parts.append(f"{s['cur']:.1f} h sleep")
    h = by["hrv"]
    if isinstance(h["cur"], (int, float)):
        parts.append(f"HRV {h['cur']:.0f} ms")
    summary = " · ".join(parts)
    bullets = []
    if alerts or watch:
        names = ", ".join(m["label"] for m in (alerts + watch)[:4])
        bullets.append(f"Moving the wrong way: {names}.")
    if better:
        names = ", ".join(m["label"] for m in better[:4])
        bullets.append(f"Better than your 30-day average: {names}.")
    if recs:
        title = recs[0]["title"]
        bullets.append(f"Top action: {title[0].lower() + title[1:]}.")
    if not (alerts or watch):
        bullets.append("Nothing is trending the wrong way. Keep the current routine.")
    return state, tone, summary, bullets


# ---------------------------------------------------------------- SVG charts

def esc(s):
    return html.escape(str(s), quote=True)


def nice_range(lo, hi, ticks=4):
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / ticks))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= ticks:
            step *= m
            break
    lo2 = math.floor(lo / step) * step
    hi2 = math.ceil(hi / step) * step
    vals, v = [], lo2
    while v <= hi2 + step / 1000:
        vals.append(round(v, 6))
        v += step
    return lo2, hi2, vals


def line_chart(days, report, key, label, unit, dec, anchor, a30):
    W, H, L, R, T, B = 320, 150, 34, 10, 12, 22
    end = report - dt.timedelta(days=anchor)
    dates = [end - dt.timedelta(days=i) for i in range(29, -1, -1)]
    vals = [days.get(d.isoformat(), {}).get(key) for d in dates]
    pts = [(i, v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
    if len(pts) < 3:
        return '<p class="muted">Not enough data yet.</p>'
    roll = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - 6): i + 1] if isinstance(v, (int, float))]
        roll.append(mean(w) if len(w) >= 3 else None)
    ys = [v for _, v in pts] + [v for v in roll if v is not None] + ([a30] if a30 else [])
    lo, hi, ticks = nice_range(min(ys), max(ys))
    x = lambda i: L + i * (W - L - R) / 29
    y = lambda v: T + (hi - v) * (H - T - B) / (hi - lo)
    g = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{esc(label)}, last 30 days">']
    for t in ticks:
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                 f'<text x="{L-6}" y="{y(t)+3.5:.1f}" class="tick" text-anchor="end">{fmt(t, 0 if t == int(t) else 1)}</text>')
    for i in (0, 14, 29):
        anchor_txt = "start" if i == 0 else "end" if i == 29 else "middle"
        g.append(f'<text x="{x(i):.1f}" y="{H-6}" class="tick" text-anchor="{anchor_txt}">{dates[i].strftime("%b %-d")}</text>')
    if a30:
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{y(a30):.1f}" y2="{y(a30):.1f}" class="ref"/>')
    path = " ".join(f"{'M' if j == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for j, (i, v) in enumerate(pts))
    g.append(f'<path d="{path}" class="s-daily"/>')
    rpts = [(i, v) for i, v in enumerate(roll) if v is not None]
    rpath = " ".join(f"{'M' if j == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for j, (i, v) in enumerate(rpts))
    g.append(f'<path d="{rpath}" class="s-roll"/>')
    for i, v in pts:
        tip = f"{dates[i].strftime('%a %b %-d')}: {fmt(v, dec, unit)}"
        if roll[i] is not None:
            tip += f" · 7-day {fmt(roll[i], dec, unit)}"
        last = i == 29
        g.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="{4.5 if last else 2.2}" class="{"pt-last" if last else "pt"}"/>')
        g.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="9" class="hit" data-tip="{esc(tip)}"/>')
    g.append("</svg>")
    return "".join(g)


def deviation_chart(ms):
    rows = [m for m in ms if m["better"] and m.get("p30") is not None]
    W, rowh, L, R = 520, 26, 170, 54
    H = 30 + rowh * len(rows)
    lim = max([30] + [min(80, abs(m["p30"])) for m in rows] + [min(80, abs(m.get("p7") or 0)) for m in rows])
    lim = math.ceil(lim / 10) * 10
    cx = L + (W - L - R) / 2
    sx = lambda p: cx + max(-lim, min(lim, p)) / lim * (W - L - R) / 2
    g = [f'<svg viewBox="0 0 {W} {H}" class="chart dev" role="img" aria-label="Last 24 hours versus averages, percent difference">']
    for p in (-lim, -lim / 2, 0, lim / 2, lim):
        g.append(f'<line x1="{sx(p):.1f}" x2="{sx(p):.1f}" y1="18" y2="{H-6}" class="{"axis0" if p == 0 else "grid"}"/>'
                 f'<text x="{sx(p):.1f}" y="12" class="tick" text-anchor="middle">{p:+.0f}%</text>')
    for j, m in enumerate(rows):
        yc = 30 + j * rowh + rowh / 2 - 4
        sign = 1 if m["better"] == "up" else -1
        good = sign * m["p30"] >= 0
        cls = {"alert": "bar-bad", "watch": "bar-bad", "better": "bar-good"}.get(m["status"], "bar-neutral")
        x0, x1 = sorted((cx, sx(m["p30"])))
        w = max(2, x1 - x0)
        g.append(f'<text x="{L-10}" y="{yc+4}" class="lab" text-anchor="end">{esc(m["label"])}</text>')
        g.append(f'<rect x="{x0:.1f}" y="{yc-7}" width="{w:.1f}" height="14" rx="3" class="{cls}"/>')
        if m.get("p7") is not None:
            g.append(f'<line x1="{sx(m["p7"]):.1f}" x2="{sx(m["p7"]):.1f}" y1="{yc-9}" y2="{yc+9}" class="tick7"/>')
        word = "better" if good else "worse"
        g.append(f'<text x="{W-R+6}" y="{yc+4}" class="val">{m["p30"]:+.0f}%</text>')
        tip = (f"{m['label']}: {fmt(m['cur'], m['dec'], m['unit'])} · 30-day {fmt(m['a30'], m['dec'], m['unit'])} "
               f"({m['p30']:+.0f}%, {word}) · 7-day {fmt(m['a7'], m['dec'], m['unit'])}")
        g.append(f'<rect x="0" y="{yc-12}" width="{W}" height="{rowh}" class="hit" data-tip="{esc(tip)}"/>')
    g.append("</svg>")
    return "".join(g)


def stage_chart(days, report):
    stages = [("deep_min", "Deep", "c1"), ("light_min", "Light", "c2"), ("rem_min", "REM", "c3"), ("awake_min", "Awake", "c4")]
    dates = [report - dt.timedelta(days=i) for i in range(13, -1, -1)]
    W, H, L, R, T, B = 520, 190, 34, 8, 10, 22
    tot = [sum(days.get(d.isoformat(), {}).get(k) or 0 for k, _, _ in stages) / 60 for d in dates]
    lo, hi, ticks = nice_range(0, max(tot + [8]))
    bw = (W - L - R) / len(dates)
    y = lambda v: T + (hi - v) * (H - T - B) / (hi - lo)
    g = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Sleep stages, last 14 nights">']
    for t in ticks:
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                 f'<text x="{L-6}" y="{y(t)+3.5:.1f}" class="tick" text-anchor="end">{t:g}h</text>')
    for i, d in enumerate(dates):
        rec = days.get(d.isoformat(), {})
        acc = 0.0
        x0 = L + i * bw + 3
        parts = []
        for k, name, c in stages:
            v = (rec.get(k) or 0) / 60
            if v <= 0:
                continue
            y1, y0 = y(acc + v), y(acc)
            g.append(f'<rect x="{x0:.1f}" y="{y1+1:.1f}" width="{bw-6:.1f}" height="{max(0.5, y0-y1-2):.1f}" rx="2" class="{c}"/>')
            parts.append(f"{name} {v*60:.0f} min")
            acc += v
        if i % 2 == 1 or i == len(dates) - 1:
            g.append(f'<text x="{x0+(bw-6)/2:.1f}" y="{H-6}" class="tick" text-anchor="middle">{d.strftime("%-m/%-d")}</text>')
        if parts:
            tip = f"{d.strftime('%a %b %-d')}: {acc:.1f} h · " + " · ".join(parts)
            g.append(f'<rect x="{x0:.1f}" y="{T}" width="{bw-6:.1f}" height="{H-T-B}" class="hit" data-tip="{esc(tip)}"/>')
    g.append("</svg>")
    return "".join(g)


def load_chart(days, report):
    end = report - dt.timedelta(days=1)
    dates = [end - dt.timedelta(days=i) for i in range(29, -1, -1)]
    vals = [days.get(d.isoformat(), {}).get("train_load") or 0 for d in dates]
    W, H, L, R, T, B = 520, 160, 34, 8, 10, 22
    lo, hi, ticks = nice_range(0, max(vals + [100]))
    bw = (W - L - R) / len(dates)
    y = lambda v: T + (hi - v) * (H - T - B) / (hi - lo)
    g = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Daily training load, last 30 days">']
    for t in ticks:
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                 f'<text x="{L-6}" y="{y(t)+3.5:.1f}" class="tick" text-anchor="end">{t:g}</text>')
    for i, (d, v) in enumerate(zip(dates, vals)):
        x0 = L + i * bw + 1.5
        acts = days.get(d.isoformat(), {}).get("activities", [])
        if v > 0:
            g.append(f'<rect x="{x0:.1f}" y="{y(v):.1f}" width="{bw-3:.1f}" height="{y(0)-y(v):.1f}" rx="2" class="{"c1-strong" if i == 29 else "c1"}"/>')
        if i in (0, 10, 20, 29):
            g.append(f'<text x="{x0+(bw-3)/2:.1f}" y="{H-6}" class="tick" text-anchor="middle">{d.strftime("%b %-d")}</text>')
        names = ", ".join(f"{a['name']} ({(a['load'] or 0):.0f})" for a in acts) or "Rest"
        g.append(f'<rect x="{x0:.1f}" y="{T}" width="{bw-3:.1f}" height="{H-T-B}" class="hit" data-tip="{esc(d.strftime("%a %b %-d") + ": load " + format(v, ".0f") + " · " + names)}"/>')
    g.append(f'<line x1="{L}" x2="{W-R}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="axis0"/>')
    g.append("</svg>")
    return "".join(g)


# ---------------------------------------------------------------- HTML

STATUS_TEXT = {"alert": ("▼", "Worse"), "watch": ("▽", "Slipping"), "better": ("▲", "Better"),
               "steady": ("●", "Steady"), "info": ("·", "Context"), "nodata": ("–", "No data")}

CSS = """
/* Layout: one reading column; summary band, then deviation chart, actions, trend grid, detail table. */
:root{--bg:#f6f7f9;--panel:#ffffff;--ink:#14171c;--ink2:#4b5260;--muted:#7a8191;--line:#e3e6eb;--grid:#eceef2;
--accent:#2a78d6;--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--roll:#eb6834;
--good:#0ca30c;--warn:#fab219;--bad:#d03b3b;--good-ink:#0a7a0a;--bad-ink:#b42f2f;--warn-ink:#8a5d00;
--neutral:#c5c9d1;--display:"Archivo",system-ui,sans-serif;--body:"Archivo",system-ui,sans-serif;--mono:"JetBrains Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121418;--panel:#1a1d22;--ink:#f2f3f5;--ink2:#c0c5ce;--muted:#8d94a1;--line:#2b2f37;--grid:#24272e;
--accent:#3987e5;--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--roll:#d95926;--good-ink:#4fd04f;--bad-ink:#f07a7a;--warn-ink:#fab219;--neutral:#4a4f59;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121418;--panel:#1a1d22;--ink:#f2f3f5;--ink2:#c0c5ce;--muted:#8d94a1;--line:#2b2f37;--grid:#24272e;
--accent:#3987e5;--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--roll:#d95926;--good-ink:#4fd04f;--bad-ink:#f07a7a;--warn-ink:#fab219;--neutral:#4a4f59;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.5;margin:0}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:28px 48px;display:flex;flex-direction:column;gap:28px}
h1,h2,h3{font-family:var(--display);text-wrap:balance;margin:0}
h1{font-size:clamp(26px,4vw,34px);font-weight:700;letter-spacing:-.01em}
h2{font-size:18px;font-weight:650}
h3{font-size:14px;font-weight:600}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
.muted{color:var(--muted)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
header{display:flex;flex-direction:column;gap:6px}
.band{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,2fr);gap:20px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}
@media (max-width:760px){.band{grid-template-columns:1fr}}
.state{display:flex;flex-direction:column;gap:8px;border-left:6px solid var(--tone);padding-left:16px}
.state .label{font-family:var(--display);font-size:28px;font-weight:700;line-height:1.1}
.state.good{--tone:var(--good)}.state.warning{--tone:var(--warn)}.state.critical{--tone:var(--bad)}
.band ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px;color:var(--ink2)}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
@media (max-width:760px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{display:flex;flex-direction:column;gap:2px;padding:14px;border-radius:10px;background:var(--panel);border:1px solid var(--line)}
.tile .v{font-family:var(--mono);font-size:24px;font-weight:600}
.tile .cmp{font-size:12px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;border:1px solid currentColor;width:fit-content}
.pill.alert,.pill.watch{color:var(--bad-ink)}.pill.better{color:var(--good-ink)}.pill.steady,.pill.info,.pill.nodata{color:var(--muted)}
section{display:flex;flex-direction:column;gap:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--ink2)}
.legend i{display:inline-block;width:14px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.legend i.line{height:2px;border-radius:0;vertical-align:3px}
.recs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (max-width:760px){.recs{grid-template-columns:1fr}}
.rec{display:flex;flex-direction:column;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.rec .p{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.rec .p1{color:var(--bad-ink)}.rec .p2{color:var(--warn-ink)}.rec .p3{color:var(--muted)}
.rec p{margin:0;color:var(--ink2)}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:900px){.grid3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:560px){.grid3{grid-template-columns:1fr}}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.chart{width:100%;height:auto;display:block;overflow:visible}
.chart.dev{max-width:680px}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .axis0{stroke:var(--muted);stroke-width:1}
.chart .ref{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3}
.chart .tick{fill:var(--muted);font-size:10px;font-family:var(--mono)}
.chart .lab{fill:var(--ink2);font-size:12px}
.chart .val{fill:var(--ink);font-size:12px;font-family:var(--mono)}
.chart .s-daily{fill:none;stroke:var(--c1);stroke-width:1.5;opacity:.55}
.chart .s-roll{fill:none;stroke:var(--roll);stroke-width:2}
.chart .pt{fill:var(--c1)}
.chart .pt-last{fill:var(--c1);stroke:var(--panel);stroke-width:2}
.chart .hit{fill:transparent;cursor:crosshair}
.chart .c1{fill:var(--c1)}.chart .c1-strong{fill:var(--c1);stroke:var(--ink);stroke-width:1}
.chart .c2{fill:var(--c2)}.chart .c3{fill:var(--c3)}.chart .c4{fill:var(--c4)}
.chart .bar-bad{fill:var(--bad)}.chart .bar-good{fill:var(--good)}.chart .bar-neutral{fill:var(--neutral)}
.chart .tick7{stroke:var(--ink);stroke-width:2}
.mini{display:flex;flex-direction:column;gap:6px}
.mini .hd{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.mini .hd .num{font-size:13px;color:var(--ink2)}
.tbl{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:620px}
th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
td.num{font-family:var(--mono)}
tr.grp td{font-weight:700;color:var(--muted);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding-top:14px}
.acts{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}
.acts li{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding-block:6px}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);font-size:12px;padding:6px 9px;border-radius:6px;max-width:280px;z-index:10;font-family:var(--body)}
footer{font-size:12px;color:var(--muted)}
"""

JS = """
(function(){var t=document.getElementById('tip');
document.addEventListener('pointermove',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
if(!el){t.hidden=true;return}t.textContent=el.getAttribute('data-tip');t.hidden=false;
var x=e.clientX+14,y=e.clientY+14,w=t.offsetWidth,h=t.offsetHeight;
if(x+w>innerWidth-8)x=e.clientX-w-14;if(y+h>innerHeight-8)y=e.clientY-h-14;t.style.left=x+'px';t.style.top=y+'px'});
document.addEventListener('pointerleave',function(){t.hidden=true})})();
"""


def running_zones():
    """Running heart-rate zone floors (z1..z5) from Garmin, or None."""
    try:
        rows = sql("SELECT data FROM resources WHERE resource_type='hr_zone_config'")
        cfg = json.loads(rows[0]["data"]) if rows else {}
    except (RuntimeError, ValueError, TypeError, KeyError):
        return None
    zones = cfg.get("heartRateZones", []) if isinstance(cfg, dict) else []
    pick = next((z for z in zones if z.get("sport") == "RUNNING"), None) or next(
        (z for z in zones if z.get("sport") == "DEFAULT"), None)
    if not pick:
        return None
    floors = [pick.get(f"zone{i}Floor") for i in range(1, 6)]
    return floors if all(isinstance(f, (int, float)) for f in floors) else None


def workout(ms, days, report, zones):
    """Today's session, chosen from walking, strength and running only."""
    by = {m["key"]: m for m in ms}
    r = by["readiness"]["cur"]
    if not isinstance(r, (int, float)):
        return None
    bad = {k for k, m in by.items() if m["status"] in ("watch", "alert")}
    flags = sum(k in bad for k in ("hrv", "sleep_rhr", "bb_wake", "sleep_resp"))
    recent = []
    for i in (1, 2):
        recent += days.get((report - dt.timedelta(days=i)).isoformat(), {}).get("activities", [])
    # Garmin labels most strength sessions "anaerobic", so only load marks strength as hard.
    hard_48h = [a for a in recent if (a.get("load") or 0) >= 180
                or (a.get("te_label") in HARD_LABELS and a.get("type") != "strength_training")]
    yday = days.get((report - dt.timedelta(days=1)).isoformat(), {}).get("activities", [])
    strength_yday = any(a.get("type") == "strength_training" and (a.get("dur_min") or 0) >= 15 for a in yday)
    z1, z2, z3, z4, z5 = zones or (None,) * 5
    done = [a for a in days.get(report.isoformat(), {}).get("activities", [])
            if (a.get("load") or 0) >= 100 or (a.get("dur_min") or 0) >= 45]
    if done:
        main = max(done, key=lambda a: a.get("load") or 0)
        mins = sum(a.get("dur_min") or 0 for a in done)
        load = sum(a.get("load") or 0 for a in done)
        return dict(level="Done", why=f"Today's main session is already in: {main['name']} "
                    f"({mins:.0f} min, load {load:.0f}). Keep the rest of the day easy.",
                    options=[dict(activity="Walking", plan="20–30 min easy walk later in the day to loosen up."),
                             dict(activity="Strength", plan="Optional 10–15 min core and mobility. No heavy lifting on top of today's session."),
                             dict(activity="Running", plan="Done for today.")])
    hr = lambda lo, hi: f" (heart rate {lo:.0f}–{hi:.0f} bpm)" if lo and hi else ""
    easy_cap = f" (heart rate under {z2:.0f} bpm)" if z2 else ""

    if r < 30 or flags >= 3:
        level = "Rest"
        why = f"Readiness {r:.0f} with {flags} recovery markers down."
        opts = [("Walking", "30–45 min easy walk, ideally outdoors, plus 10 min of gentle mobility."),
                ("Strength", "Skip today."), ("Running", "Skip today.")]
    elif r < 60 or hard_48h or flags >= 2:
        level = "Easy"
        reason = []
        if r < 60:
            reason.append(f"readiness {r:.0f}")
        if hard_48h:
            reason.append(f"a hard session in the last 48 h ({hard_48h[-1]['name']})")
        if flags >= 2:
            reason.append(f"{flags} recovery markers down")
        why = "Easy day because of " + " and ".join(reason) + "."
        opts = [("Walking", "45–60 min brisk walk. The best choice if your legs feel heavy."),
                ("Running", f"30–40 min easy run on flat ground{easy_cap}. Walk the hills."),
                ("Strength", "20–30 min upper body and core only, light weights." if not strength_yday
                 else "Skip; you did strength yesterday. 10–15 min of mobility instead.")]
    elif r < 75:
        level = "Moderate"
        why = f"Readiness {r:.0f}: good for steady aerobic work, not intervals."
        opts = [("Running", f"45–60 min steady run in zone 2{hr(z2, z3 - 1 if z3 else None)}."),
                ("Strength", "30–40 min full-body strength, moderate weights." if not strength_yday
                 else "Light core and mobility only; you did strength yesterday."),
                ("Walking", "30 min easy walk later in the day for recovery and steps.")]
    else:
        level = "Quality"
        why = f"Readiness {r:.0f} and no hard session in the last 48 h: a good day for your key workout."
        opts = [("Running", f"Threshold run: 15 min easy, 3 × 8 min in zone 4{hr(z4, z5 - 1 if z5 else None)} "
                 f"with 2 min easy jogs, 10 min cool-down. Or a long run in zone 2{hr(z2, z3 - 1 if z3 else None)}."),
                ("Strength", "30–40 min full-body strength, after the run or later in the day, not before it."),
                ("Walking", "20–30 min easy walk in the evening to loosen up.")]
    return dict(level=level, why=why, options=[dict(activity=a, plan=t) for a, t in opts])


def render(report, days, ms, recs, plan, generated, wo=None):
    by = {m["key"]: m for m in ms}
    state, tone, summary, bullets = headline(ms, recs)
    yday = report - dt.timedelta(days=1)

    def tile(key):
        m = by[key]
        icon, word = STATUS_TEXT[m["status"]]
        return (f'<div class="tile"><span class="eyebrow">{esc(m["label"])}</span>'
                f'<span class="v">{fmt(m["cur"], m["dec"])}<small class="muted"> {esc(m["unit"])}</small></span>'
                f'<span class="cmp num">7d {fmt(m["a7"], m["dec"])} · 30d {fmt(m["a30"], m["dec"])}</span>'
                f'<span class="pill {m["status"]}">{icon} {word}</span></div>')

    rec_html = "".join(
        f'<div class="rec"><span class="p p{r["priority"]}">{["", "Do today", "Important", "Worth doing"][r["priority"]]}</span>'
        f'<h3>{esc(r["title"])}</h3><p>{esc(r["body"])}</p></div>' for r in recs
    ) or '<div class="rec"><h3>No corrections needed</h3><p>Every tracked metric is at or better than your averages. Keep the current routine.</p></div>'

    trend_keys = [("readiness", 0), ("hrv", 0), ("sleep_rhr", 0), ("sleep_h", 0), ("sleep_score", 0),
                  ("bb_wake", 0), ("stress", 1), ("steps", 1), ("deep_min", 0)]
    minis = []
    for key, anchor in trend_keys:
        m = by[key]
        minis.append(f'<div class="panel mini"><div class="hd"><h3>{esc(m["label"])}</h3>'
                     f'<span class="num">{fmt(m["cur"], m["dec"], m["unit"])}</span></div>'
                     f'{line_chart(days, report, key, m["label"], m["unit"], m["dec"], anchor, m["a30"])}</div>')

    rows, grp = [], None
    for m in ms:
        if m["group"] != grp:
            grp = m["group"]
            when = "last night / this morning" if grp in ("Recovery", "Sleep") else yday.strftime("%a %b %-d")
            rows.append(f'<tr class="grp"><td colspan="6">{grp} <span class="muted">({when})</span></td></tr>')
        icon, word = STATUS_TEXT[m["status"]]
        d7 = f'{m["d7"]:+.{m["dec"]}f}' if m.get("d7") is not None else "–"
        rows.append(f'<tr><td>{esc(m["label"])}{(" (" + esc(m["unit"]) + ")") if m["unit"] else ""}</td>'
                    f'<td class="num">{fmt(m["cur"], m["dec"])}</td><td class="num">{fmt(m["a7"], m["dec"])}</td>'
                    f'<td class="num">{fmt(m["a30"], m["dec"])}</td><td class="num">{d7}</td>'
                    f'<td><span class="pill {m["status"]}">{icon} {word}</span></td></tr>')

    pending = "" if isinstance(by["sleep_h"]["cur"], (int, float)) else (
        '<div class="panel"><b>Last night\'s sleep hasn\'t synced yet.</b> <span class="muted">Sleep, HRV and readiness '
        'fill in after your watch syncs with Garmin Connect. Ask Claude to rerun the report after you wake.</span></div>')
    workout_html = ""
    if wo:
        workout_html = (f'<section><h2>Today\'s workout <span class="pill steady">{esc(wo["level"])}</span></h2>'
                        f'<div class="muted">{esc(wo["why"])}</div><div class="recs">'
                        + "".join(f'<div class="rec"><span class="p p3">{esc(o["activity"])}</span><p>{esc(o["plan"])}</p></div>'
                                  for o in wo["options"]) + "</div></section>")
    acts = days.get(yday.isoformat(), {}).get("activities", [])
    act_html = "".join(
        f'<li><span>{esc(a["name"])}</span><span class="num muted">{(a["dur_min"] or 0):.0f} min · load {(a["load"] or 0):.0f}'
        f'{(" · avg HR " + format(a["avg_hr"], ".0f")) if a.get("avg_hr") else ""}</span></li>' for a in acts
    ) or '<li><span>Rest day</span><span class="muted">No activities recorded</span></li>'

    return f"""<title>Garmin Morning Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header><span class="eyebrow">Garmin morning report · {report.strftime('%A, %B %-d, %Y')}</span>
<h1>Last 24 hours vs your 7- and 30-day averages</h1>
<span class="muted">Sleep and recovery from the night ending {report.strftime('%b %-d')}; daytime totals from {yday.strftime('%A %b %-d')}.</span></header>

{pending}
<div class="band"><div class="state {tone}"><span class="eyebrow">Today</span><span class="label">{esc(state)}</span>
<span class="num muted">{esc(summary)}</span></div>
<ul>{''.join(f'<li>{esc(b)}</li>' for b in bullets)}</ul></div>

<div class="tiles">{tile('readiness')}{tile('sleep_h')}{tile('hrv')}{tile('sleep_rhr')}</div>

{workout_html}
<section><h2>What to do today</h2>
<div class="muted">Tonight's target: lights out by <b>{plan['lights_out']}</b> for {plan['target_h']:.1f} h before a {plan['wake']} wake-up.</div>
<div class="recs">{rec_html}</div></section>

<section><h2>How the last 24 hours compare</h2>
<div class="panel"><div class="legend"><span><i style="background:var(--bad)"></i>Worse than 30-day avg</span>
<span><i style="background:var(--good)"></i>Better</span><span><i style="background:var(--neutral)"></i>Within normal range</span>
<span><i class="line" style="background:var(--ink)"></i>Difference vs 7-day avg</span></div>
<div class="tbl">{deviation_chart(ms)}</div>
<p class="muted" style="margin:0;font-size:12px">Bar length is the % difference from your 30-day average, capped at ±80%. Color reflects direction: for resting heart rate and stress, lower is better.</p></div></section>

<section><h2>30-day trends</h2>
<div class="legend"><span><i class="line" style="background:var(--c1)"></i>Daily value</span>
<span><i class="line" style="background:var(--roll)"></i>7-day rolling average</span>
<span><i class="line" style="background:var(--muted)"></i>30-day average (dashed)</span><span>Large dot = latest</span></div>
<div class="grid3">{''.join(minis)}</div></section>

<section><div class="grid2">
<div class="panel"><h3>Sleep stages · last 14 nights</h3>
<div class="legend" style="margin-block:8px"><span><i style="background:var(--c1)"></i>Deep</span><span><i style="background:var(--c2)"></i>Light</span>
<span><i style="background:var(--c3)"></i>REM</span><span><i style="background:var(--c4)"></i>Awake</span></div>{stage_chart(days, report)}</div>
<div class="panel"><h3>Training load · last 30 days</h3><div style="margin-block:8px" class="legend"><span>Outlined bar = yesterday</span></div>{load_chart(days, report)}
<h3 style="margin-top:12px">Yesterday's activities</h3><ul class="acts">{act_html}</ul></div>
</div></section>

<section><h2>All metrics</h2><div class="panel tbl"><table>
<thead><tr><th>Metric</th><th>Last 24 h</th><th>7-day avg</th><th>30-day avg</th><th>vs 7-day</th><th>Status</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>

<footer>Generated {generated} from Garmin Connect data via garmin-pp-cli. Averages exclude the day being reported. Guidance is based on your own trends, not medical advice.</footer>
</div>
<div id="tip" hidden></div>
<script>{JS}</script>
"""


def render_error(report, message):
    return f"""<title>Garmin Morning Report</title>
<style>{CSS}</style><div class="wrap"><header><span class="eyebrow">Garmin morning report · {report.strftime('%A, %B %-d, %Y')}</span>
<h1>Report not generated</h1></header><div class="panel"><p>{esc(message)}</p></div></div>"""


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="report morning, YYYY-MM-DD (default: today in TZ)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "reports" / "garmin-daily.html"))
    ap.add_argument("--json", help="also write the computed summary as JSON here")
    ap.add_argument("--no-sync", action="store_true")
    args = ap.parse_args()

    tz = os.environ.get("TZ_REPORT", "America/Los_Angeles")
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo(tz))
    except Exception:
        now = dt.datetime.now()
    report = dt.date.fromisoformat(args.date) if args.date else now.date()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        check_auth()
        if not args.no_sync:
            sync()
        days = load(report)
    except PermissionError as e:
        out.write_text(render_error(report, f"Garmin sign-in is needed before the report can run ({e}). Ask Claude to log in to Garmin again."))
        print(f"AUTH_REQUIRED: {e}", file=sys.stderr)
        return 4

    ms = assess(days, report)
    recs, plan = recommendations(ms, days, report)
    wo = workout(ms, days, report, running_zones())
    out.write_text(render(report, days, ms, recs, plan, now.strftime("%Y-%m-%d %H:%M %Z"), wo))
    state, tone, summary, bullets = headline(ms, recs)
    result = dict(report_date=report.isoformat(), sleep_synced=isinstance(next(m for m in ms if m["key"] == "sleep_h")["cur"], (int, float)), state=state, summary=summary, bullets=bullets,
                  recommendations=[dict(title=r["title"], body=r["body"]) for r in recs],
                  tonight=plan, workout=wo,
                  metrics=[{k: m.get(k) for k in ("label", "cur", "a7", "a30", "status", "unit")} for m in ms])
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result, indent=1, default=str))
    print(f"\nWrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
