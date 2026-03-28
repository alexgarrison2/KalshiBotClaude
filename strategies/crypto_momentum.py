"""
Crypto Hourly Momentum Strategy — MACD + RSI

This strategy trades Bitcoin/Ethereum hourly contracts on Kalshi
based on price momentum indicators.

HOW IT WORKS:
1. Fetches recent candlestick data for a crypto hourly market
2. Calculates two indicators:
   - MACD (3/15/3): catches when momentum is shifting direction
   - RSI (14): measures if the market is overbought or oversold
3. If both indicators agree on direction → place a limit order
4. Always uses limit orders (4x cheaper fees than market orders)

MACD EXPLAINED (simply):
The MACD tracks the difference between a "fast" and "slow" price average.
When the fast average crosses ABOVE the slow average → upward momentum → buy YES
When the fast average crosses BELOW the slow average → downward momentum → buy NO

RSI EXPLAINED (simply):
RSI measures recent price changes on a 0-100 scale.
- Below 30 = "oversold" → likely to bounce up → buy YES
- Above 70 = "overbought" → likely to fall → buy NO
- 30-70 = neutral → HOLD
"""
import pandas as pd
import numpy as np
from ta.trend import MACD
from ta.momentum import RSIIndicator
from rich.console import Console

from strategies.base_strategy import BaseStrategy, TradeSignal, Signal
from config.settings import (
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
)

console = Console()


