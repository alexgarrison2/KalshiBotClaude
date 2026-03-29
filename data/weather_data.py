"""
Weather Data — fetches all external data needed by the WeatherEdge strategy.

Three data sources (ported from Taylor's trading bot):

  NWS Forecasts   — Official next-day high/low forecasts from api.weather.gov
  METAR Intraday  — Current observed temperature from airport stations
  Open-Meteo      — GFS + ECMWF ensemble model forecasts (free, no API key)

All functions return plain dicts. They are called once per scan loop and
the results are passed down to the strategy for signal evaluation.
"""
import json
import logging
import math
import random
import time
import requests
from datetime import date, datetime
from typing import Optional
from pathlib import Path

log = logging.getLogger(__name__)

NWS_HEADERS    = {"User-Agent": "kalshi-bot/1.0", "Accept": "application/geo+json"}
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_BACKOFF_BASE    = 1.0   # seconds — first retry wait
_BACKOFF_MAX     = 16.0  # seconds — cap on backoff
_BACKOFF_RETRIES = 3     # number of retries on 429 / 5xx


def _http_get(url: str, **kwargs) -> requests.Response:
    """
    GET with exponential backoff on rate-limit (429) or server errors (5xx).

    On success returns the Response. On permanent failure (4xx other than
    429, or exhausted retries) raises the last exception / returns the
    last bad response for the caller to handle.
    """
    delay = _BACKOFF_BASE
    for attempt in range(_BACKOFF_RETRIES + 1):
        try:
            r = requests.get(url, **kwargs)
            if r.status_code in (429,) or r.status_code >= 500:
                if attempt < _BACKOFF_RETRIES:
                    jitter = random.uniform(0, delay * 0.3)
                    log.warning(
                        f"HTTP {r.status_code} from {url!r} — "
                        f"retry {attempt+1}/{_BACKOFF_RETRIES} in {delay:.1f}s"
                    )
                    time.sleep(delay + jitter)
                    delay = min(delay * 2, _BACKOFF_MAX)
                    continue
            return r
        except requests.RequestException as exc:
            if attempt < _BACKOFF_RETRIES:
                jitter = random.uniform(0, delay * 0.3)
                log.warning(f"Request error for {url!r}: {exc} — retry {attempt+1}/{_BACKOFF_RETRIES}")
                time.sleep(delay + jitter)
                delay = min(delay * 2, _BACKOFF_MAX)
            else:
                raise
    return r  # exhausted retries, return last bad response

_CONFIG_PATH = Path(__file__).parent / "series_config.json"
with open(_CONFIG_PATH) as _f:
    SERIES_CONFIG: dict = json.load(_f)

ALL_SERIES = list(SERIES_CONFIG.keys())


# ── NWS Forecasts ─────────────────────────────────────────────────────────────

def fetch_nws_forecast(forecast_url: str) -> dict:
    """
    Return {date: (high_f, low_f), ...} from an NWS gridpoint forecast URL.
    Daytime periods → high_f, overnight periods → low_f.
    """
    r = requests.get(forecast_url, headers=NWS_HEADERS, timeout=10)
    r.raise_for_status()
    periods = r.json()["properties"]["periods"]

    highs: dict = {}
    lows: dict  = {}
    for p in periods:
        d    = date.fromisoformat(p["startTime"][:10])
        temp = float(p["temperature"])
        if p["isDaytime"]:
            if d not in highs:
                highs[d] = temp
        else:
            if d not in lows:
                lows[d] = temp

    all_dates = set(highs) | set(lows)
    return {d: (highs.get(d), lows.get(d)) for d in all_dates}


def fetch_all_forecasts() -> dict:
    """
    Return { series_ticker: {date: (high_f, low_f)} } for all configured series.
    Each city's forecast URL is only fetched once even if multiple series share it.
    """
    city_cache: dict = {}
    result: dict     = {}

    for series, cfg in SERIES_CONFIG.items():
        city = cfg["city"]
        if city not in city_cache:
            try:
                city_cache[city] = fetch_nws_forecast(cfg["forecast_url"])
                time.sleep(0.3)
            except Exception as e:
                log.warning(f"NWS forecast fetch failed for {city}: {e}")
                city_cache[city] = {}
        result[series] = city_cache[city]

    return result


