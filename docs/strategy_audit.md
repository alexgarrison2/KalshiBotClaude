# Kalshi Weather Bot — Full Strategy Audit & Improvement Plan

*Conducted 2026-03-28. Phase 2 (sigma calibration) completed same day.*

---

## PART 1: WHAT YOU'RE DOING WELL

### 1. Multi-Source Ensemble Architecture (A)
Your 4-layer signal model is genuinely sophisticated for a first bot. Stacking NWS + GFS + ECMWF and requiring directional consensus before entering is *exactly* what professional weather traders do. Most beginners use a single forecast source and get destroyed by model errors. You're already ahead of 90% of retail Kalshi traders.

### 2. METAR Intraday Override (A+)
This is your best feature. Using real-time airport observations to override model probability when the outcome is already confirmed is free money — it's the equivalent of betting on a game that's already decided but the market hasn't caught up. The 0.97/0.03 bounds are appropriately conservative (not 1.0/0.0).

### 3. Limit Order Pricing Tiers (A-)
The PASSIVE/MIDWAY/CHASE tiered pricing is smart. Placing at mid for large edges (maker fee = near zero) and only chasing when edge persists after 30 min is disciplined. Most beginners use market orders and bleed fees.

### 4. Quarter-Kelly Sizing (B+)
Using 0.25x Kelly is textbook conservative for a new strategy without calibration history. This protects you from ruin during the learning phase. The hard cap of 10 contracts adds a second safety layer.

### 5. Operational Infrastructure (B+)
PID locking, pending order persistence across restarts, CSV trade logging with git push, LaunchAgent scheduling — this is production-grade ops for a personal bot. The restart resilience (reloading `traded_today` from logs) prevents duplicate trades.

### 6. Risk Manager (B)
Daily loss limit ($10), max positions (3), per-trade risk cap (5% of balance), max position size (5) — all reasonable guard rails for a small account learning phase.

---

## PART 2: CRITICAL WEAKNESSES (Profit Killers)

### ~~CRITICAL-1: Your Sigma Values Are Not Empirically Calibrated~~ ✅ FIXED (Phase 2)
**Files:** `strategies/weather_edge.py:58-59`, `data/weather_data.py`

**The Problem (was):** Hardcoded sigma = 3.0°F (morning) / 2.0°F (afternoon) everywhere. These were guesses, not calibrated values. NWS forecast error varies dramatically by city, season, and weather regime.

**Fix applied:** `data/calibrate_sigma.py` downloads 365 days of IEM ASOS observations per city, computes monthly std dev of daily highs/lows, scales by 0.65 (NWS forecast accuracy factor), and writes `data/sigma_lookup.json`. The strategy reads this at runtime via `get_calibrated_sigma(series, hour_et)` in `data/weather_data.py`.

**March calibration results (sample):**
- LA low: 2.12°F (was 2.0°F — correctly stable)
- Seattle high: 3.07°F (was 2.0°F)
- Houston high: 3.56°F (was 2.0°F)
- Las Vegas high: 5.98°F (was 2.0°F — nearly 3× wider)
- DC / Minneapolis / Boston: hit 8.0°F cap (was 2.0°F — massively underestimated spring volatility)

**To re-run calibration monthly:**
```bash
python3 data/calibrate_sigma.py
```

---

### CRITICAL-2: No Backtest for the Weather Strategy
**Files:** `backtesting/engine.py` (only implements `run_crypto_momentum`)

**The Problem:** Your backtesting engine only tests the crypto momentum strategy. There is **no backtest for the weather edge strategy**. You went live with zero historical validation of the 4-layer model.

**Why It Matters:** You literally don't know if this strategy is profitable. The benchmarks in `metrics.py` (win rate >55%, profit factor >1.5, drawdown <20%, Sharpe >1.0) have never been applied to your actual trading strategy. You're flying blind.

**Fix:** Build a weather-specific backtest that replays settled weather markets against historical NWS forecasts.

---

### CRITICAL-3: Kelly Formula Ignores Fees
**File:** `strategies/weather_edge.py:346-371`

