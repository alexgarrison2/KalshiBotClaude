"""
Check settlement results for today's (or any date's) trades.

Queries Kalshi for market outcomes, updates trades.csv, and prints a P&L summary.

USAGE:
    python deploy/check_results.py           # today's trades
    python deploy/check_results.py --date 2026-03-28
"""
import argparse
import csv
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from data.kalshi_client import KalshiAPIClient
from rich.console import Console
from rich.table import Table

CSV_FILE    = "data/trades.csv"
CSV_HEADERS = [
    "date", "ticker", "city", "temp_type", "threshold", "strike_type",
    "side", "entry_mode", "price_cents", "contracts", "entry_cost",
    "model_prob", "effective_edge", "z_score", "sigma_used", "source", "notes",
    "order_id", "placed_at", "fill_price_cents", "fill_time", "fee", "result", "pnl", "brier_score",
]

console = Console()


def fetch_result(client: KalshiAPIClient, ticker: str):
    """Return ('yes'/'no', fee_dollars) or (None, 0) if not yet settled."""
    try:
        data   = client._get(f"/markets/{ticker}")
        market = data.get("market", data)
        status = market.get("status", "")
        result = market.get("result", "")
        if status == "finalized" and result in ("yes", "no"):
            return result, 0.0
    except Exception:
        pass
    return None, 0.0


def update_csv(rows: list, ticker: str, result: str, pnl: float, fee: float):
    updated = False
    for row in rows:
        if not updated and row["ticker"] == ticker and row["result"] == "":
            row["result"] = result
            row["pnl"]    = round(pnl, 4)
            row["fee"]    = round(fee, 4)
            updated = True
    return updated


def main():
    parser = argparse.ArgumentParser(description="Check trade settlement results")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Date to check (YYYY-MM-DD, default: today)")
    args = parser.parse_args()

    if not os.path.exists(CSV_FILE):
        console.print(f"[yellow]No trades CSV found at {CSV_FILE}[/yellow]")
        console.print("Trades are logged automatically when running --live.")
        return

    # Load CSV
    rows = []
    with open(CSV_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    day_rows = [r for r in rows if r["date"] == args.date]
    if not day_rows:
        console.print(f"[yellow]No trades found for {args.date}[/yellow]")
        return

    client = KalshiAPIClient()
    console.print(f"\n[bold]Results for {args.date}[/bold]")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="dim", width=36)
    table.add_column("Side", width=4)
    table.add_column("Price", justify="right", width=5)
    table.add_column("×", justify="right", width=2)
    table.add_column("Cost", justify="right", width=5)
    table.add_column("Edge", justify="right", width=6)
    table.add_column("Result", width=7)
    table.add_column("P&L", justify="right", width=6)
    table.add_column("Source", width=7)

    total_pnl  = 0.0
    total_cost = 0.0
    wins = losses = pending = 0

    for row in day_rows:
        ticker    = row["ticker"]
        side      = row["side"]
        price     = int(row["price_cents"])
        contracts = int(row.get("contracts") or 1)
        cost      = float(row["entry_cost"])
        edge      = float(row["effective_edge"])
        source    = row["source"]

        result = row.get("result", "")
        # Guard against column-shift bug: result field may contain a timestamp instead of yes/no
        if result not in ("yes", "no"):
            result = ""
            row["result"] = ""
        pnl    = float(row["pnl"]) if row.get("pnl") else None

        if not result:
            fetched_result, fee = fetch_result(client, ticker)
            if fetched_result:
                won    = (fetched_result == side)
                pnl    = (1.0 - cost - fee) if won else -cost
                result = fetched_result
                update_csv(rows, ticker, result, pnl, fee)

        if result in ("yes", "no"):
            won = (result == side)
            total_pnl  += pnl
            total_cost += cost
            if won:
                wins += 1
                result_str = "[green]WIN[/green]"
                pnl_str    = f"[green]+{pnl:.2f}[/green]"
            else:
                losses += 1
                result_str = "[red]LOSS[/red]"
                pnl_str    = f"[red]{pnl:.2f}[/red]"
        else:
            pending += 1
            result_str = "[yellow]pending[/yellow]"
            pnl_str    = "—"

        table.add_row(
            ticker,
            side.upper(),
            f"{price}¢",
            str(contracts),
            f"${cost:.2f}",
            f"{edge*100:+.0f}¢",
            result_str,
            pnl_str,
            source,
        )

    # Write updated CSV back
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    console.print(table)

    settled = wins + losses
    if settled > 0 and total_cost > 0:
        roi = total_pnl / total_cost * 100
        win_rate = wins / settled * 100
        console.print(
            f"\n  [bold]{wins}W / {losses}L[/bold] ({win_rate:.0f}% win rate)"
            f"  |  Invested [bold]${total_cost:.2f}[/bold]"
            f"  |  Net P&L [bold]{'[green]' if total_pnl >= 0 else '[red]'}"
            f"${total_pnl:+.2f}{'[/green]' if total_pnl >= 0 else '[/red]'}[/bold]"
            f"  |  ROI [bold]{roi:+.1f}%[/bold]"
        )
    if pending > 0:
        console.print(f"  [yellow]{pending} market(s) still pending settlement[/yellow]")


if __name__ == "__main__":
    main()
