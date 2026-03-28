"""
Order Manager — handles the lifecycle of individual orders.

Responsibilities:
- Place limit orders safely (always cancel old ones first)
- Track order status
- Handle partial fills (when only some of your contracts get filled)
- Cancel stale orders that haven't been filled after a timeout

WHY WE CANCEL BEFORE PLACING:
On Kalshi, if you place two orders for the same market without canceling
the first, you could end up with double the position you intended.
The rule: always cancel existing orders for a market before placing a new one.
"""
import time
from typing import Optional
from rich.console import Console

from data.kalshi_client import KalshiAPIClient
from strategies.base_strategy import TradeSignal, Signal
from backtesting.metrics import calculate_fee_cents

console = Console()


class OrderManager:
    """
    Manages order placement and cancellation for a single strategy run.
    """

    def __init__(self, client: KalshiAPIClient, order_timeout_seconds: int = 300):
        """
        Args:
            client: Kalshi API client
            order_timeout_seconds: Cancel unfilled limit orders after this many seconds (default 5 min)
        """
        self.client = client
        self.order_timeout = order_timeout_seconds
        self._pending_orders: dict[str, dict] = {}  # order_id → {ticker, placed_at}

    def execute_signal(self, signal: TradeSignal) -> Optional[dict]:
        """
        Execute a trading signal by placing a limit order on Kalshi.

        Steps:
        1. Cancel any existing open orders for this market
        2. Place a new limit order
        3. Track the order

        Args:
            signal: The TradeSignal from a strategy

        Returns:
            Order object from Kalshi, or None if the trade was not placed
        """
        if signal.signal == Signal.HOLD:
            return None

        ticker = signal.ticker

        # Step 1: Cancel existing orders for this market (safety rule)
        canceled = self.client.cancel_all_orders(ticker=ticker)
        if canceled > 0:
            console.print(f"[dim]Canceled {canceled} existing orders for {ticker}[/dim]")

        # Step 2: Determine order parameters
        if signal.signal == Signal.BUY_YES:
            side = "yes"
            action = "buy"
            yes_price = signal.price
            no_price = None
        else:  # BUY_NO
            side = "no"
            action = "buy"
            yes_price = None
            no_price = signal.price

        # Step 3: Place the limit order
        console.print(
            f"[cyan]Placing order: {ticker} | {side.upper()} | "
            f"{signal.count}x @ {signal.price}c[/cyan]"
        )

        try:
            order = self.client.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=signal.count,
                order_type="limit",     # ALWAYS limit orders (4x cheaper fees)
                yes_price=yes_price,
                no_price=no_price,
            )

            order_id = getattr(order, "order_id", None)
            if order_id:
                self._pending_orders[order_id] = {
                    "ticker": ticker,
                    "placed_at": time.time(),
                    "signal": signal,
                }

            status = getattr(order, "status", "unknown")
            console.print(f"[green]Order placed: {order_id} | Status: {status}[/green]")
            return order

        except Exception as e:
            console.print(f"[red]Order failed for {ticker}: {e}[/red]")
            return None

    def cancel_stale_orders(self):
        """
        Cancel any orders that have been sitting open too long without being filled.

        A limit order that hasn't filled after 5 minutes is probably too far
        from the current market price. Better to cancel and reassess.
        """
        now = time.time()
        stale_ids = [
            order_id
            for order_id, info in self._pending_orders.items()
            if now - info["placed_at"] > self.order_timeout
        ]

        for order_id in stale_ids:
            try:
                self.client.cancel_order(order_id)
                info = self._pending_orders.pop(order_id)
                console.print(
                    f"[yellow]Canceled stale order {order_id} "
                    f"for {info['ticker']} (>{self.order_timeout}s old)[/yellow]"
                )
            except Exception as e:
                console.print(f"[dim]Could not cancel stale order {order_id}: {e}[/dim]")
