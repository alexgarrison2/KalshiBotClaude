"""
Validate the calibration bias claim (Layer 4).

Layer 4 of the weather edge strategy applies a +15¢ edge credit when:
  - Market mid is 35–65¢
  - We are buying NO (model thinks YES is overpriced)

This is based on the claim that "YES markets priced 35–65¢ win only
4–21% of the time on Kalshi weather markets."

This script tests that claim against recent settled markets.

USAGE:
    python scripts/validate_calib_bias.py
    python scripts/validate_calib_bias.py --days 90
"""
import os
import sys
import time
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from data.kalshi_client import KalshiAPIClient
from data.weather_data import SERIES_CONFIG

console = Console()

# Bins to test — each is (low, high) in cents
PRICE_BINS = [
    (10, 20),
    (20, 30),
    (30, 40),
    (35, 65),   # ← the exact range Layer 4 uses
    (40, 50),
    (50, 60),
    (60, 70),
    (70, 80),
    (80, 90),
]


def main():
    parser = argparse.ArgumentParser(description="Validate Layer 4 calibration bias")
    parser.add_argument("--days", type=int, default=30, help="Days of history to check")
    args = parser.parse_args()

    client = KalshiAPIClient()
    end_ts   = int(time.time())
    start_ts = end_ts - args.days * 86400

    console.print(f"\n[bold]Layer 4 Calibration Bias Validation[/bold]")
    console.print(f"Checking {args.days} days of settled weather markets\n")

    # Collect all (opening_mid_cents, resolved_yes) pairs
    observations = []  # list of (mid_cents, resolved_yes, city, series)

    for series in SERIES_CONFIG:
        city = SERIES_CONFIG[series]["city"]
        try:
            markets, _ = client.get_markets(
                status="settled",
                series_ticker=series,
                min_close_ts=start_ts,
                max_close_ts=end_ts,
                limit=200,
            )
        except Exception as e:
            console.print(f"[dim red]{series}: {e}[/dim red]")
            continue

        if not markets:
            continue

        for market in markets:
            ticker = market.get("ticker", "")
            result = market.get("result", "")
            if not ticker or not result:
                continue

            resolved_yes = str(result).lower() == "yes"

            # Get the opening candlestick (first hourly candle of the trading day)
            try:
                close_str = market.get("close_time") or market.get("expiration_time")
                if not close_str:
                    continue
                close_dt  = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                close_ts  = int(close_dt.timestamp())
                # Opening = ~8 hours before close (market opens ~8AM ET, closes ~4PM ET)
                open_ts   = close_ts - 8 * 3600

                candles = client.get_candlesticks(
                    series_ticker   = series,
                    market_ticker   = ticker,
                    start_ts        = open_ts,
                    end_ts          = open_ts + 2 * 3600,  # first 2 hours only
                    period_interval = 60,
                )
            except Exception:
                continue

            if not candles:
                continue

            # Use the first available candle as opening price
            first = candles[0]
            bid_obj = first.get("yes_bid", {})
            ask_obj = first.get("yes_ask", {})
            bid = bid_obj.get("close_dollars") if isinstance(bid_obj, dict) else None
            ask = ask_obj.get("close_dollars") if isinstance(ask_obj, dict) else None
            if bid is None or ask is None:
                continue

            mid_cents = round((float(bid) + float(ask)) / 2 * 100)
            if mid_cents < 1 or mid_cents > 99:
                continue

            observations.append((mid_cents, resolved_yes, city, series))

        time.sleep(0.2)

    if not observations:
        console.print("[yellow]No data collected.[/yellow]")
        return

    console.print(f"Collected [bold]{len(observations)}[/bold] market observations\n")

    # ── Per-bin analysis ──────────────────────────────────────────────────────
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Price Range",     style="white")
    table.add_column("Markets",         justify="right", style="yellow")
    table.add_column("YES Wins",        justify="right", style="yellow")
    table.add_column("Actual Win%",     justify="right")
    table.add_column("Implied Win%",    justify="right", style="dim")
    table.add_column("Edge vs Implied", justify="right")
    table.add_column("Layer 4 Range?",  justify="center")

    for lo, hi in PRICE_BINS:
        subset = [(m, r) for (m, r, c, s) in observations if lo <= m < hi]
        if not subset:
            continue

        count     = len(subset)
        yes_wins  = sum(1 for _, r in subset if r)
        actual_pct = yes_wins / count * 100
        implied_pct = (lo + hi) / 2  # midpoint of range

        edge = actual_pct - implied_pct
        edge_str = f"{edge:+.1f}pp"
        edge_color = "red" if edge > -5 else "green"  # green = YES is overpriced (Layer 4 valid)

        actual_color = "green" if actual_pct < implied_pct - 5 else "red"
        is_layer4 = "✓" if lo == 35 and hi == 65 else ""

        table.add_row(
            f"{lo}–{hi}¢",
            str(count),
            str(yes_wins),
            f"[{actual_color}]{actual_pct:.1f}%[/{actual_color}]",
            f"{implied_pct:.0f}%",
            f"[{edge_color}]{edge_str}[/{edge_color}]",
            f"[bold cyan]{is_layer4}[/bold cyan]",
        )

    console.print(table)

    # ── Layer 4 verdict ───────────────────────────────────────────────────────
    layer4 = [(m, r) for (m, r, c, s) in observations if 35 <= m < 65]
    if layer4:
        count     = len(layer4)
        yes_wins  = sum(1 for _, r in layer4 if r)
        actual_pct = yes_wins / count * 100
        console.print()
        if actual_pct < 30:
            console.print(
                f"[bold green]✓ Layer 4 VALIDATED[/bold green] — "
                f"YES wins only {actual_pct:.1f}% in the 35–65¢ range "
                f"({count} markets). Claim holds: +15¢ NO edge credit is justified."
            )
        elif actual_pct < 45:
            console.print(
                f"[bold yellow]⚠ Layer 4 WEAKLY supported[/bold yellow] — "
                f"YES wins {actual_pct:.1f}% in the 35–65¢ range "
                f"({count} markets). Consider reducing CALIB_BIAS_EDGE from 15¢ to ~8¢."
            )
        else:
            console.print(
                f"[bold red]✗ Layer 4 INVALIDATED[/bold red] — "
                f"YES wins {actual_pct:.1f}% in the 35–65¢ range "
                f"({count} markets). The bias claim does not hold. "
                f"Recommend removing Layer 4 or setting CALIB_BIAS_EDGE = 0."
            )
    console.print()


if __name__ == "__main__":
    main()