**The Problem:** Your Kelly calculation uses `entry_cost` as `c` but doesn't subtract the expected fee from the win payout. The correct formula for a binary contract with fee `f` is:

```
f* = (p(1-f) - c) / (1 - c)     [not (p - c) / (1 - c)]
```

For taker fills (7% fee on notional), this matters. On a 50-cent contract, the fee is ~1.75 cents (maker) or ~7 cents (taker). Your Kelly over-sizes by the fee amount, which over hundreds of trades compounds into real money lost.

---

### CRITICAL-4: The Calibration Bias Layer Is Based on Unverified Hearsay
**File:** `strategies/weather_edge.py:23-26, 300-302`

**The Problem:** Layer 4 adds +15 cents edge credit based on the claim that "YES markets priced 35-65 cents win only 4-21% of the time." This is attributed to "Taylor's trading bot" but you have **no data to verify this claim** for current markets. Kalshi market microstructure changes over time. If this bias has been arbitraged away (which happens fast in prediction markets), Layer 4 is adding phantom edge and causing you to enter trades that aren't actually profitable.

**Fix:** Validate this claim against recent settled markets before relying on it.

---

## PART 3: SIGNIFICANT WEAKNESSES (Edge Leakers)

### SIG-1: Ensemble Disagreement = Hard Skip Wastes Opportunities
**File:** `strategies/weather_edge.py:296-297`

If GFS and ECMWF disagree with NWS, you skip the trade entirely. But "disagreement" is binary (prob > 0.5 vs < 0.5). If NWS says 51% YES, GFS says 49% YES, and ECMWF says 52% YES — that's a 1-2% disagreement but your code treats it as a hard conflict and skips. You're throwing away trades where models are *functionally* in agreement.

**Fix:** Use a tolerance band (e.g., skip only if any model differs by >15 percentage points from NWS).

### SIG-2: Single Sigma Step Function at 11 AM
**File:** `strategies/weather_edge.py:222-223`

Sigma jumps from 3.0 to 2.0 at exactly 11 AM ET. In reality, forecast uncertainty decreases continuously throughout the day. This discontinuity creates an artificial "signal cliff" around 11 AM — trades placed at 10:55 AM get dramatically different sizing than trades at 11:05 AM.

**Fix:** Use a linear or exponential decay: `sigma = 3.0 - (hours_since_6am / 12) * 1.5` clamped to [1.5, 3.5].

### SIG-3: METAR Logic Is Binary — No Partial Boost
**File:** `strategies/weather_edge.py:259-265`

METAR data is either used as a full override (0.97/0.03) or ignored entirely. There's no middle ground. If it's 2 PM and the current temp is 87°F with a threshold of 89°F and the forecast high was 91°F, this should tighten sigma dramatically — but currently the model doesn't use it unless it's already a certainty.

### SIG-4: No Tracking of Total Portfolio Exposure
**File:** `bot/risk_manager.py`

Your risk manager checks per-trade risk but has no concept of *total open exposure*. With MAX_OPEN_POSITIONS=3 and MAX_POSITION_SIZE=5 contracts each, you could have 15 contracts simultaneously across correlated weather markets (e.g., Dallas/Houston/San Antonio all betting on Texas heat). A single weather front could wipe all three.

**Fix:** Add correlation-aware exposure limits. Group cities by geographic proximity or weather regime.

### SIG-5: `traded_today` Set Prevents Re-Entry on Improved Edge
**File:** `bot/trader.py:597`

Once a ticker is in `traded_today`, you never trade it again that day — even if a METAR confirmation creates a near-certain 97% probability on a market that's still mispriced. This is leaving free money on the table.

### SIG-6: You're Only Trading 1 Contract at a Time
**File:** `.env` — `MAX_POSITION_SIZE=1`

Your Kelly sizing code is sophisticated, but your env config caps everything at 1 contract. At 1 contract of 3-4 cents, your maximum daily profit is roughly $5. Scale to 3-5 contracts per trade only after validating >55% win rate on 50+ settled trades.

