#!/usr/bin/env python3
"""
Scarica da Intervals.icu:
  - l'elenco delle attivita' (summary.csv)
  - gli stream per-secondo di ogni attivita' (data/activities/*.csv)
  - i dati wellness giornalieri (wellness.csv): VO2max, HRV, riposo, peso, load

Incrementale: gli stream gia' scaricati non vengono riscaricati.

Variabili d'ambiente richieste:
  INTERVALS_ATHLETE_ID   es. i123456
  INTERVALS_API_KEY      da intervals.icu -> Settings -> Developer
Opzionali:
  SYNC_OLDEST            data minima ISO (default: 2026-01-01)
  FORCE_REFRESH          "1" per riscaricare tutti gli stream
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

BASE = "https://intervals.icu/api/v1"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ACTIVITIES_DIR = DATA / "activities"

# Stream richiesti. Se il file FIT non ne contiene qualcuno, Intervals lo omette
# semplicemente dalla risposta: non e' un errore.
STREAM_TYPES = [
    "time",
    "distance",
    "velocity_smooth",   # velocita' istantanea m/s
    "heartrate",
    "cadence",
    "altitude",
    "grade_smooth",
    "watts",
    "temp",
    "moving",
]

TIMEOUT = 60
RETRIES = 3
RETRY_WAIT = 5


def log(msg):
    print(msg, flush=True)


def get_env():
    athlete = os.environ.get("INTERVALS_ATHLETE_ID", "").strip()
    key = os.environ.get("INTERVALS_API_KEY", "").strip()
    if not athlete or not key:
        log("ERRORE: INTERVALS_ATHLETE_ID o INTERVALS_API_KEY mancanti.")
        log("Impostali come GitHub Secrets (o come variabili d'ambiente in locale).")
        sys.exit(1)
    if not athlete.startswith("i"):
        log(f"ATTENZIONE: athlete id '{athlete}' non inizia con 'i'. Di solito e' tipo i123456.")
    return athlete, key


def request(session, url, params=None, expect="json"):
    """GET con retry. Ritorna dict/list se expect='json', altrimenti testo grezzo."""
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                log("ERRORE 401: API key o athlete id non validi.")
                sys.exit(1)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = RETRY_WAIT * attempt * 3
                log(f"  rate limit, attendo {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json() if expect == "json" else r.text
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT * attempt)
    log(f"  fallito dopo {RETRIES} tentativi: {url} ({last_error})")
    return None


def fetch_activities(session, athlete, oldest, newest):
    url = f"{BASE}/athlete/{athlete}/activities"
    data = request(session, url, params={"oldest": oldest, "newest": newest})
    if data is None:
        log("Nessuna attivita' ricevuta (endpoint non raggiungibile).")
        return []
    if not isinstance(data, list):
        log(f"Risposta inattesa dall'endpoint attivita': {type(data)}")
        return []
    return data


def fetch_wellness(session, athlete, oldest, newest):
    url = f"{BASE}/athlete/{athlete}/wellness.csv"
    text = request(session, url, params={"oldest": oldest, "newest": newest}, expect="text")
    return text


def fetch_streams(session, activity_id):
    url = f"{BASE}/activity/{activity_id}/streams.csv"
    return request(
        session,
        url,
        params={"types": ",".join(STREAM_TYPES)},
        expect="text",
    )


def activity_filename(act):
    """Nome file stabile: YYYY-MM-DD_<id>.csv"""
    start = act.get("start_date_local") or act.get("start_date") or ""
    day = start[:10] if len(start) >= 10 else "0000-00-00"
    return f"{day}_{act.get('id', 'unknown')}.csv"


SUMMARY_FIELDS = [
    ("id", "id"),
    ("start_date_local", "start_local"),
    ("type", "type"),
    ("name", "name"),
    ("distance", "distance_m"),
    ("moving_time", "moving_time_s"),
    ("elapsed_time", "elapsed_time_s"),
    ("total_elevation_gain", "elev_gain_m"),
    ("average_speed", "avg_speed_ms"),
    ("max_speed", "max_speed_ms"),
    ("average_heartrate", "avg_hr"),
    ("max_heartrate", "max_hr"),
    ("average_cadence", "avg_cadence"),
    ("icu_training_load", "training_load"),
    ("icu_intensity", "intensity"),
    ("icu_efficiency_factor", "efficiency_factor"),
    ("icu_hr_zone_times", "hr_zone_times"),
    ("pace", "pace"),
    ("gap", "grade_adjusted_pace"),
    ("icu_rpe", "rpe"),
    ("feel", "feel"),
    ("icu_atl", "atl_fatica"),
    ("icu_ctl", "ctl_fitness"),
    ("calories", "calories"),
]


def write_summary(activities):
    path = DATA / "summary.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([alias for _, alias in SUMMARY_FIELDS] + ["has_streams"])
        for act in sorted(activities, key=lambda a: a.get("start_date_local") or ""):
            has_streams = (ACTIVITIES_DIR / activity_filename(act)).exists()
            row = []
            for key, _alias in SUMMARY_FIELDS:
                val = act.get(key)
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, separators=(",", ":"))
                row.append("" if val is None else val)
            row.append("1" if has_streams else "0")
            writer.writerow(row)
    log(f"Scritto {path.relative_to(ROOT)} ({len(activities)} attivita')")


def main():
    athlete, key = get_env()
    oldest = os.environ.get("SYNC_OLDEST", "2026-01-01")
    newest = date.today().isoformat()
    force = os.environ.get("FORCE_REFRESH", "") == "1"

    DATA.mkdir(exist_ok=True)
    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.auth = ("API_KEY", key)
    session.headers.update({"User-Agent": "Training_Plan-sync/1.0"})

    log(f"Sync Intervals.icu | atleta {athlete} | {oldest} -> {newest}")

    activities = fetch_activities(session, athlete, oldest, newest)
    log(f"Trovate {len(activities)} attivita'.")

    downloaded = 0
    skipped = 0
    failed = 0

    for act in activities:
        act_id = act.get("id")
        if not act_id:
            continue
        target = ACTIVITIES_DIR / activity_filename(act)
        if target.exists() and not force:
            skipped += 1
            continue
        text = fetch_streams(session, act_id)
        if not text or not text.strip():
            log(f"  nessuno stream per {act_id} ({act.get('name', '')})")
            failed += 1
            continue
        target.write_text(text, encoding="utf-8")
        downloaded += 1
        log(f"  salvato {target.name}")
        time.sleep(0.4)  # gentile con l'API

    log(f"Stream: {downloaded} nuovi, {skipped} gia' presenti, {failed} senza dati.")

    wellness = fetch_wellness(session, athlete, oldest, newest)
    if wellness and wellness.strip():
        (DATA / "wellness.csv").write_text(wellness, encoding="utf-8")
        log("Scritto data/wellness.csv")
    else:
        log("Nessun dato wellness ricevuto.")

    write_summary(activities)

    (DATA / "last_sync.txt").write_text(
        datetime.now().astimezone().isoformat(timespec="seconds") + "\n",
        encoding="utf-8",
    )
    log("Fatto.")


if __name__ == "__main__":
    main()
