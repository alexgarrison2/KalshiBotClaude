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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Optional
from pathlib import Path

log = logging.getLogger(__name__)

NWS_HEADERS    = {"User-Agent": "kalshi-bot/1.0", "Accept": "application/geo+json"}
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# ── Persistent HTTP sessions (connection keep-alive across parallel threads) ──
# Each host gets its own session so TCP connections are reused within a host.
# Sessions are thread-safe for reading; urllib3's connection pool handles locking.
_nws_session      = requests.Session()
_nws_session.headers.update(NWS_HEADERS)
_open_meteo_session = requests.Session()

_BACKOFF_BASE    = 1.0   # seconds — first retry wait
_BACKOFF_MAX     = 16.0  # seconds — cap on backoff
_BACKOFF_RETRIES = 3     # number of retries on 429 / 5xx

# ── In-memory daily extremes cache (running high/low per city) ───────────────
_metar_day_extremes: dict = {}        # {city: {"day_high": float, "day_low": float}}
_metar_last_full_fetch: float = 0.0   # timestamp of last full-day history pull
_metar_current_date: str = ""         # detect day rollover → reset cache
_metar_last_reading: dict = {}        # {city: temp_f} — previous obs for temporal check
_FULL_FETCH_INTERVAL = 600            # 10 minutes

# ── METAR plausibility bounds ────────────────────────────────────────────────
_METAR_ABS_MIN       = -40.0   # °F — reject anything below
_METAR_ABS_MAX       = 130.0   # °F — reject anything above
_METAR_TEMPORAL_MAX  = 20.0    # °F — max jump between consecutive readings


def _validate_metar_temp(temp_f: float, city: str, station: str) -> bool:
    """
    Reject implausible METAR temperatures that could cause bad trades.

    A broken weather station reporting e.g. 150°F would trigger a 97%
    confidence METAR override and place a guaranteed-loss trade.

    Checks:
      1. Absolute bounds: -40°F to 130°F
      2. Temporal continuity: ≤20°F jump from previous reading at same city
    """
    if temp_f < _METAR_ABS_MIN or temp_f > _METAR_ABS_MAX:
        log.warning(
            f"METAR REJECTED {city} ({station}): {temp_f:.1f}°F "
            f"outside [{_METAR_ABS_MIN}, {_METAR_ABS_MAX}]"
        )
        return False

    prev = _metar_last_reading.get(city)
    if prev is not None and abs(temp_f - prev) > _METAR_TEMPORAL_MAX:
        log.warning(
            f"METAR REJECTED {city} ({station}): {temp_f:.1f}°F "
            f"jumped {abs(temp_f - prev):.1f}° from prev {prev:.1f}°F"
        )
        return False

    return True


def _http_get(url: str, **kwargs) -> requests.Response:
    """
    GET with exponential backoff on rate-limit (429) or server errors (5xx).

    On success returns the Response. On permanent failure (4xx other than
    429, or exhausted retries) raises the last exception / returns the
    last bad response for the caller to handle.
    """
    # Pick the right session based on host so connections are reused
    if "weather.gov" in url:
        session = _nws_session
    elif "open-meteo.com" in url:
        session = _open_meteo_session
    else:
        session = requests.Session()

    delay = _BACKOFF_BASE
    for attempt in range(_BACKOFF_RETRIES + 1):
        try:
            r = session.get(url, **kwargs)
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
    Uses the persistent _nws_session so TCP connections are reused across cities.
    """
    r = _nws_session.get(forecast_url, timeout=10)
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


def _fetch_city_forecast(city: str, forecast_url: str) -> tuple:
    """Fetch a single city's NWS forecast. Returns (city, data_dict)."""
    try:
        data = fetch_nws_forecast(forecast_url)
        return city, data
    except Exception as e:
        log.warning(f"NWS forecast fetch failed for {city}: {e}")
        return city, {}