# ── METAR Intraday Observations ───────────────────────────────────────────────

def fetch_metar_observations() -> dict:
    """
    Fetch current observed temperature for each city's airport station.

    Returns: { city_name: {"obs_temp": float, "obs_time": str, "station": str} }

    Used by Strategy #1: if the observed high/low already confirms the
    contract outcome, we override model_prob to near-certainty.
    """
    city_obs:    dict = {}
    seen_cities: set  = set()

    for series, cfg in SERIES_CONFIG.items():
        city    = cfg["city"]
        station = cfg.get("station")
        if city in seen_cities or not station:
            continue
        seen_cities.add(city)

        try:
            r = _http_get(
                f"https://api.weather.gov/stations/{station}/observations/latest",
                headers=NWS_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"METAR fetch for {city} ({station}) returned HTTP {r.status_code}")
                continue
            props    = r.json().get("properties", {})
            temp_c   = props.get("temperature", {}).get("value")
            obs_time = props.get("timestamp", "")[:16]
            if temp_c is None:
                log.warning(f"METAR for {city} ({station}): temperature value missing in response")
                continue
            temp_f = temp_c * 9 / 5 + 32
            city_obs[city] = {
                "obs_temp": round(temp_f, 1),
                "obs_time": obs_time,
                "station":  station,
            }
            time.sleep(0.2)
        except Exception as e:
            log.warning(f"METAR fetch failed for {city} ({station}): {e}")

    return city_obs


# ── Open-Meteo Ensemble (GFS + ECMWF) ────────────────────────────────────────

def fetch_ensemble_forecasts() -> dict:
    """
    Fetch GFS + ECMWF daily high/low forecasts from Open-Meteo.

    Returns: { city_name: { date_str: {"gfs_high": f, "gfs_low": f,
                                        "ecmwf_high": f, "ecmwf_low": f} } }

    Used by Strategy #3: require GFS and ECMWF to agree directionally
    with NWS before entering. Boost edge when all three agree.
    """
    city_ensemble: dict = {}
    seen_cities:   set  = set()

    for series, cfg in SERIES_CONFIG.items():
        city = cfg["city"]
        if city in seen_cities:
            continue
        seen_cities.add(city)

        try:
            r = _http_get(OPEN_METEO_URL, params={
                "latitude":         cfg["lat"],
                "longitude":        cfg["lon"],
                "daily":            "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "models":           "gfs_seamless,ecmwf_ifs025",
                "forecast_days":    3,
                "timezone":         "America/New_York",
            }, timeout=15)
            if r.status_code != 200:
                log.warning(f"Open-Meteo ensemble fetch for {city} returned HTTP {r.status_code}")
                continue

            data      = r.json()
            city_data: dict = {}

            for model_block in (data if isinstance(data, list) else [data]):
                model_name = model_block.get("model", "")
                daily      = model_block.get("daily", {})
                dates      = daily.get("time", [])
                highs      = daily.get("temperature_2m_max", [])
                lows       = daily.get("temperature_2m_min", [])
                prefix     = "gfs" if "gfs" in model_name else "ecmwf"

                for d, h, lo in zip(dates, highs, lows):
                    if d not in city_data:
                        city_data[d] = {}
                    if h is not None:
                        city_data[d][f"{prefix}_high"] = h
                    if lo is not None:
                        city_data[d][f"{prefix}_low"]  = lo

            city_ensemble[city] = city_data
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Ensemble fetch failed for {city}: {e}")

    return city_ensemble


# ── Math ──────────────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def model_prob(forecast: float, threshold: float, strike_type: str, sigma: float) -> float:
    """
    P(YES) = normCDF((forecast - threshold) / sigma)  for 'greater' contracts
    P(YES) = 1 - normCDF(...)                          for 'less' contracts
    """
    z = (forecast - threshold) / sigma
    p = norm_cdf(z)
    return p if strike_type == "greater" else 1.0 - p


