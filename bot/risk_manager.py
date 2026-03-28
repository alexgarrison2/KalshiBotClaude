"""
Risk Manager — the safety system that keeps you from losing too much.

The risk manager checks every potential trade BEFORE it goes through.
Think of it like a bouncer at a club — it decides what gets in and what doesn't.

RULES ENFORCED:
1. Daily loss limit: Stop trading for the day if we lose more than $X
2. Max position size: Never buy more than N contracts at once
3. Max open positions: Never have more than N markets open at once
4. Balance check: Never risk more than X% of your account on one trade
"""
import json
import os
from datetime import date
from typing import Optional
from rich.console import Console

from config.settings import (
    MAX_DAILY_LOSS,
    MAX_POSITION_SIZE,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE_PCT,
)

console = Console()

DAILY_LOSS_FILE = "/tmp/kalshi_daily_loss.json"


class RiskManager:
    """
    Enforces trading limits. All trades must pass through here first.
    """

    def __init__(
        self,
        max_daily_loss: float = MAX_DAILY_LOSS,
        max_position_size: int = MAX_POSITION_SIZE,
        max_open_positions: int = MAX_OPEN_POSITIONS,
        max_risk_pct: float = MAX_RISK_PER_TRADE_PCT,
    ):
        self.max_daily_loss = max_daily_loss
        self.max_position_size = max_position_size
        self.max_open_positions = max_open_positions
        self.max_risk_pct = max_risk_pct
        self._load_daily_loss()

    def _load_daily_loss(self):
        """Load today's running loss total from disk."""
        today = date.today().isoformat()
        try:
            if os.path.exists(DAILY_LOSS_FILE):
                with open(DAILY_LOSS_FILE) as f:
                    data = json.load(f)
                if data.get("date") == today:
                    self.daily_loss = data.get("loss", 0.0)
                    return
        except Exception:
            pass
        self.daily_loss = 0.0

    def _save_daily_loss(self):
        """Save today's running loss total to disk."""
        today = date.today().isoformat()
        data = {"date": today, "loss": self.daily_loss}
        try:
            with open(DAILY_LOSS_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def record_loss(self, amount_dollars: float):
        """
        Record a realized loss. Call this when a trade closes as a loss.

        Args:
            amount_dollars: How much was lost (positive number)
        """
        self.daily_loss += amount_dollars
        self._save_daily_loss()
        console.print(f"[red]Loss recorded: ${amount_dollars:.2f} | Daily total: ${self.daily_loss:.2f}[/red]")

    def record_win(self, amount_dollars: float):
        """
        Record a win (reduces daily loss counter if it was previously negative).

        Args:
            amount_dollars: How much was gained (positive number)
        """
        self.daily_loss = max(0.0, self.daily_loss - amount_dollars)
        self._save_daily_loss()
        console.print(f"[green]Win recorded: ${amount_dollars:.2f} | Daily loss: ${self.daily_loss:.2f}[/green]")

    def is_daily_limit_hit(self) -> bool:
        """Returns True if we've hit the daily loss limit and should stop trading."""
        return self.daily_loss >= self.max_daily_loss

    def check_trade(
        self,
        price_cents: int,
        count: int,
        open_positions: int,
        account_balance: Optional[float] = None,
    ) -> tuple[bool, str]:
        """
        Check if a trade is allowed under our risk rules.

        Args:
            price_cents: The order price in cents (1-99)
            count: Number of contracts
            open_positions: How many positions are currently open
            account_balance: Current account balance in dollars (optional)

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        # Rule 1: Daily loss limit
        if self.is_daily_limit_hit():
            return False, f"Daily loss limit hit (${self.daily_loss:.2f} >= ${self.max_daily_loss})"

        # Rule 2: Max position size
        if count > self.max_position_size:
            return False, f"Position size {count} exceeds max {self.max_position_size}"

        # Rule 3: Max open positions
        if open_positions >= self.max_open_positions:
            return False, f"Already at max open positions ({self.max_open_positions})"

        # Rule 4: Balance check
        if account_balance is not None and account_balance > 0:
            trade_cost = (price_cents * count) / 100.0
            risk_pct = (trade_cost / account_balance) * 100
            if risk_pct > self.max_risk_pct:
                return False, (
                    f"Trade risk {risk_pct:.1f}% exceeds max {self.max_risk_pct}% "
                    f"(${trade_cost:.2f} on ${account_balance:.2f} balance)"
                )

        return True, "OK"

    @property
    def status_summary(self) -> str:
        """One-line summary of current risk status."""
        if self.is_daily_limit_hit():
            return f"[red]TRADING HALTED - Daily limit hit: ${self.daily_loss:.2f}[/red]"
        remaining = self.max_daily_loss - self.daily_loss
        return f"Risk OK | Daily loss: ${self.daily_loss:.2f} | Remaining: ${remaining:.2f}"
