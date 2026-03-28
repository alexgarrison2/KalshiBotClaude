"""
Backtesting Metrics — the scoreboard for your strategies.

After running a backtest, these functions calculate:
- Win rate: What % of trades made money?
- Profit factor: How much did winners make vs how much did losers lose?
- Max drawdown: What was the worst losing streak?
- Sharpe ratio: How good are returns adjusted for risk?

WHAT GOOD NUMBERS LOOK LIKE:
- Win rate > 55%
- Profit factor > 1.5
- Max drawdown < 20%
- Sharpe ratio > 1.0

A strategy that fails any of these tests needs work before going live.
"""
import math
from dataclasses import dataclass, field
from typing import List
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class Trade:
    """A single completed trade in the backtest."""
    ticker: str
    side: str           # "yes" or "no"
    entry_price: int    # Cents (1-99)
    exit_price: int     # Cents (1-99)
    count: int          # Number of contracts
    fee_cents: float    # Total fees paid in cents
    won: bool           # Did the contract resolve in our favor?
    reason: str = ""    # Strategy signal reason


@dataclass
class BacktestResults:
    """The full results of a backtest run."""
    strategy_name: str
    trades: List[Trade] = field(default_factory=list)

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def num_wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def num_losses(self) -> int:
        return sum(1 for t in self.trades if not t.won)

    @property
    def win_rate(self) -> float:
        """Percentage of trades that were profitable."""
        if self.num_trades == 0:
            return 0.0
        return self.num_wins / self.num_trades

    @property
    def gross_profit_dollars(self) -> float:
        """Total profit from winning trades, in dollars."""
        return sum(
            (100 - t.entry_price) * t.count / 100.0  # payout minus cost
            for t in self.trades if t.won
        )

    @property
    def gross_loss_dollars(self) -> float:
        """Total loss from losing trades, in dollars (positive number)."""
        return sum(
            t.entry_price * t.count / 100.0  # cost of losing contract
            for t in self.trades if not t.won
        )

    @property
    def total_fees_dollars(self) -> float:
        """Total fees paid across all trades, in dollars."""
        return sum(t.fee_cents for t in self.trades) / 100.0

    @property
    def net_profit_dollars(self) -> float:
        """Net profit after fees."""
        return self.gross_profit_dollars - self.gross_loss_dollars - self.total_fees_dollars

    @property
    def profit_factor(self) -> float:
        """
        Gross profit / gross loss. Above 1.0 means profitable overall.
        Above 1.5 is good. Above 2.0 is excellent.
        """
        if self.gross_loss_dollars == 0:
            return float("inf") if self.gross_profit_dollars > 0 else 0.0
        return self.gross_profit_dollars / self.gross_loss_dollars

    @property
    def max_drawdown(self) -> float:
        """
        The worst losing streak as a percentage of peak equity.

        Example: if your account grew to $100 then fell to $80,
        max drawdown = 20%.
        """
        if not self.trades:
            return 0.0

        equity = 0.0
        peak = 0.0
        max_dd = 0.0

        for t in self.trades:
            if t.won:
                equity += (100 - t.entry_price) * t.count / 100.0
            else:
                equity -= t.entry_price * t.count / 100.0
            equity -= t.fee_cents / 100.0

            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak
                max_dd = max(max_dd, dd)

        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        """
        Risk-adjusted return. Higher is better.
        > 1.0 is acceptable, > 2.0 is good, > 3.0 is excellent.

        Calculated as: average return per trade / standard deviation of returns.
        """
        if len(self.trades) < 2:
            return 0.0

        returns = []
        for t in self.trades:
            if t.won:
                ret = (100 - t.entry_price) / t.entry_price  # % return on investment
            else:
                ret = -1.0  # 100% loss on the position

            returns.append(ret)

        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0

        if std_dev == 0:
            return 0.0
        return avg / std_dev


def calculate_fee_cents(price_cents: int, count: int, is_maker: bool = True) -> float:
    """
    Calculate the Kalshi fee for an order.

    Kalshi's fee formula: ceil(0.07 * count * (price/100) * ((100-price)/100))
    Maker (limit order) fee = 25% of that = ceil(0.0175 * ...)

    Args:
        price_cents: Price in cents (1-99)
        count: Number of contracts
        is_maker: True for limit orders (cheaper!), False for market orders

    Returns:
        Fee in cents
    """
    p = price_cents / 100.0
    q = 1.0 - p
    base_fee = 0.07 * count * p * q
    if is_maker:
        base_fee *= 0.25  # Maker discount
    return math.ceil(base_fee)


def print_results(results: BacktestResults) -> None:
    """Print a nicely formatted backtest results table."""
    console.print(f"\n[bold cyan]Backtest Results: {results.strategy_name}[/bold cyan]")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="white")
    table.add_column("Value", style="yellow")
    table.add_column("Target", style="dim")

    def check(value, target_ok, fmt="{:.2f}"):
        color = "green" if target_ok else "red"
        return f"[{color}]{fmt.format(value)}[/{color}]"

    table.add_row("Total Trades", str(results.num_trades), "> 100")
    table.add_row(
        "Win Rate",
        check(results.win_rate * 100, results.win_rate > 0.55, "{:.1f}%"),
        "> 55%",
    )
    table.add_row(
        "Profit Factor",
        check(results.profit_factor, results.profit_factor > 1.5, "{:.2f}x"),
        "> 1.5x",
    )
    table.add_row(
        "Max Drawdown",
        check(results.max_drawdown * 100, results.max_drawdown < 0.20, "{:.1f}%"),
        "< 20%",
    )
    table.add_row(
        "Sharpe Ratio",
        check(results.sharpe_ratio, results.sharpe_ratio > 1.0),
        "> 1.0",
    )
    table.add_row(
        "Net Profit",
        f"${results.net_profit_dollars:.2f}",
        "> $0",
    )
    table.add_row("Total Fees Paid", f"${results.total_fees_dollars:.2f}", "")

    console.print(table)

    # Overall verdict
    passed = (
        results.num_trades >= 100
        and results.win_rate > 0.55
        and results.profit_factor > 1.5
        and results.max_drawdown < 0.20
        and results.sharpe_ratio > 1.0
    )

    if results.num_trades < 10:
        console.print("[yellow]⚠ Not enough trades to evaluate — need more historical data[/yellow]")
    elif passed:
        console.print("[bold green]✓ Strategy PASSES all benchmarks — ready to incubate![/bold green]")
    else:
        console.print("[bold red]✗ Strategy FAILS one or more benchmarks — needs refinement[/bold red]")

    console.print()
