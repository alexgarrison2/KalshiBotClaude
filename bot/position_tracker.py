"""
Position Tracker — keeps a running record of everything the bot does.

Tracks:
- Open positions (what we currently own)
- Closed trades (history of wins and losses)
- Running P&L (profit and loss)

All data is saved to a simple JSON file so you can see what the bot
has been doing even if you restart it.
"""
import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import List, Optional
from rich.console import Console
from rich.table import Table

console = Console()

POSITIONS_FILE = "data/positions.json"


@dataclass
class Position:
    """A currently open position."""
    order_id: str
    ticker: str
    side: str           # "yes" or "no"
    entry_price: int    # Cents paid
    count: int
    opened_at: str      # ISO timestamp
    strategy: str
    reason: str = ""

    @property
    def cost_dollars(self) -> float:
        return (self.entry_price * self.count) / 100.0


@dataclass
class ClosedTrade:
    """A trade that has been settled."""
    order_id: str
    ticker: str
    side: str
    entry_price: int
    exit_price: int     # 100 (win) or 0 (loss)
    count: int
    fee_cents: float
    opened_at: str
    closed_at: str
    strategy: str
    won: bool
    pnl_dollars: float
    reason: str = ""


class PositionTracker:
    """Tracks open positions and trade history, saved to disk."""

    def __init__(self, file_path: str = POSITIONS_FILE):
        self.file_path = file_path
        self.open_positions: List[Position] = []
        self.closed_trades: List[ClosedTrade] = []
        self._load()

    def _load(self):
        """Load positions from disk."""
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path) as f:
                data = json.load(f)
            self.open_positions = [Position(**p) for p in data.get("open", [])]
            self.closed_trades = [ClosedTrade(**t) for t in data.get("closed", [])]
        except Exception as e:
            console.print(f"[yellow]Could not load positions file: {e}[/yellow]")

    def _save(self):
        """Save positions to disk."""
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        data = {
            "open": [asdict(p) for p in self.open_positions],
            "closed": [asdict(t) for t in self.closed_trades],
        }
        with open(self.file_path, "w") as f:
            json.dump(data, f, indent=2)

    def add_position(
        self,
        order_id: str,
        ticker: str,
        side: str,
        entry_price: int,
        count: int,
        strategy: str,
        reason: str = "",
    ) -> Position:
        """Record a new open position."""
        position = Position(
            order_id=order_id,
            ticker=ticker,
            side=side,
            entry_price=entry_price,
            count=count,
            opened_at=datetime.now().isoformat(),
            strategy=strategy,
            reason=reason,
        )
        self.open_positions.append(position)
        self._save()
        console.print(
            f"[green]Position opened: {ticker} | {side.upper()} | "
            f"{count}x @ {entry_price}c | {strategy}[/green]"
        )
        return position

    def close_position(
        self,
        order_id: str,
        exit_price: int,
        fee_cents: float,
        won: bool,
    ) -> Optional[ClosedTrade]:
        """Close an open position and record the trade result."""
        position = next((p for p in self.open_positions if p.order_id == order_id), None)
        if not position:
            console.print(f"[yellow]Position {order_id} not found[/yellow]")
            return None

        pnl = (exit_price - position.entry_price) * position.count / 100.0 - fee_cents / 100.0

        trade = ClosedTrade(
            order_id=order_id,
            ticker=position.ticker,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            count=position.count,
            fee_cents=fee_cents,
            opened_at=position.opened_at,
            closed_at=datetime.now().isoformat(),
            strategy=position.strategy,
            won=won,
            pnl_dollars=pnl,
            reason=position.reason,
        )

        self.open_positions.remove(position)
        self.closed_trades.append(trade)
        self._save()

        status = "[green]WIN" if won else "[red]LOSS"
        console.print(
            f"{status}[/]: {position.ticker} | "
            f"P&L: {'+'if pnl >= 0 else ''}${pnl:.2f}"
        )
        return trade

    @property
    def total_pnl(self) -> float:
        """Total profit/loss across all closed trades."""
        return sum(t.pnl_dollars for t in self.closed_trades)

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        return sum(1 for t in self.closed_trades if t.won) / len(self.closed_trades)

    def print_summary(self):
        """Print a summary of current positions and trade history."""
        console.print("\n[bold cyan]Position Summary[/bold cyan]")

        # Open positions
        if self.open_positions:
            table = Table(title="Open Positions", show_header=True)
            table.add_column("Ticker")
            table.add_column("Side")
            table.add_column("Count")
            table.add_column("Entry")
            table.add_column("Strategy")
            for p in self.open_positions:
                table.add_row(
                    p.ticker, p.side.upper(), str(p.count),
                    f"{p.entry_price}c", p.strategy,
                )
            console.print(table)
        else:
            console.print("[dim]No open positions[/dim]")

        # Summary stats
        total = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.won)
        console.print(
            f"\nTotal trades: {total} | Wins: {wins} | "
            f"Win rate: {self.win_rate:.1%} | "
            f"Net P&L: {'+'if self.total_pnl >= 0 else ''}${self.total_pnl:.2f}"
        )
