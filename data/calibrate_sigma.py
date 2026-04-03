"""
Sigma Calibration — empirically calibrate per-city forecast uncertainty.

WHY THIS MATTERS
─────────────────
The core probability model is:
    P(YES) = normCDF((NWS_forecast − threshold) / σ)

The σ (sigma) controls how wide the uncertainty band is around the forecast.
Originally: 3.0°F (morning) / 2.0°F (afternoon) everywhere.

But forecast uncertainty varies dramatically by city:
  • Miami summer:     ~2°F   (stable maritime air)
  • Denver spring:   ~5°F   (highly variable front-driven weather)
  • Phoenix summer:  ~1.5°F (locked into heat dome)
  • Chicago winter:  ~5°F   (Lake effect, polar vortex swings)

Using a flat 3°F everywhere means we're OVERCONFIDENT in stable cities
(and miss profitable trades) and UNDERCONFIDENT in volatile cities
(and enter bad trades).

DATA SOURCES
─────────────
Two sources are supported (selectable via --source flag):

  1. IEM ASOS (default legacy): 365 days of hourly observations from Iowa
     Environmental Mesonet. Daily high/low derived from hourly readings.

  2. CF6 (preferred): ~4 years of official NWS Climate First-order reports
     from weather.gov. Daily max/min directly reported — same source Kalshi
     uses for settlement. Run `python data/fetch_cf6.py` first to download.

     More data = more robust monthly sigma estimates, especially for volatile
     shoulder seasons. CF6 also ensures apples-to-apples with Kalshi.

HOW IT WORKS
─────────────
1. Load daily high/low data (from IEM ASOS or CF6)
2. For each calendar month, compute the standard deviation of daily highs
   and lows separately
3. Scale by 0.65 (the empirical ratio of NWS 24h forecast error to raw
   temperature variability, from NWS verification statistics literature)
4. Write a lookup table: data/sigma_lookup.json

The live bot reads sigma_lookup.json on startup and uses per-city,
per-month sigma values instead of hardcoded 3.0/2.0.

USAGE
──────
    python data/calibrate_sigma.py                # IEM ASOS (1 year, legacy)
    python data/calibrate_sigma.py --source cf6   # CF6 (~4 years, preferred)

Re-run monthly to keep calibration fresh.

OUTPUT
───────
    data/sigma_lookup.json   ← read by weather_data.get_calibrated_sigma()

    Sample console output:
        Las Vegas     Jan: afternoon=1.8°F  morning=2.7°F
        Denver        Mar: afternoon=4.1°F  morning=6.2°F
        ...
"""

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# NWS 24-hour forecast error ≈ 65% of raw daily temperature variability.
# This scaling factor comes from published NWS MOS verification reports:
#   Raw temp std dev (day-to-day variability) is wider than forecast error
#   because forecasters have skill — they can anticipate trends.
#   Typical published NWS RMSE / raw std dev ratio ≈ 0.60–0.70.
FORECAST_TO_VARIABILITY_RATIO = 0.65

MORNING_MULTIPLIER = 1.5    # morning sigma is 50% wider than afternoon
CUTOFF_HOUR        = 11     # ET hour when we switch morning → afternoon
SIGMA_MIN          = 1.0    # floor: never below 1°F
SIGMA_MAX          = 5.0    # ceiling: never above 5°F
                             # Published NWS 24h high-temp RMSE is 3–4°F for most US
                             # cities.  8°F allowed effectively non-predictive sigmas
                             # where normCDF barely moves and nearly everything looks
                             # like edge.  5°F keeps the model discriminating.
MIN_DAYS_PER_MONTH = 8      # require this many obs-days to trust the std dev
DAYS_OF_HISTORY    = 365    # 1 year covers all 12 calendar months

CONFIG_PATH  = Path(__file__).parent / "series_config.json"
LOOKUP_PATH  = Path(__file__).parent / "sigma_lookup.json"
CF6_DATA_PATH = Path(__file__).parent / "cf6_daily.json"


# ── IEM Data Fetch ─────────────────────────────────────────────────────────────