def fetch_all_forecasts() -> dict:
    """
    Return { series_ticker: {date: (high_f, low_f)} } for all configured series.
    Each city's forecast URL is only fetched once even if multiple series share it.
    Fetches are parallelized with ThreadPoolExecutor (max 5 concurrent).
    """
    # Deduplicate: one fetch per city
    city_urls: dict = {}
    for series, cfg in SERIES_CONFIG.items():
        city = cfg["city"]
        if city not in city_urls:
            city_urls[city] = cfg["forecast_url"]

    city_cache: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_city_forecast, city, url): city
            for city, url in city_urls.items()
        }
        for future in as_completed(futures):
            city, data = future.result()
            city_cache[city] = data

    result: dict = {}
    for series, cfg in SERIES_CONFIG.items():
        result[series] = city_cache.get(cfg["city"], {})

    return result


# ── METAR Intraday Observations ───────────────────────────────────────────────

def _fetch_full_day_observations(station: str) -> tuple[Optional[float], Optional[float]]:
    """
    Fetch ALL of today's observations for a station and return (max_f, min_f).

    Calls GET /stations/{station}/observations?start={today}T00:00:00Z
    Returns (None, None) on failure.
    """
    today_iso = date.today().isoformat()
    try:
        r = _http_get(
            f"https://api.weather.gov/stations/{station}/observations"
            f"?start={today_iso}T00:00:00Z",
            headers=NWS_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"Full-day obs for {station} returned HTTP {r.status_code}")
            return None, None

        features = r.json().get("features", [])
        temps_f = []
        for feat in features:
            temp_c = feat.get("properties", {}).get("temperature", {}).get("value")
            if temp_c is not None:
                t = temp_c * 9 / 5 + 32
                if _METAR_ABS_MIN <= t <= _METAR_ABS_MAX:
                    temps_f.append(t)

        if not temps_f:
            return None, None
        return max(temps_f), min(temps_f)
    except Exception as e:
        log.warning(f"Full-day obs fetch failed for {station}: {e}")
        return None, None