# ── Calibrated Sigma Lookup ───────────────────────────────────────────────────
#
# calibrate_sigma.py generates data/sigma_lookup.json with per-city,
# per-month sigma values derived from 365 days of IEM ASOS observations.
# get_calibrated_sigma() is the public API the strategy uses at runtime.
# It always returns a valid float — falling back to original hardcoded
# defaults if the lookup file is missing or the city/month isn't covered.

_SIGMA_LOOKUP: dict       = {}
_SIGMA_LOOKUP_LOADED: bool = False

_SIGMA_EARLY_DEFAULT = 3.0   # °F — morning anchor (6 AM ET)
_SIGMA_LATE_DEFAULT  = 2.0   # °F — afternoon anchor (11 AM ET onward)
_SIGMA_INTERP_START  = 6     # ET hour where sigma begins decaying
_SIGMA_INTERP_END    = 11    # ET hour where sigma reaches afternoon value


def _load_sigma_lookup() -> dict:
    """Read sigma_lookup.json from the data/ directory. Returns {} if absent."""
    lookup_path = Path(__file__).parent / "sigma_lookup.json"
    if lookup_path.exists():
        try:
            with open(lookup_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _interpolate_sigma(early: float, late: float, hour_et: int) -> float:
    """
    Linearly interpolate sigma between the morning anchor (6 AM) and
    afternoon anchor (11 AM). Before 6 AM returns early; after 11 AM
    returns late; in between smoothly transitions.

    This eliminates the artificial sigma cliff at 11 AM where trades
    placed at 10:59 had 50% higher uncertainty than trades at 11:01.
    """
    if hour_et <= _SIGMA_INTERP_START:
        return early
    if hour_et >= _SIGMA_INTERP_END:
        return late
    t = (hour_et - _SIGMA_INTERP_START) / (_SIGMA_INTERP_END - _SIGMA_INTERP_START)
    return early + t * (late - early)


def get_calibrated_sigma(series: str, hour_et: int) -> float:
    """
    Return the calibrated forecast-uncertainty sigma (°F) for a given
    series and time of day.

    Reads from data/sigma_lookup.json (generated by calibrate_sigma.py).
    Uses linear interpolation between the morning (6 AM) and afternoon
    (11 AM) anchor points so sigma decays smoothly rather than jumping
    at 11 AM.

    Falls back to the original hardcoded 3.0 / 2.0 °F defaults if:
      - sigma_lookup.json doesn't exist yet
      - The series isn't covered (new city, or download failed)
      - The current calendar month has no data

    Args:
        series:   Series ticker, e.g. "KXHIGHTLV"
        hour_et:  Current hour in Eastern Time (0–23)

    Returns:
        Sigma in °F — always a valid positive float, never raises.
    """
    global _SIGMA_LOOKUP, _SIGMA_LOOKUP_LOADED
    if not _SIGMA_LOOKUP_LOADED:
        _SIGMA_LOOKUP        = _load_sigma_lookup()
        _SIGMA_LOOKUP_LOADED = True

    series_data = _SIGMA_LOOKUP.get(series)
    if not series_data:
        return _interpolate_sigma(_SIGMA_EARLY_DEFAULT, _SIGMA_LATE_DEFAULT, hour_et)

    current_month = str(date.today().month)
    month_data    = series_data.get(current_month)
    if not month_data:
        return _interpolate_sigma(_SIGMA_EARLY_DEFAULT, _SIGMA_LATE_DEFAULT, hour_et)

    morning_sigma   = float(month_data.get("morning",   _SIGMA_EARLY_DEFAULT))
    afternoon_sigma = float(month_data.get("afternoon", _SIGMA_LATE_DEFAULT))
    return _interpolate_sigma(morning_sigma, afternoon_sigma, hour_et)