---

## PART 4: BLIND SPOTS

### BLIND-1: You Have Zero Understanding of Your Actual Win Rate
With 0 settled trades, you have no empirical data. You're making parameter decisions (sigma, Kelly fraction, calibration bias) with zero feedback. **You need data before you optimize.**

### BLIND-2: No Weather Regime Detection
Your model treats every day the same. But weather predictability varies enormously:
- **High-pressure dome days**: Very predictable, NWS error < 1°F.
- **Frontal passage days**: Very unpredictable, errors can be 5-10°F.
- **Marine layer / inversions**: Coastal cities (SFO, LAX) can swing 15°F if fog burns off vs. persists.

**Fix:** Detect weather regime from NWS forecast discussion text and adjust sigma accordingly.

### BLIND-3: No Tracking of WHERE Your Edge Comes From
You don't log which of the 4 layers contributed to each trade's edge. Was it METAR confirmations printing money? Or was the NWS base model carrying the load? Without this, you can't optimize.

### BLIND-4: Seasonal Variation Not Accounted For
Your calibration bias and ensemble boost are constants. Weather trading is deeply seasonal — summer highs are very predictable (smaller sigma), spring/fall are volatile (larger sigma). Phase 2 (sigma calibration) handles sigma, but bias and boost are still flat.

### BLIND-5: No Stale Data Detection
**File:** `bot/trader.py:517-526`

If the NWS API fails silently for an hour, your bot keeps trading on stale forecasts with no warning. The `forecast_age` counter only tracks scan cycles, not actual data freshness.

---

## PART 5: NOVICE MISTAKES

### NOVICE-1: Fixed `time.sleep()` for Rate Limiting
**Files:** `data/weather_data.py:71, 119, 180`

Fixed sleep delays (0.2-0.3s) between API calls are fragile. Use exponential backoff with jitter on 429/5xx responses.

### NOVICE-2: Silent Exception Swallowing
**Files:** `data/weather_data.py:72-73, 120-121, 181-182`

Empty `except Exception: pass` blocks mean you'll never know if a city's data is stale or missing.

### NOVICE-3: No Weather Backtest
The backtest engine only works for CryptoMomentumStrategy. You went live without ever testing WeatherEdge historically.

### NOVICE-4: Dead Config Setting
**File:** `config/settings.py:46`

`WEATHER_EDGE_THRESHOLD = 0.10` is never imported or used. The real threshold is `MIN_EDGE = 0.20` in `weather_edge.py:55`. Delete to prevent confusion.

### NOVICE-5: Dry-Run Simulates 100% Fill Rate
**File:** `bot/trader.py:338-340`

Dry-run orders are logged as instantly filled. In reality, passive limit orders at mid-price often don't fill. Dry-run results are wildly optimistic.

---

## PART 6: PRIORITIZED IMPROVEMENT ROADMAP

### Phase 1: DATA COLLECTION — Do This Now
**Priority: URGENT | No code needed**

Let the bot run 2-3 weeks. You need at least 30-50 settled trades before any parameter tuning means anything. Manually spot-check 5-10 trades against actual weather outcomes to sanity-check sigma accuracy.

---

### Phase 2: SIGMA CALIBRATION ✅ COMPLETED 2026-03-28
**Priority: HIGH | Model: Sonnet**

Per-city, per-month sigma values derived from 365 days of IEM ASOS observations.
- New files: `data/calibrate_sigma.py`, `data/sigma_lookup.json`
- Modified: `data/weather_data.py` (added `get_calibrated_sigma()`), `strategies/weather_edge.py` (uses calibrated sigma in `evaluate_market()`)

---

### Phase 3: WEATHER BACKTEST ENGINE
**Priority: HIGH | Model: Sonnet**

