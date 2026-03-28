"""
Monitor — a live dashboard showing what your bot is doing.

Run this in a separate terminal window while the bot is running.
It shows your positions, recent trades, P&L, and risk status.

USAGE:
    python deploy/run_monitor.py
    python deploy/run_monitor.py --refresh 10

ARGUMENTS:
    --refresh    How often to refresh the display, in seconds (default: 5)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from dotenv import load_dotenv

load_dotenv()

from bot.position_tracker import PositionTracker, POSITIONS_FILE
from bot.risk_manager import RiskManager, DAILY_LOSS_FILE

console = Console()


def build_dashboard() -> Panel:
    """Build a rich dashboard showing current bot status."""
    tracker = PositionTracker(POSITIONS_FILE)
    risk = RiskManager()

    # ── Header ──────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pnl = tracker.total_pnl
    pnl_str = f"{'+'if pnl >= 0 else ''}${pnl:.2f}"
    pnl_color = "green" if pnl >= 0 else "red"

    header = Text()
    header.append(f"Last updated: {now}  |  ", style="dim")
    header.append(f"Net P&L: {pnl_str}", style=f"bold {pnl_color}")
    header.append(f"  |  Win rate: {tracker.win_rate:.1%}", style="white")
    header.append(f"  |  Trades: {len(tracker.closed_trades)}", style="dim")

    # ── Risk Status ──────────────────────────────────────────────────
    daily_loss = risk.daily_loss
    max_loss = risk.max_daily_loss
    remaining = max_loss - daily_loss
    risk_pct = (daily_loss / max_loss * 100) if max_loss > 0 else 0

    risk_color = "red" if risk.is_daily_limit_hit() else ("yellow" if risk_pct > 50 else "green")
    risk_text = (
        f"[{risk_color}]Daily loss: ${daily_loss:.2f} / ${max_loss:.2f} "
        f"({risk_pct:.0f}%) — Remaining: ${remaining:.2f}[/{risk_color}]"
    )

    # ── Open Positions Table ─────────────────────────────────────────
    open_table = Table(title="Open Positions", show_header=True, header_style="bold cyan")
    open_table.add_column("Ticker", style="white")
    open_table.add_column("Side", style="yellow")
    open_table.add_column("Qty", justify="right")
    open_table.add_column("Entry", justify="right")
    open_table.add_column("Cost", justify="right", style="dim")
    open_table.add_column("Strategy", style="dim")
    open_table.add_column("Opened", style="dim")

    if tracker.open_positions:
        for p in tracker.open_positions:
            open_table.add_row(
                p.ticker,
                f"[green]{p.side.upper()}[/green]" if p.side == "yes" else f"[red]{p.side.upper()}[/red]",
                str(p.count),
                f"{p.entry_price}¢",
                f"${p.cost_dollars:.2f}",
                p.strategy[:20],
                p.opened_at[:16].replace("T", " "),
            )
    else:
        open_table.add_row("[dim]No open positions[/dim]", "", "", "", "", "", "")

    # ── Recent Trades Table ──────────────────────────────────────────
    recent_table = Table(title="Recent Trades (last 10)", show_header=True, header_style="bold magenta")
    recent_table.add_column("Result", justify="center")
    recent_table.add_column("Ticker", style="white")
    recent_table.add_column("Side")
    recent_table.add_column("Entry", justify="right")
    recent_table.add_column("P&L", justify="right")
    recent_table.add_column("Closed", style="dim")

    recent = list(reversed(tracker.closed_trades))[:10]
    if recent:
        for t in recent:
            result_str = "[green]WIN[/green]" if t.won else "[red]LOSS[/red]"
            pnl_color_t = "green" if t.pnl_dollars >= 0 else "red"
            pnl_str_t = f"[{pnl_color_t}]{'+'if t.pnl_dollars>=0 else ''}${t.pnl_dollars:.2f}[/{pnl_color_t}]"
            recent_table.add_row(
                result_str,
                t.ticker[:30],
                t.side.upper(),
                f"{t.entry_price}¢",
                pnl_str_t,
                t.closed_at[:16].replace("T", " "),
            )
    else:
        recent_table.add_row("[dim]No trades yet[/dim]", "", "", "", "", "")

    # ── Assemble ─────────────────────────────────────────────────────
    from rich.columns import Columns
    content = Text()
    content.append(risk_text)

    panel = Panel(
        "\n".join([
            str(header),
            str(risk_text),
            "",
            str(open_table),
            "",
            str(recent_table),
        ]),
        title="[bold]Kalshi Bot Monitor[/bold]",
        border_style="cyan",
    )
    return panel


def main():
    parser = argparse.ArgumentParser(description="Kalshi Bot Monitor")
    parser.add_argument("--refresh", type=int, default=5, help="Refresh interval in seconds")
    args = parser.parse_args()

    console.print("[cyan]Starting monitor... Press Ctrl+C to exit.[/cyan]")

    try:
        with Live(build_dashboard(), refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(args.refresh)
                live.update(build_dashboard())
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/yellow]")


if __name__ == "__main__":
    main()