def fetch_hourly_obs(station: str, days_back: int = DAYS_OF_HISTORY) -> list[dict]:
    """
    Download hourly temperature observations for a given station from IEM ASOS.

    Returns a list of dicts: [{"date": "YYYY-MM-DD", "hour": 14, "tmpf": 87.2}, ...]
    Missing observations (marked "M" by IEM) are silently dropped.
    """
    end   = date.today()
    start = end - timedelta(days=days_back)

    params = {
        "station":     station,
        "data":        "tmpf",          # temperature in Fahrenheit
        "year1":       start.year,
        "month1":      start.month,
        "day1":        start.day,
        "year2":       end.year,
        "month2":      end.month,
        "day2":        end.day,
        "tz":          "UTC",
        "format":      "comma",
        "latlon":      "no",
        "elev":        "no",
        "missing":     "M",
        "trace":       "T",
        "direct":      "no",
        "report_type": "3",             # ASOS routine hourly obs
    }

    r = requests.get(IEM_ASOS_URL, params=params, timeout=60)
    r.raise_for_status()

    obs = []
    for line in r.text.splitlines():
        # Skip comment lines and the header row
        if line.startswith("#") or "station" in line[:20]:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        valid_utc = parts[1].strip()   # "YYYY-MM-DD HH:MM"
        tmpf_str  = parts[2].strip()
        try:
            obs.append({
                "date": valid_utc[:10],
                "hour": int(valid_utc[11:13]),
                "tmpf": float(tmpf_str),
            })
        except (ValueError, IndexError):
            continue  # skip missing / malformed rows

    return obs


# ── CF6 Data Loader ──────────────────────────────────────────────────────────

def load_cf6_daily_stats(station: str) -> dict[str, dict]:
    """
    Load daily high/low from pre-fetched CF6 data (data/cf6_daily.json).
    Run `python data/fetch_cf6.py` first to populate.

    Returns: {"YYYY-MM-DD": {"high": float, "low": float}, ...}
    """
    if not CF6_DATA_PATH.exists():
        raise FileNotFoundError(
            f"{CF6_DATA_PATH} not found. Run `python data/fetch_cf6.py` first."
        )
    with open(CF6_DATA_PATH) as f:
        all_data = json.load(f)

    records = all_data.get(station, [])
    result = {}
    for r in records:
        result[r["date"]] = {"high": float(r["max"]), "low": float(r["min"])}
    return result


# ── Daily Stats ───────────────────────────────────────────────────────────────

def compute_daily_stats(obs: list[dict]) -> dict[str, dict]:
    """
    Convert hourly observations to daily high and low temperatures.

    Requires at least 12 hourly readings in a day to produce a valid
    estimate (avoids corrupt days with only 1–2 obs skewing the stats).

    Returns: {"YYYY-MM-DD": {"high": float, "low": float}, ...}
    """
    by_date: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        by_date[o["date"]].append(o["tmpf"])

    result = {}
    for d, temps in by_date.items():
        if len(temps) >= 12:
            result[d] = {"high": max(temps), "low": min(temps)}
    return result


# ── Monthly Sigma Computation ─────────────────────────────────────────────────

def _stdev(values: list[float]) -> float:
    """Sample standard deviation. Returns 3.0 if fewer than 3 values."""
    if len(values) < 3:
        return 3.0
    n    = len(values)
    mean = sum(values) / n
    var  = sum((x - mean) ** 2 for x in values) / (n - 1)
    return math.sqrt(var)