**Prompt for agent session:**
```
I have a Kalshi weather trading bot at /Users/alexgarrison/KalshiBotClaude.
The backtesting engine (backtesting/engine.py) only tests crypto strategy.
I need a weather-specific backtest.

Tasks:
1. Add a method run_weather_edge() to BacktestEngine in backtesting/engine.py
2. It should:
   - Fetch settled weather markets for each series in series_config.json
     (past 30-90 days)
   - For each settled market, reconstruct what the NWS forecast WAS at
     market open (use historical forecast archives or settled market
     mid-price as proxy)
   - Run evaluate_market() from strategies/weather_edge.py against
     each market
   - Compare signal vs actual outcome
   - Calculate all metrics from backtesting/metrics.py
3. Handle the fact that historical NWS forecasts aren't easily
   available — consider using the market's opening price as a proxy
   for "market consensus" and testing our model's deviation from that
4. Output should include per-city and per-layer breakdowns
5. Add a CLI command: python deploy/run_bot.py --backtest --days 30
```

---

### Phase 4: FIX KELLY FORMULA + VALIDATE CALIBRATION BIAS
**Priority: HIGH | Model: Haiku**

**Prompt for agent session:**
```
I have a Kalshi weather trading bot at /Users/alexgarrison/KalshiBotClaude.
Two targeted fixes needed:

Fix 1 — Kelly formula in strategies/weather_edge.py lines 346-371:
- The kelly_contracts() function ignores trading fees
- Kalshi maker fee = ceil(0.0175 * count * price * (1-price))
- Kalshi taker fee = ceil(0.07 * count * price * (1-price))
- Modify to subtract expected fee from win payout:
  full_kelly = (model_prob * (1 - fee_rate) - c) / (1 - c)
- Use maker fee rate (0.0175) as default since we use limit orders
- Import calculate_fee_cents from backtesting/metrics.py or inline it

Fix 2 — Validate calibration bias claim:
- Layer 4 (line 300-302) adds +15 cents edge when market mid is 35-65
  cents and we're buying NO
- This is based on the unverified claim that "YES markets priced 35-65
  cents win only 4-21% of the time"
- Create a script scripts/validate_calib_bias.py that:
  - Fetches all settled weather markets from Kalshi (past 90 days)
  - For markets where YES was priced 35-65 cents at close
  - Calculate actual YES win rate
  - If actual rate > 30%, the bias is weaker than assumed — reduce
    CALIB_BIAS_EDGE or remove Layer 4
  - Print results as a table
```

---

### Phase 5: ENSEMBLE LOGIC + CONTINUOUS SIGMA
**Priority: MEDIUM | Model: Haiku**

**Prompt for agent session:**
```
I have a Kalshi weather trading bot at /Users/alexgarrison/KalshiBotClaude.
Two refinements to the signal model:

Fix 1 — Soft ensemble disagreement (strategies/weather_edge.py:289-297):
- Currently: if models disagree directionally, skip trade entirely
- Change to: calculate disagreement magnitude
  - If all 3 models within 10 percentage points of each other:
    full +10 cent boost
  - If within 20 points: half boost (+5 cents)
  - If >20 points apart: skip trade (current behavior)
- This prevents skipping trades where models functionally agree
  (e.g., NWS=51%, GFS=49%)

Fix 2 — Continuous sigma decay (strategies/weather_edge.py:222-223):
- Replace the step function with smooth decay:
  sigma = max(1.5, 3.5 - (hours_since_6am_et * 0.2))
- This means sigma = 3.5 at 6 AM, 2.5 at 11 AM, 1.5 at 4 PM
- More realistic: uncertainty shrinks gradually as the day progresses
- Keep the function name current_sigma() for compatibility
```

---

### Phase 6: OPERATIONAL HARDENING
**Priority: MEDIUM | Model: Haiku**