def fetch_metar_observations() -> dict:
    """
    Fetch current observed temperature for each city's airport station
    and track the running daily high/low across all scans.

    Returns: { city_name: {
        "obs_temp": float,   # current reading
        "obs_time": str,
        "station":  str,
        "day_high": float,   # max observed today
        "day_low":  float,   # min observed today
    } }

    Used by Strategy #1: if the observed high/low already confirms the
    contract outcome, we override model_prob to near-certainty.
    """
    global _metar_day_extremes, _metar_last_full_fetch, _metar_current_date, _metar_last_reading

    # Reset cache on day rollover
    today_str = date.today().isoformat()
    if today_str != _metar_current_date:
        _metar_day_extremes = {}
        _metar_last_full_fetch = 0.0
        _metar_current_date = today_str
        _metar_last_reading = {}

    city_obs:    dict = {}
    seen_cities: set  = set()
    city_stations: dict = {}  # track station per city for full-day fetch

    # Deduplicate cities
    cities_to_fetch: list = []
    for series, cfg in SERIES_CONFIG.items():
        city    = cfg["city"]
        station = cfg.get("station")
        if city in seen_cities or not station:
            continue
        seen_cities.add(city)
        city_stations[city] = station
        cities_to_fetch.append((city, station))

    def _fetch_one_metar(city: str, station: str):
        """Fetch latest METAR for one city. Returns (city, station, props) or None."""
        try:
            r = _http_get(
                f"https://api.weather.gov/stations/{station}/observations/latest",
                headers=NWS_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"METAR fetch for {city} ({station}) returned HTTP {r.status_code}")
                return None
            return city, station, r.json().get("properties", {})
        except Exception as e:
            log.warning(f"METAR fetch failed for {city} ({station}): {e}")
            return None

    # Parallel METAR fetches (max 5 concurrent to respect rate limits)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_fetch_one_metar, c, s) for c, s in cities_to_fetch]
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            city, station, props = result
            temp_c   = props.get("temperature", {}).get("value")
            obs_time = props.get("timestamp", "")[:16]
            if temp_c is None:
                log.warning(f"METAR for {city} ({station}): temperature value missing in response")
                continue
            temp_f = temp_c * 9 / 5 + 32

            # Reject implausible readings (broken sensor protection)
            if not _validate_metar_temp(temp_f, city, station):
                continue
            _metar_last_reading[city] = temp_f

            # Update running daily extremes
            prev = _metar_day_extremes.get(city, {})
            _metar_day_extremes[city] = {
                "day_high": max(temp_f, prev.get("day_high", temp_f)),
                "day_low":  min(temp_f, prev.get("day_low", temp_f)),
            }

            city_obs[city] = {
                "obs_temp": round(temp_f, 1),
                "obs_time": obs_time,
                "station":  station,
            }

    # Every 10 min: backfill from full day's observation history (parallelized)
    now = time.time()
    if now - _metar_last_full_fetch > _FULL_FETCH_INTERVAL:
        _metar_last_full_fetch = now

        def _backfill_city(city: str, station: str):
            return city, _fetch_full_day_observations(station)

        with ThreadPoolExecutor(max_workers=5) as pool:
            bf_futures = [
                pool.submit(_backfill_city, city, station)
                for city, station in city_stations.items()
            ]
            for future in as_completed(bf_futures):
                city, (day_max, day_min) = future.result()
                if day_max is not None:
                    prev = _metar_day_extremes.get(city, {})
                    _metar_day_extremes[city] = {
                        "day_high": max(day_max, prev.get("day_high", day_max)),
                        "day_low":  min(day_min, prev.get("day_low", day_min)),
                    }
        log.info(f"Full-day METAR backfill complete for {len(city_stations)} cities")

    # Attach day_high / day_low to return dict
    for city in city_obs:
        extremes = _metar_day_extremes.get(city, {})
        city_obs[city]["day_high"] = round(extremes.get("day_high", city_obs[city]["obs_temp"]), 1)
        city_obs[city]["day_low"]  = round(extremes.get("day_low",  city_obs[city]["obs_temp"]), 1)

    return city_obs


# ── Open-Meteo Ensemble (GFS + ECMWF) ────────────────────────────────────────

def _fetch_one_ensemble(city: str, lat: float, lon: float) -> tuple:
    """Fetch GFS + ECMWF for one city. Returns (city, data_dict)."""
    try:
        r = _http_get(OPEN_METEO_URL, params={
            "latitude":         lat,
            "longitude":        lon,
            "daily":            "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "models":           "gfs_seamless,ecmwf_ifs025",
            "forecast_days":    3,
            "timezone":         "America/New_York",
        }, timeout=15)
        if r.status_code != 200:
            log.warning(f"Open-Meteo ensemble fetch for {city} returned HTTP {r.status_code}")
            return city, {}

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

        return city, city_data
    except Exception as e:
        log.warning(f"Ensemble fetch failed for {city}: {e}")
        return city, {}


def fetch_ensemble_forecasts() -> dict:
    """
    Fetch GFS + ECMWF daily high/low forecasts from Open-Meteo.

    Returns: { city_name: { date_str: {"gfs_high": f, "gfs_low": f,
                                        "ecmwf_high": f, "ecmwf_low": f} } }

    Used by Strategy #3: require GFS and ECMWF to agree directionally
    with NWS before entering. Boost edge when all three agree.
    Parallelized with ThreadPoolExecutor (max 5 concurrent).
    """
    # Deduplicate cities
    city_configs: dict = {}
    for series, cfg in SERIES_CONFIG.items():
        city = cfg["city"]
        if city not in city_configs:
            city_configs[city] = (cfg["lat"], cfg["lon"])

    city_ensemble: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_one_ensemble, city, lat, lon): city
            for city, (lat, lon) in city_configs.items()
        }
        for future in as_completed(futures):
            city, data = future.result()
            city_ensemble[city] = data

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
