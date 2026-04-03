"""
Backtesting Engine — tests a strategy against historical data.

WHAT BACKTESTING IS:
Instead of trading with real money to see if a strategy works,
we "replay" historical market data and simulate what would have
happened if the bot was running during that period.

Think of it like watching a sports game replay and testing whether
your betting system would have been profitable.

IMPORTANT LIMITATION:
Backtests use past data. Past performance does NOT guarantee future results.
But a strategy that performs well on historical data is much better
than one with no testing at all.

HOW TO INTERPRET RESULTS:
- Run the backtest, check the metrics in metrics.py
- Win rate > 55%, profit factor > 1.5, max drawdown < 20% → good sign
- Then run the bot live with $1 trades and compare results to backtest
"""
import time
import pandas as pd
from datetime import datetime, timezone, date
from typing import List, Optional
from zoneinfo import ZoneInfo
from rich.console import Console
from rich.progress import Progress

from data.kalshi_client import KalshiAPIClient
from data.price_data import candlesticks_to_dataframe
from strategies.base_strategy import BaseStrategy, Signal
from strategies.crypto_momentum import CryptoMomentumStrategy
from backtesting.metrics import BacktestResults, Trade, WeatherTrade, calculate_fee_cents

ET = ZoneInfo("America/New_York")

console = Console()