def compute_monthly_sigma(daily_stats: dict) -> dict[int, dict]:
    """
    For each calendar month, compute the calibrated sigma for highs and lows.

    Steps:
      1. Group daily highs/lows by month
      2. Compute std dev of daily highs → high variability
      3. Compute std dev of daily lows  → low variability
      4. Multiply by FORECAST_TO_VARIABILITY_RATIO (0.65)
      5. Clip to [SIGMA_MIN, SIGMA_MAX]

    Returns: {month_int: {"high_sigma": float, "low_sigma": float}}
    """
    by_month: dict[int, dict] = defaultdict(lambda: {"highs": [], "lows": []})

    for d, stats in daily_stats.items():
        month = int(d[5:7])
        by_month[month]["highs"].append(stats["high"])
        by_month[month]["lows"].append(stats["low"])

    result = {}
    for month, data in sorted(by_month.items()):
        if len(data["highs"]) < MIN_DAYS_PER_MONTH:
            continue   # not enough data to calibrate this month
        raw_high = _stdev(data["highs"])
        raw_low  = _stdev(data["lows"])
        result[month] = {
            "high_sigma": round(
                max(SIGMA_MIN, min(SIGMA_MAX, raw_high * FORECAST_TO_VARIABILITY_RATIO)), 2
            ),
            "low_sigma": round(
                max(SIGMA_MIN, min(SIGMA_MAX, raw_low * FORECAST_TO_VARIABILITY_RATIO)), 2
            ),
        }
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Calibrate per-city sigma values")
    parser.add_argument(
        "--source", choices=["iem", "cf6"], default="iem",
        help="Data source: 'iem' (IEM ASOS, 1 year) or 'cf6' (weather.gov CF6, ~4 years)"
    )
    args = parser.parse_args()

    use_cf6 = args.source == "cf6"

    with open(CONFIG_PATH) as f:
        config: dict = json.load(f)

    lookup: dict  = {}
    station_cache: dict = {}   # station → monthly sigma (avoid duplicate downloads)

    total  = len(config)
    done   = 0
    failed = 0

    source_label = "CF6 (weather.gov)" if use_cf6 else f"IEM ASOS ({DAYS_OF_HISTORY} days)"
    print(f"\nCalibrating sigma for {total} weather series — source: {source_label}\n")
    print(f"  Ratio: forecast error ≈ {FORECAST_TO_VARIABILITY_RATIO:.0%} of raw variability")
    print(f"  Morning: {MORNING_MULTIPLIER}× afternoon sigma (before {CUTOFF_HOUR}:00 ET)")
    print(f"  Sigma bounds: [{SIGMA_MIN}°F, {SIGMA_MAX}°F]\n")

    for series, cfg in config.items():
        station   = cfg.get("station", "")
        temp_type = cfg.get("temp_type", "high")
        city      = cfg.get("city", series)

        if not station:
            print(f"  {series:28s}  SKIP — no station configured")
            continue

        # Load data once per station even if multiple series share it
        if station not in station_cache:
            print(f"  {'Loading' if use_cf6 else 'Fetching'} {city:20s} ({station}) ...", end=" ", flush=True)
            try:
                if use_cf6:
                    daily = load_cf6_daily_stats(station)
                else:
                    obs   = fetch_hourly_obs(station)
                    daily = compute_daily_stats(obs)
                    time.sleep(0.5)   # be polite to IEM

                monthly = compute_monthly_sigma(daily)
                station_cache[station] = monthly
                print(f"OK  ({len(daily)} days,  {len(monthly)} months)")
            except Exception as e:
                print(f"FAILED — {e}")
                station_cache[station] = {}
                failed += 1
                continue

        monthly = station_cache[station]
        if not monthly:
            continue

        # Build series lookup: {month_str: {"afternoon": sigma, "morning": sigma}}
        series_lookup: dict = {}
        for month, sigmas in monthly.items():
            base = sigmas["high_sigma"] if temp_type == "high" else sigmas["low_sigma"]
            morning = round(base * MORNING_MULTIPLIER, 2)
            series_lookup[str(month)] = {
                "afternoon": base,
                "morning":   min(SIGMA_MAX, morning),
            }

        if series_lookup:
            lookup[series] = series_lookup
            done += 1

    # ── Write output ──────────────────────────────────────────────────────────

    with open(LOOKUP_PATH, "w") as f:
        json.dump(lookup, f, indent=2)

    print(f"\n{'─' * 60}")
    print(f"Saved {LOOKUP_PATH}")
    print(f"Calibrated {done}/{total} series  |  {failed} failed")

    # ── Print summary for current month ──────────────────────────────────────

    current_month = str(date.today().month)
    month_name    = date.today().strftime("%B")

    print(f"\nSigma values for {month_name} (vs previous hardcoded 3.0°F/2.0°F):\n")
    print(f"  {'City':20s}  {'Type':4s}  {'Afternoon':>9}  {'Morning':>8}  {'vs old (3.0/2.0)':>18}")
    print(f"  {'─'*20}  {'─'*4}  {'─'*9}  {'─'*8}  {'─'*18}")

    for series in sorted(lookup):
        months = lookup[series]
        month_data = months.get(current_month)
        if not month_data:
            continue
        cfg  = config.get(series, {})
        city = cfg.get("city", series)[:20]
        ttype = cfg.get("temp_type", "high")
        aft  = month_data["afternoon"]
        mor  = month_data["morning"]
        delta_aft = aft  - 2.0
        delta_mor = mor  - 3.0
        print(
            f"  {city:20s}  {ttype:4s}  {aft:>8.2f}°F  {mor:>7.2f}°F"
            f"  aft {delta_aft:+.2f}  mor {delta_mor:+.2f}"
        )

    print()


if __name__ == "__main__":
    main()