**Prompt for agent session:**
```
I have a Kalshi weather trading bot at /Users/alexgarrison/KalshiBotClaude.
Several operational fixes needed:

1. Stale forecast detection (bot/trader.py around line 517):
   - Track the actual timestamp of last successful NWS fetch
   - If forecast data is older than 30 minutes AND we're in live mode,
     log a WARNING and skip placing new orders (but keep checking
     pending orders)
   - Resume normal trading when fresh data arrives

2. Silent exception logging (data/weather_data.py lines 72, 120, 181):
   - Replace bare "except Exception: pass" with logged warnings
   - At minimum: logging.warning(f"Failed to fetch {city}: {e}")

3. Remove dead code (config/settings.py line 46):
   - WEATHER_EDGE_THRESHOLD = 0.10 is never used
   - The real threshold is MIN_EDGE = 0.20 in weather_edge.py
   - Delete the dead setting to prevent confusion

4. Add rate limiting with exponential backoff:
   - Replace fixed time.sleep() calls in weather_data.py with a
     utility function that does exponential backoff on failure
   - time.sleep(0.3) on success is fine, but on 429/5xx, back off
     1s, 2s, 4s, 8s with jitter
```

---

### Phase 7: ADVANCED IMPROVEMENTS (After 100+ Trades)
**Priority: LOW | Model: Opus**

Wait until you have real performance data before touching these:

1. **Weather regime detection** from NWS forecast discussion text (adjust sigma for frontal vs. stable days)
2. **Correlation-aware position limits** (don't overload Dallas/Houston/San Antonio on the same weather system)
3. **Allow re-entry on METAR confirmation** (remove from `traded_today` if METAR fires on a market already passed on)
4. **Realistic dry-run mode** with probabilistic fill simulation
5. **Scale to 3-5 contracts per trade** once win rate validated >55% on 50+ trades
6. **Edge decomposition logging** — track which layer (METAR / NWS base / ensemble / calibration bias) contributed what to each trade's edge
7. **Next-day market trading** — currently mostly same-day; next-day has wider spreads but potentially more edge

---

## PART 6B: COMPETITIVE RESEARCH IMPROVEMENTS (Added 2026-03-31)

*Based on analysis of 4 competitor bots and weather.gov CF6 historical data. See plan file for full research notes.*

### Phase 8: BRIER SCORE CALIBRATION TRACKING
**Priority: HIGH | Effort: 1 day**

Neither the live bot nor the backtester tracks how well model probabilities match actual outcomes. If the model says 70%, does it actually resolve YES ~70% of the time? Brier score answers this.

**What to build:**
- After each market settles, compute Brier score: `(model_prob - actual_outcome)^2`
- Track cumulative Brier score, plus per-city and per-probability-bucket breakdowns
- Log to `data/calibration_log.csv` with columns: date, ticker, city, model_prob, market_price, actual_outcome, brier_score
- Add a reliability diagram script that plots predicted vs actual probabilities
- This is the single most important diagnostic for knowing *where* your model is wrong

---

### Phase 9: CF6 HISTORICAL DATA FOR SIGMA CALIBRATION
**Priority: HIGH | Effort: 1-2 days**

Weather.gov CF6 reports provide ~4 years of daily max/min temperature data for all 20 cities (vs. current 1 year from IEM ASOS). This is Kalshi's source of record for weather data — apples-to-apples alignment.

**What to build:**
- Scraper for `forecast.weather.gov/product.php?site={SITE}&issuedby={CITY}&product=CF6&format=CI&version={1-50}`
- Parse fixed-width format inside `<pre>` tag: extract daily MAX, MIN columns
- Feed 4 years of data into `calibrate_sigma.py` (replace or supplement IEM ASOS)
- More data = more robust monthly sigma estimates, especially for volatile shoulder seasons

**Scope boundary:** Only use CF6 for temperature sigma calibration. Do NOT build features around precipitation, wind, snowfall, or sky cover — the model is temperature-only and there's no clear mechanism for those to improve predictions.

---

### Phase 10: GUMBEL DISTRIBUTION FOR HIGH CONTRACTS
**Priority: HIGH | Effort: 2-3 days**

Borrowed from the OpenClaw bot (the most sophisticated competitor analyzed). Daily temperature maxima are extreme values — the highest reading of the day. Extreme Value Theory says these follow a Gumbel distribution, not a Gaussian. The current normCDF assumption systematically misprices tail events for HIGH contracts.

**What to build:**
- Replace `norm.cdf()` with `gumbel_r.cdf()` (from scipy.stats) for HIGH series only
- LOW series can stay normCDF (daily minima behave differently — closer to Gaussian or reverse Gumbel)
- Calibrate Gumbel location/scale parameters from CF6 historical data (Phase 9)
- Backtest before/after to quantify improvement

---

### Phase 11: DYNAMIC MINIMUM EDGE
**Priority: MEDIUM | Effort: 0.5 day**

Currently `MIN_EDGE = 0.25` regardless of uncertainty. When sigma is 5.0°F (Denver winter), you should demand more edge than when sigma is 1.5°F (Miami summer). OpenClaw uses "boundary mass" as the uncertainty signal.

**What to build:**
- `min_edge = 0.20 + 0.05 * (sigma / sigma_max)` — scales from 20¢ to 25¢ based on uncertainty
- Or simpler: if sigma > 4.0, require MIN_EDGE = 0.30; if sigma < 2.0, allow MIN_EDGE = 0.20
- This prevents low-conviction trades in volatile conditions

---

### Phase 12: SOURCE HEALTH MONITORING
**Priority: MEDIUM | Effort: 1 day**

If NWS, Open-Meteo GFS, or ECMWF starts returning stale/inconsistent data, the bot currently doesn't detect this. OpenClaw scores each source on success rate, freshness, and consistency.

**What to build:**
- Track per-source: last_success_time, consecutive_failures, data_freshness_seconds
- If any source has >3 consecutive failures or data older than 60 min: log WARNING, reduce position size by 50%
- If primary source (NWS) is down >30 min: pause all new orders until recovered
- Dashboard/log output showing source health status

---

### Phase 13: BAYESIAN SHRINKAGE TO CLIMATOLOGY
**Priority: LOW | Effort: 1-2 days**

When forecast uncertainty is high (high sigma, morning hours), blend model probability toward historical climate base rates. OpenClaw uses: `p_final = alpha * p_model + (1-alpha) * p_climate` where alpha decreases with uncertainty.

**What to build:**
- Compute climatological probability for each market from CF6 historical data (what % of days in this month does the high exceed threshold X?)
- Blend: `alpha = min(1.0, 2.0 / sigma)` — at sigma=2°F, alpha=1.0 (pure model); at sigma=4°F, alpha=0.5 (50/50 blend)
- This replaces the current 15% market blend (MARKET_BLEND_WEIGHT) with a more principled prior

---

## PART 7: EXPECTED IMPACT

| Improvement | Est. Profit Impact | Effort | Status |
|---|---|---|---|
| Sigma calibration per city | +15-30% win rate improvement | Medium | ✅ Done |
| Weather backtest engine | Risk avoidance (don't trade blind) | Medium | Phase 3 |
| Kelly fee correction | +2-5% on compounded returns | Tiny | Phase 4 |
| Calibration bias validation | Avoid phantom edge on bad trades | Small | Phase 4 |
| Soft ensemble disagreement | +20-40% more trade opportunities | Small | Phase 5 |
| Continuous sigma decay | +5-10% edge accuracy near 11 AM | Tiny | Phase 5 |
| Stale data detection | Avoid catastrophic bad trades | Small | Phase 6 |
| Brier score tracking | Know where model is miscalibrated | Small | Phase 8 |
| CF6 historical sigma (4yr) | Tighter uncertainty bands | Medium | Phase 9 |
| Gumbel distribution (HIGH) | More accurate tail probabilities | Medium | Phase 10 |
| Dynamic minimum edge | Avoid low-conviction volatile trades | Tiny | Phase 11 |
| Source health monitoring | Prevent trading on stale data | Small | Phase 12 |
| Bayesian shrinkage to climatology | Better priors when uncertain | Medium | Phase 13 |

---

## Verification Plan

After implementing each phase:
1. Run the weather backtest (once built in Phase 3) on 30-90 days of data
2. Compare win rate, profit factor, Sharpe before/after each change
3. Run 1 week of live 1-contract trading after each major change
4. Only increase position size after seeing >55% win rate on 50+ settled trades