class BacktestEngine:
    """
    Runs a strategy against historical Kalshi data and calculates performance metrics.
    """

    def __init__(self, client: KalshiAPIClient):
        self.client = client

    def run_crypto_momentum(
        self,
        strategy: CryptoMomentumStrategy,
        series_ticker: str,
        days_back: int = 7,
        candle_interval: int = 1,
        trade_size: int = 1,
    ) -> BacktestResults:
        """
        Backtest the crypto momentum strategy against historical candle data.

        The simulation:
        1. Get all available historical candles
        2. Walk forward through time, candle by candle
        3. At each point, ask the strategy: "what would you do?"
        4. Simulate the trade if the signal says BUY
        5. Check if the trade would have won or lost based on what actually happened

        Args:
            strategy: The CryptoMomentumStrategy instance
            series_ticker: Kalshi series (e.g., "KXBTC")
            days_back: How many days of history to test on
            candle_interval: Candle size ("5m", "1h")
            trade_size: Number of contracts per trade

        Returns:
            BacktestResults with all simulated trades
        """
        console.print(f"\n[bold]Running backtest: {strategy.name}[/bold]")
        console.print(f"Series: {series_ticker} | {days_back} days | {candle_interval} candles")

        end_ts = int(time.time())
        start_ts = end_ts - (days_back * 24 * 3600)

        # Get historical data (settled markets give us the resolution outcome)
        markets, _ = self.client.get_markets(
            status="settled",
            series_ticker=series_ticker,
            min_close_ts=start_ts,
            max_close_ts=end_ts,
            limit=500,
        )

        if not markets:
            console.print(f"[yellow]No settled markets found for {series_ticker}[/yellow]")
            return BacktestResults(strategy_name=strategy.name)

        console.print(f"Found {len(markets)} settled markets to test against")

        results = BacktestResults(strategy_name=strategy.name)
        tested = 0

        with Progress() as progress:
            task = progress.add_task("Backtesting...", total=len(markets))

            for market in markets:
                # Markets are now plain dicts
                ticker = market.get("ticker")
                if not ticker:
                    progress.advance(task)
                    continue

                # Get candlestick data for this market
                try:
                    close_str = market.get("close_time") or market.get("expiration_time")
                    if close_str is None:
                        progress.advance(task)
                        continue

                    if isinstance(close_str, str):
                        from datetime import datetime
                        close_ts_int = int(datetime.fromisoformat(close_str.replace("Z", "+00:00")).timestamp())
                    else:
                        close_ts_int = int(close_str)

                    # Get candles for the 2 hours leading up to this market's close
                    market_start_ts = close_ts_int - (2 * 3600)

                    candles = self.client.get_candlesticks(
                        series_ticker=series_ticker,
                        market_ticker=ticker,
                        start_ts=market_start_ts,
                        end_ts=close_ts_int,
                        period_interval=candle_interval,
                    )

                    if not candles or len(candles) < strategy.min_candles:
                        progress.advance(task)
                        continue

                    df = candlesticks_to_dataframe(candles)
                    if df.empty or len(df) < strategy.min_candles:
                        progress.advance(task)
                        continue

                    # Skip flat/dead markets — no price variation means no signal is possible
                    if df["close"].std() < 0.5:
                        progress.advance(task)
                        continue

                    # Skip near-resolved markets — if the YES price is already below 15¢ or
                    # above 85¢ the outcome is largely decided; MACD/RSI will misread that
                    # trend as a trading opportunity rather than a settled probability
                    last_close = df["close"].iloc[-2] if len(df) >= 2 else df["close"].iloc[-1]
                    if last_close < 15 or last_close > 85:
                        progress.advance(task)
                        continue

                    # Use all but the last candle for signal generation
                    # (we can't use the last candle because that would be "peeking into the future")
                    signal_df = df.iloc[:-1]
                    last_price = int(df["close"].iloc[-2]) if len(df) >= 2 else 50

                    signal = strategy.generate_signal(
                        df=signal_df,
                        ticker=ticker,
                        current_yes_price=last_price,
                        count=trade_size,
                    )

                    if signal.signal == Signal.HOLD:
                        progress.advance(task)
                        continue

                    # Determine outcome: did the market resolve YES or NO?
                    result = market.get("result") or market.get("resolution")

                    if result is None:
                        progress.advance(task)
                        continue

                    # Normalize result to "yes" or "no"
                    resolved_yes = str(result).lower() in ["yes", "true", "1"]

                    # Did our signal match the outcome?
                    if signal.signal == Signal.BUY_YES:
                        won = resolved_yes
                        entry_price = signal.price
                        side = "yes"
                    else:  # BUY_NO
                        won = not resolved_yes
                        entry_price = signal.price
                        side = "no"

                    fee = calculate_fee_cents(entry_price, trade_size, is_maker=True)
                    exit_price = 100 if won else 0

                    trade = Trade(
                        ticker=ticker,
                        side=side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        count=trade_size,
                        fee_cents=fee,
                        won=won,
                        reason=signal.reason,
                    )
                    results.trades.append(trade)
                    tested += 1

                except Exception as e:
                    console.print(f"[dim red]Error testing {ticker}: {e}[/dim red]")

                progress.advance(task)

        console.print(f"Simulated {tested} trades out of {len(markets)} markets")
        return results

    def run_weather_edge(
        self,
        days_back: int = 30,
        sample_hours: Optional[List[int]] = None,
        trade_size: int = 1,
    ) -> BacktestResults:
        """
        Backtest the WeatherEdge METAR confirmation signal against historical data.

        Signal tested: METAR intraday confirmation (Layer 1)
          - For each settled market, walk through hourly IEM observations
          - When observed temp confirms the outcome (obs > threshold for HIGH
            markets, obs < threshold for LOW markets) AND the market is still
            mispriced, simulate a BUY YES trade at the current mid price
          - Only one trade per market (first confirmation hour wins)

        Ground truth: Kalshi settled market result field ("yes" / "no")
        Market prices: Kalshi hourly candlesticks (period_interval=60)
        Intraday obs:  Iowa Environmental Mesonet (IEM) ASOS hourly temps

        Args:
            days_back:    Days of history to replay (default 30)
            sample_hours: ET hours to check for signals (default 7–15 inclusive)
            trade_size:   Contracts per simulated trade (default 1)

        Returns:
            BacktestResults containing WeatherTrade objects; pass to
            print_weather_results() for the full breakdown.
        """
        from data.weather_data import SERIES_CONFIG
        from data.calibrate_sigma import fetch_hourly_obs
        from strategies.weather_edge import _parse_threshold, _parse_event_date, MIN_EDGE

        METAR_CERTAIN = 0.97

        if sample_hours is None:
            sample_hours = list(range(7, 16))  # 7 AM – 3 PM ET

        end_ts   = int(time.time())
        start_ts = end_ts - days_back * 86400

        console.print(f"\n[bold]WeatherEdge METAR Backtest[/bold]")
        console.print(f"Signal: METAR confirmation only (Layer 1)")
        console.print(f"Period: {days_back} days  |  Hours: {sample_hours[0]}–{sample_hours[-1]} ET\n")

        # ── Step 1: Download IEM hourly obs once per station ──────────────────
        console.print("[dim]Downloading IEM historical observations...[/dim]")
        station_obs_raw: dict = {}  # station → raw obs list

        seen_stations: set = set()
        for series, cfg in SERIES_CONFIG.items():
            station = cfg.get("station", "")
            if not station or station in seen_stations:
                continue
            seen_stations.add(station)
            city = cfg.get("city", series)
            try:
                console.print(f"  {city:20s} ({station}) ...", end=" ")
                obs = fetch_hourly_obs(station, days_back + 2)
                station_obs_raw[station] = obs
                console.print(f"[green]{len(obs)} obs[/green]")
                time.sleep(0.3)
            except Exception as e:
                console.print(f"[red]FAILED — {e}[/red]")
                station_obs_raw[station] = []

        # ── Step 2: Build ET hourly lookup ────────────────────────────────────
        # station → {et_date_str → {et_hour → avg_temp_f}}
        station_hourly: dict = {}
        for station, obs_list in station_obs_raw.items():
            by_slot: dict = {}
            for o in obs_list:
                try:
                    utc_dt = datetime(
                        int(o["date"][:4]), int(o["date"][5:7]), int(o["date"][8:10]),
                        o["hour"], 0, 0, tzinfo=timezone.utc,
                    )
                    et_dt    = utc_dt.astimezone(ET)
                    date_str = et_dt.date().isoformat()
                    hour     = et_dt.hour
                    key      = (date_str, hour)
                    by_slot.setdefault(key, []).append(o["tmpf"])
                except Exception:
                    continue

            # Average temps per (date, hour) slot
            lookup: dict = {}
            for (date_str, hour), temps in by_slot.items():
                lookup.setdefault(date_str, {})[hour] = sum(temps) / len(temps)
            station_hourly[station] = lookup

        # ── Step 3: Backtest each series ──────────────────────────────────────
        results = BacktestResults(strategy_name="WeatherEdge METAR Backtest")
        total_markets = 0
        tested        = 0

        console.print()

        for series, cfg in SERIES_CONFIG.items():
            station   = cfg.get("station", "")
            temp_type = cfg.get("temp_type", "high")
            city      = cfg.get("city", series)

            daily_obs = station_hourly.get(station, {})

            # Fetch settled markets for this series
            try:
                markets, _ = self.client.get_markets(
                    status="settled",
                    series_ticker=series,
                    min_close_ts=start_ts,
                    max_close_ts=end_ts,
                    limit=200,
                )
            except Exception as e:
                console.print(f"[dim red]{series}: market fetch failed — {e}[/dim red]")
                continue

            if not markets:
                continue

            total_markets += len(markets)

            with Progress() as progress:
                task = progress.add_task(f"{city:20s} ({len(markets)} markets)", total=len(markets))

                for market in markets:
                    progress.advance(task)

                    ticker = market.get("ticker", "")
                    result = market.get("result", "")
                    if not ticker or not result:
                        continue

                    resolved_yes  = str(result).lower() == "yes"
                    threshold     = _parse_threshold(market)
                    event_date    = _parse_event_date(ticker)
                    strike_type   = market.get("strike_type", "greater")

                    if threshold == 0 or event_date is None:
                        continue

                    date_str   = event_date.isoformat()
                    obs_by_hour = daily_obs.get(date_str, {})
                    if not obs_by_hour:
                        continue

                    # Get hourly candlesticks for this market
                    try:
                        close_str = market.get("close_time") or market.get("expiration_time")
                        if not close_str:
                            continue
                        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                        close_ts_int = int(close_dt.timestamp())

                        # Day starts at 7 AM ET
                        from datetime import time as dtime
                        day_start_et  = datetime.combine(event_date, dtime(7, 0), tzinfo=ET)
                        day_start_ts  = int(day_start_et.timestamp())

                        candles = self.client.get_candlesticks(
                            series_ticker  = series,
                            market_ticker  = ticker,
                            start_ts       = day_start_ts,
                            end_ts         = close_ts_int,
                            period_interval= 60,
                        )
                    except Exception:
                        continue

                    if not candles:
                        continue

                    # Walk through each hourly candle
                    for candle in candles:
                        end_period_ts = candle.get("end_period_ts")
                        if not end_period_ts:
                            continue

                        candle_dt   = datetime.fromtimestamp(end_period_ts, tz=timezone.utc).astimezone(ET)
                        candle_hour = candle_dt.hour

                        if candle_hour not in sample_hours:
                            continue

                        # Max observed temp so far today (for HIGH contracts)
                        # Min observed temp so far today (for LOW contracts)
                        obs_so_far = [
                            obs_by_hour[h]
                            for h in range(0, candle_hour + 1)
                            if h in obs_by_hour
                        ]
                        if not obs_so_far:
                            continue

                        max_obs = max(obs_so_far)
                        min_obs = min(obs_so_far)

                        # Check METAR confirmation
                        if temp_type == "high" and strike_type == "greater":
                            if max_obs <= threshold:
                                continue  # not confirmed yet
                            confirmed_temp  = max_obs
                            signal_prob     = METAR_CERTAIN
                            signal_source   = "METAR↑"
                        elif temp_type == "low" and strike_type == "less":
                            if min_obs >= threshold:
                                continue  # not confirmed yet
                            confirmed_temp  = min_obs
                            signal_prob     = METAR_CERTAIN
                            signal_source   = "METAR↓"
                        else:
                            continue

                        # Get market mid price from this candle
                        bid_obj = candle.get("yes_bid", {})
                        ask_obj = candle.get("yes_ask", {})
                        bid_close = bid_obj.get("close_dollars") if isinstance(bid_obj, dict) else None
                        ask_close = ask_obj.get("close_dollars") if isinstance(ask_obj, dict) else None

                        if bid_close is None or ask_close is None:
                            continue

                        market_mid  = (float(bid_close) + float(ask_close)) / 2.0
                        entry_cents = round(market_mid * 100)

                        if entry_cents < 1 or entry_cents > 99:
                            continue

                        edge = signal_prob - market_mid
                        if edge < MIN_EDGE:
                            continue  # market already priced in the confirmation

                        fee = calculate_fee_cents(entry_cents, trade_size, is_maker=True)

                        trade = WeatherTrade(
                            ticker          = ticker,
                            side            = "yes",
                            entry_price     = entry_cents,
                            exit_price      = 100 if resolved_yes else 0,
                            count           = trade_size,
                            fee_cents       = fee,
                            won             = resolved_yes,
                            reason          = (
                                f"{signal_source}: obs={confirmed_temp:.1f}°F "
                                f"vs threshold={threshold:.0f}°F  "
                                f"mid={market_mid*100:.0f}¢  edge={edge*100:.0f}¢"
                            ),
                            city            = city,
                            series          = series,
                            trade_date      = date_str,
                            hour_et         = candle_hour,
                            threshold       = threshold,
                            obs_temp        = confirmed_temp,
                            model_prob_val  = signal_prob,
                            market_mid      = market_mid,
                            signal_source   = signal_source,
                        )
                        results.trades.append(trade)
                        tested += 1
                        break  # one trade per market (first confirmation hour wins)

        console.print(
            f"\nScanned {total_markets} settled markets → "
            f"[bold]{tested} METAR confirmation trades[/bold]"
        )
        return results
