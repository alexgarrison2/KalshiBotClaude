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
from typing import List
from rich.console import Console
from rich.progress import Progress

from data.kalshi_client import KalshiAPIClient
from data.price_data import candlesticks_to_dataframe
from strategies.base_strategy import BaseStrategy, Signal
from strategies.crypto_momentum import CryptoMomentumStrategy
from backtesting.metrics import BacktestResults, Trade, calculate_fee_cents

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
