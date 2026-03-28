"""
Base Strategy — the template that all strategies follow.

Every strategy (crypto momentum, weather edge) inherits from this class.
Think of it as a contract that says "every strategy must be able to
generate signals and tell us what to trade."
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class Signal(Enum):
    """What the strategy wants to do."""
    BUY_YES = "buy_yes"    # Buy the YES side (betting it happens)
    BUY_NO = "buy_no"      # Buy the NO side (betting it doesn't happen)
    HOLD = "hold"          # Do nothing


@dataclass
class TradeSignal:
    """
    A signal from the strategy, with all the info needed to place a trade.

    Attributes:
        signal: BUY_YES, BUY_NO, or HOLD
        ticker: Which Kalshi market to trade
        price: What price to place the limit order at (1-99 cents)
        count: How many contracts to buy
        confidence: How confident the strategy is (0.0 to 1.0)
        reason: Human-readable explanation of why this signal was generated
    """
    signal: Signal
    ticker: str
    price: int              # In cents (1-99)
    count: int = 1
    confidence: float = 0.5
    reason: str = ""


class BaseStrategy(ABC):
    """
    Abstract base class that all trading strategies must follow.

    To create a new strategy:
    1. Inherit from BaseStrategy
    2. Implement the generate_signal() method
    3. Implement the name property
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The name of this strategy."""
        pass

    @abstractmethod
    def generate_signal(self, **kwargs) -> TradeSignal:
        """
        Analyze the market and generate a trading signal.

        Returns a TradeSignal with what to do (buy, sell, or hold).
        """
        pass

    def __repr__(self) -> str:
        return f"<Strategy: {self.name}>"
