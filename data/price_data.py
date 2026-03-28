"""
Price Data — converts raw Kalshi candlestick data into a format
our strategies can use (pandas DataFrames).

Think of this as the "translator" between Kalshi's raw data and
our strategy calculations.
"""
import time
import pandas as pd
from typing import Optional
from rich.console import Console

from data.kalshi_client import KalshiAPIClient

console = Console()


def _dollars_to_cents(val) -> float:
    """Convert a dollar string like '0.4500' to cents (45.0)."""
    try:
        return float(val) * 100
    except (TypeError, ValueError):
        return 0.0


def candlesticks_to_dataframe(candlesticks: list) -> pd.DataFrame:
    """
    Convert Kalshi candlestick objects into a pandas DataFrame.

    Kalshi returns candles with yes_bid and yes_ask objects containing
    open_dollars, high_dollars, low_dollars, close_dollars fields.
    We use the mid-price (average of bid and ask) for OHLC values.

    This format is what our technical indicators (MACD, RSI) expect.
    """
    if not candlesticks:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    rows = []
    for c in candlesticks:
        bid = c.get("yes_bid") or {}
        ask = c.get("yes_ask") or {}

        def mid(field):
            b = _dollars_to_cents(bid.get(field))
            a = _dollars_to_cents(ask.get(field))
            if a and b:
                return (a + b) / 2
            return a or b or 0.0

        rows.append(
            {
                "timestamp": c.get("end_period_ts") or c.get("period_end_ts") or c.get("ts", 0),
                "open":   mid("open_dollars"),
                "high":   mid("high_dollars"),
                "low":    mid("low_dollars"),
                "close":  mid("close_dollars"),
                "volume": float(c.get("volume_fp") or c.get("volume") or 0),
            }
        )

    df = pd.DataFrame(rows)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


def get_market_price_data(
    client: KalshiAPIClient,
    series_ticker: str,
    market_ticker: str,
    lookback_minutes: int = 120,
    interval: str = "5m",
) -> pd.DataFrame:
    """
    Get price data for a market as a ready-to-use DataFrame.

    Args:
        client: Your Kalshi API client
        series_ticker: Series (e.g., "KXBTC")
        market_ticker: Specific market ticker
        lookback_minutes: How far back to look (default 2 hours)
        interval: Candle size (default 5 minutes)

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    end_ts = int(time.time())
    start_ts = end_ts - (lookback_minutes * 60)

    candlesticks = client.get_candlesticks(
        series_ticker=series_ticker,
        market_ticker=market_ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=interval,
    )

    df = candlesticks_to_dataframe(candlesticks)

    if len(df) > 0:
        console.print(
            f"[dim]Got {len(df)} candles for {market_ticker} "
            f"({interval} interval)[/dim]"
        )
    else:
        console.print(f"[yellow]No candle data for {market_ticker}[/yellow]")

    return df
