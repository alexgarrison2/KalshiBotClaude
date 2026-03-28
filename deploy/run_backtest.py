"""
Run a backtest to test a strategy against historical Kalshi data.

USAGE:
    python deploy/run_backtest.py --strategy crypto_momentum
    python deploy/run_backtest.py --strategy crypto_momentum --days 14

ARGUMENTS:
    --strategy    Which strategy to backtest: "crypto_momentum"
    --days        How many days of historical data to use (default: 7)

WHAT TO LOOK FOR:
    Win rate   > 55%   — more than half of trades should be profitable
    Profit factor > 1.5 — winners should outweigh losers by 50%+
    Max drawdown < 20%  — worst losing streak under 20%
    Sharpe ratio > 1.0  — good risk-adjusted returns

Run this BEFORE running the live bot. If the strategy fails these tests,
do not go live with it.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from dotenv import load_dotenv

load_dotenv()

from data.kalshi_client import KalshiAPIClient
from strategies.crypto_momentum import CryptoMomentumStrategy
from backtesting.engine import BacktestEngine
from backtesting.metrics import print_results

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Kalshi Strategy Backtester")
    parser.add_argument(
        "--strategy",
        choices=["crypto_momentum"],
        default="crypto_momentum",
        help="Strategy to backtest",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Days of history to test against (default: 7)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=1,
        help="Simulated trade size in contracts (default: 1)",
    )

    args = parser.parse_args()

    console.print(f"[bold cyan]Backtesting {args.strategy} — {args.days} days[/bold cyan]")

    client = KalshiAPIClient()

    if args.strategy == "crypto_momentum":
        strategy = CryptoMomentumStrategy()
        engine = BacktestEngine(client)
        results = engine.run_crypto_momentum(
            strategy=strategy,
            series_ticker="KXBTC",
            days_back=args.days,
            trade_size=args.size,
        )
        print_results(results)
    else:
        console.print(f"[yellow]Backtest for {args.strategy} not yet implemented.[/yellow]")
        console.print("Weather edge strategy requires live market data for accurate backtesting.")


if __name__ == "__main__":
    main()
