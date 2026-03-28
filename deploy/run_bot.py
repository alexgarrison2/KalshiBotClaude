"""
Run the trading bot.

USAGE:
    # Weather strategy (primary — proven approach from Taylor's bot)
    python deploy/run_bot.py --strategy weather_edge             # dry-run (safe, default)
    python deploy/run_bot.py --strategy weather_edge --live      # LIVE — real money!
    python deploy/run_bot.py --strategy weather_edge --live --once   # one scan and exit

    # Crypto momentum strategy (experimental)
    python deploy/run_bot.py --strategy crypto_momentum

ARGUMENTS:
    --strategy    "weather_edge" (default) or "crypto_momentum"
    --live        Place real orders. Omit for dry-run (prints signals, no trades).
    --once        Run one scan and exit (useful for testing)
    --size        Contracts per trade (default: 1)
    --poll        Seconds between scans (default: 30)
    --hours       Stop after N hours (default: run until Ctrl+C)
    --backtest    Run weather METAR backtest against historical data and exit
    --days        Days of history to use for backtest (default: 30)

START HERE:
    1. Run dry-run first to see signals without risking money:
       python deploy/run_bot.py --strategy weather_edge
    2. If signals look sane, go live with size 1:
       python deploy/run_bot.py --strategy weather_edge --live --size 1
    3. Check logs/trades_YYYY-MM-DD.log for trade history
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from dotenv import load_dotenv

load_dotenv()

from data.kalshi_client import KalshiAPIClient
from bot.trader import WeatherTrader, Trader

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--strategy",
        choices=["weather_edge", "crypto_momentum"],
        default="weather_edge",
        help="Which strategy to run (default: weather_edge)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Place real orders. Omit to run in dry-run mode (safe).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan and exit.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=1,
        help="Contracts per trade (default: 1)",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=30,
        help="Seconds between scans (default: 30)",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Stop after N hours (default: run until Ctrl+C)",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run weather backtest (METAR layer) against historical data and exit.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days of history to replay in backtest (default: 30)",
    )

    args = parser.parse_args()

    # Validate credentials
    key_id   = os.getenv("KALSHI_API_KEY_ID", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_id or not key_path:
        console.print("[bold red]ERROR: Missing Kalshi API credentials[/bold red]")
        console.print("  Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiAPIClient()

    try:
        balance = client.get_balance()
        console.print(f"[green]Connected — account balance: ${balance:.2f}[/green]")
    except Exception as e:
        console.print(f"[red]Could not connect: {e}[/red]")
        sys.exit(1)

    dry_run = not args.live
    if dry_run:
        console.print(
            "[yellow]DRY-RUN mode — signals will be shown but no orders placed.[/yellow]\n"
            "[yellow]Pass --live to trade with real money.[/yellow]"
        )
    else:
        console.print("[bold red]LIVE TRADING — real orders will be placed![/bold red]")

    if args.backtest:
        from backtesting.engine import BacktestEngine
        from backtesting.metrics import print_weather_results
        engine = BacktestEngine(client)
        results = engine.run_weather_edge(days_back=args.days, trade_size=args.size)
        print_weather_results(results)
        sys.exit(0)

    if args.strategy == "weather_edge":
        trader = WeatherTrader(
            client       = client,
            dry_run      = dry_run,
            trade_size   = args.size,
            poll_interval = args.poll,
        )
        try:
            trader.run(loop=not args.once, max_hours=args.hours)
        except KeyboardInterrupt:
            console.print("\n[yellow]Bot stopped by user.[/yellow]")

    else:
        from strategies.crypto_momentum import CryptoMomentumStrategy
        strategy = CryptoMomentumStrategy()
        trader   = Trader(client=client, strategy=strategy, trade_size=args.size)
        try:
            trader.run(poll_interval=args.poll)
        except KeyboardInterrupt:
            console.print("\n[yellow]Bot stopped by user.[/yellow]")


if __name__ == "__main__":
    main()