class CryptoMomentumStrategy(BaseStrategy):
    """
    MACD + RSI momentum strategy for Kalshi's crypto hourly markets.

    Generates BUY_YES when momentum is bullish (price going up).
    Generates BUY_NO when momentum is bearish (price going down).
    Holds when signals are mixed or weak.
    """

    def __init__(
        self,
        macd_fast: int = MACD_FAST,
        macd_slow: int = MACD_SLOW,
        macd_signal: int = MACD_SIGNAL,
        rsi_period: int = RSI_PERIOD,
        rsi_oversold: float = RSI_OVERSOLD,
        rsi_overbought: float = RSI_OVERBOUGHT,
        min_candles: int = 20,
    ):
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.min_candles = min_candles

    @property
    def name(self) -> str:
        return f"CryptoMomentum(MACD {self.macd_fast}/{self.macd_slow}/{self.macd_signal}, RSI {self.rsi_period})"

    def _calculate_indicators(self, df: pd.DataFrame) -> dict:
        """
        Calculate MACD and RSI for the given price data.

        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume

        Returns:
            Dictionary with indicator values (or None if not enough data)
        """
        if len(df) < self.min_candles:
            return {}

        close = df["close"].astype(float)

        # MACD — uses closing prices to find momentum shifts
        macd_indicator = MACD(
            close=close,
            window_fast=self.macd_fast,
            window_slow=self.macd_slow,
            window_sign=self.macd_signal,
        )
        macd_line = macd_indicator.macd()
        signal_line = macd_indicator.macd_signal()
        histogram = macd_indicator.macd_diff()   # positive = bullish, negative = bearish

        # RSI — measures momentum on a 0-100 scale
        rsi_indicator = RSIIndicator(close=close, window=self.rsi_period)
        rsi = rsi_indicator.rsi()

        # We want the last two values of the histogram to detect a crossover
        # (when the histogram changes from negative to positive or vice versa)
        last_idx = len(histogram) - 1
        prev_idx = last_idx - 1

        return {
            "macd_current": float(histogram.iloc[last_idx]) if not pd.isna(histogram.iloc[last_idx]) else None,
            "macd_previous": float(histogram.iloc[prev_idx]) if prev_idx >= 0 and not pd.isna(histogram.iloc[prev_idx]) else None,
            "macd_line": float(macd_line.iloc[last_idx]) if not pd.isna(macd_line.iloc[last_idx]) else None,
            "signal_line": float(signal_line.iloc[last_idx]) if not pd.isna(signal_line.iloc[last_idx]) else None,
            "rsi": float(rsi.iloc[last_idx]) if not pd.isna(rsi.iloc[last_idx]) else None,
            "close": float(close.iloc[last_idx]),
        }

    def generate_signal(
        self,
        df: pd.DataFrame,
        ticker: str,
        current_yes_price: int,
        count: int = 1,
        **kwargs,
    ) -> TradeSignal:
        """
        Analyze price data and decide whether to buy YES, buy NO, or hold.

        Args:
            df: Candlestick price data (from price_data.py)
            ticker: Kalshi market ticker to trade
            current_yes_price: Current price of YES contracts (1-99 cents)
            count: Number of contracts to buy

        Returns:
            TradeSignal with buy/hold decision
        """
        indicators = self._calculate_indicators(df)

        if not indicators:
            return TradeSignal(
                signal=Signal.HOLD,
                ticker=ticker,
                price=current_yes_price,
                count=count,
                confidence=0.0,
                reason=f"Not enough data (need {self.min_candles} candles, have {len(df)})",
            )

        macd_curr = indicators.get("macd_current")
        macd_prev = indicators.get("macd_previous")
        rsi = indicators.get("rsi")

        if macd_curr is None or rsi is None:
            return TradeSignal(
                signal=Signal.HOLD,
                ticker=ticker,
                price=current_yes_price,
                count=count,
                confidence=0.0,
                reason="Indicator calculation failed",
            )

        # ── MACD Signal ─────────────────────────────────────────────
        # Bullish crossover: histogram goes from negative to positive
        macd_bullish = (macd_prev is not None and macd_prev < 0 and macd_curr > 0)
        # Bearish crossover: histogram goes from positive to negative
        macd_bearish = (macd_prev is not None and macd_prev > 0 and macd_curr < 0)

        # ── RSI Signal ───────────────────────────────────────────────
        rsi_oversold = rsi < self.rsi_oversold      # Likely to go up
        rsi_overbought = rsi > self.rsi_overbought   # Likely to go down

        # ── Combine Signals ──────────────────────────────────────────
        bullish = macd_bullish or (macd_curr > 0 and rsi_oversold)
        bearish = macd_bearish or (macd_curr < 0 and rsi_overbought)

        # Calculate confidence based on how many signals agree
        if bullish and not bearish:
            agreement_count = sum([macd_bullish, rsi_oversold])
            confidence = 0.5 + (agreement_count * 0.2)
            reason = (
                f"Bullish: MACD {'crossover ✓' if macd_bullish else 'positive'}, "
                f"RSI={rsi:.1f} {'(oversold ✓)' if rsi_oversold else ''}"
            )
            console.print(f"[green]BUY YES signal: {reason}[/green]")
            return TradeSignal(
                signal=Signal.BUY_YES,
                ticker=ticker,
                price=max(1, min(99, current_yes_price)),
                count=count,
                confidence=min(0.9, confidence),
                reason=reason,
            )

        elif bearish and not bullish:
            agreement_count = sum([macd_bearish, rsi_overbought])
            confidence = 0.5 + (agreement_count * 0.2)
            no_price = 100 - current_yes_price
            reason = (
                f"Bearish: MACD {'crossover ✓' if macd_bearish else 'negative'}, "
                f"RSI={rsi:.1f} {'(overbought ✓)' if rsi_overbought else ''}"
            )
            console.print(f"[red]BUY NO signal: {reason}[/red]")
            return TradeSignal(
                signal=Signal.BUY_NO,
                ticker=ticker,
                price=max(1, min(99, no_price)),
                count=count,
                confidence=min(0.9, confidence),
                reason=reason,
            )

        else:
            console.print(
                f"[dim]HOLD: MACD={macd_curr:.4f}, RSI={rsi:.1f} — no clear signal[/dim]"
            )
            return TradeSignal(
                signal=Signal.HOLD,
                ticker=ticker,
                price=current_yes_price,
                count=count,
                confidence=0.0,
                reason=f"Mixed signals: MACD={macd_curr:.4f}, RSI={rsi:.1f}",
            )
