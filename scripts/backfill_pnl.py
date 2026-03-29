"""
Backfill result/pnl for any trades.csv rows that were placed but never settled.

Run this anytime — it's safe to run multiple times (skips already-filled rows).
Queries Kalshi for each market's current status and writes result + P&L.

USAGE:
    python3 scripts/backfill_pnl.py
    python3 scripts/backfill_pnl.py --dry-run   # show what would change, don't write
"""
import csv
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from data.kalshi_client import KalshiAPIClient

console = Console()
CSV_FILE = "data/trades.csv"


def fetch_result(client: KalshiAPIClient, ticker: str):
    """Return ('yes'|'no', result_str) or (None, 'pending/error')."""
    try:
        data   = client._get(f"/markets/{ticker}")
        market = data.get("market", data)
        status = market.get("status", "")
        result = market.get("result", "")
        if status == "finalized" and result in ("yes", "no"):
            return result, status
        return None, status or "open"
    except Exception as e:
        return None, f"error: {e}"


def calc_pnl(side: str, entry_cost: float, fee: float, result: str) -> float:
    """
    Calculate net P&L for one contract.
      side="yes": won if result=="yes", pnl = 1 - cost - fee, else -cost
      side="no":  won if result=="no",  pnl = 1 - cost - fee, else -cost
    entry_cost is already in dollars (e.g. 0.04 for a 4¢ contract).
    """
    won = (result == side)
    return (1.0 - entry_cost - fee) if won else -entry_cost


def main():
    parser = argparse.ArgumentParser(description="Backfill result/pnl in trades.csv")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()

    if not os.path.exists(CSV_FILE):
        console.print(f"[red]{CSV_FILE} not found.[/red]")
        return

    client = KalshiAPIClient()

    with open(CSV_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    fieldnames = list(rows[0].keys()) if rows else []

    # Find rows needing backfill
    pending_rows = [r for r in rows if r.get("result", "") == ""]
    if not pending_rows:
        console.print("[green]All trades already have results — nothing to backfill.[/green]")
        return

    console.print(f"\n[bold]PnL Backfill[/bold] — {len(pending_rows)} unsettled trade(s)\n")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Date",        style="dim")
    table.add_column("Ticker",      style="white")
    table.add_column("Side",        style="yellow")
    table.add_column("Cost",        justify="right", style="yellow")
    table.add_column("Result",      justify="center")
    table.add_column("P&L",         justify="right")
    table.add_column("Status",      style="dim")

    updated = 0
    for row in rows:
        if row.get("result", "") != "":
            continue  # already filled

        ticker     = row["ticker"]
        side       = row["side"]
        entry_cost = float(row["entry_cost"] or 0)
        fee        = float(row["fee"] or 0)

        result, status = fetch_result(client, ticker)

        if result is None:
            table.add_row(
                row["date"], ticker, side.upper(),
                f"${entry_cost:.2f}", "—", "—", status,
            )
            continue

        pnl = calc_pnl(side, entry_cost, fee, result)
        won = (result == side)
        pnl_color  = "green" if won else "red"
        result_str = f"[{'green' if result=='yes' else 'red'}]{result.upper()}[/]"

        table.add_row(
            row["date"], ticker, side.upper(),
            f"${entry_cost:.2f}",
            result_str,
            f"[{pnl_color}]${pnl:+.2f}[/{pnl_color}]",
            "✓ filled" if not args.dry_run else "dry-run",
        )

        if not args.dry_run:
            row["result"] = result
            row["pnl"]    = round(pnl, 4)
            if not row.get("fee"):
                row["fee"] = round(fee, 4)
            updated += 1

    console.print(table)

    if args.dry_run:
        console.print("\n[yellow]Dry-run — no changes written.[/yellow]")
        return

    if updated == 0:
        console.print("\n[yellow]No settled markets found yet — try again after 4 PM ET.[/yellow]")
        return

    # Write back
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Push to GitHub
    import subprocess
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(["git", "-C", root, "add", "data/trades.csv"], check=True, capture_output=True)
        diff = subprocess.run(["git", "-C", root, "diff", "--cached", "--quiet"], capture_output=True)
        if diff.returncode != 0:
            subprocess.run(["git", "-C", root, "commit", "-m", "auto: backfill pnl results"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", root, "push"], check=True, capture_output=True)
            console.print(f"\n[green]✓ Updated {updated} row(s) and pushed to GitHub.[/green]")
        else:
            console.print(f"\n[green]✓ Updated {updated} row(s) locally (nothing to push).[/green]")
    except Exception as e:
        console.print(f"\n[yellow]Updated locally but couldn't push: {e}[/yellow]")


if __name__ == "__main__":
    main()
