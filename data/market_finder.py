"""
Market Finder — discovers the best markets to trade on Kalshi.

Markets are now returned as plain dicts (e.g., market["ticker"]).
"""
import time
from typing import Optional
from rich.console import Console

from data.kalshi_client import KalshiAPIClient

console = Console()


class MarketFinder:
    """Finds tradeable markets on Kalshi."""

    def __init__(self, client: KalshiAPIClient):
        self.client = client

    def find_crypto_hourly_markets(self, crypto: str = "BTC") -> list:
        """
        Find active crypto hourly markets closing within the next 6 hours.

        Args:
            crypto: "BTC" for Bitcoin, "ETH" for Ethereum

        Returns:
            List of market dicts, sorted by closest to expiration
        """
        series_ticker = f"KX{crypto}"
        all_markets = []
        cursor = None

        while True:
            markets, cursor = self.client.get_markets(
                status="open",
                series_ticker=series_ticker,
                limit=200,
                cursor=cursor,
            )
            all_markets.extend(markets)
            if not cursor:
                break

        # Filter to markets closing in the next 6 hours
        now = int(time.time())
        hourly_markets = []
        for m in all_markets:
            close_ts = _parse_close_ts(m)
            if close_ts and now < close_ts < now + (6 * 3600):
                hourly_markets.append(m)

        # Sort by closest to expiration first
        hourly_markets.sort(key=lambda m: _parse_close_ts(m) or 0)

        console.print(f"[cyan]Found {len(hourly_markets)} active {crypto} hourly markets[/cyan]")
        return hourly_markets

    def find_weather_markets(self, city: Optional[str] = None) -> list:
        """
        Find active weather/temperature markets.

        Args:
            city: Optional city filter (e.g., "CHI" for Chicago)

        Returns:
            List of market dicts
        """
        all_markets = []

        # Search known weather series tickers
        for series in ["KXTEMP", "KHIGHNY", "KHIGHCHI", "KHIGHLA", "KTEMP"]:
            try:
                markets, _ = self.client.get_markets(status="open", series_ticker=series, limit=200)
                all_markets.extend(markets)
            except Exception:
                continue

        # Also do a broad search for temperature keywords
        try:
            markets, _ = self.client.get_markets(status="open", limit=1000)
            seen = {m.get("ticker") for m in all_markets}
            for m in markets:
                ticker = (m.get("ticker") or "").upper()
                title = (m.get("title") or "").upper()
                if any(kw in ticker or kw in title for kw in ["TEMP", "WEATHER", "HIGH", "LOW", "DEGREE"]):
                    if m.get("ticker") not in seen:
                        all_markets.append(m)
                        seen.add(m.get("ticker"))
        except Exception:
            pass

        if city:
            city_upper = city.upper()
            all_markets = [
                m for m in all_markets
                if city_upper in (m.get("ticker") or "").upper()
                or city_upper in (m.get("title") or "").upper()
            ]

        console.print(f"[cyan]Found {len(all_markets)} active weather markets[/cyan]")
        return all_markets

    def get_market_with_best_liquidity(self, markets: list) -> Optional[dict]:
        """Find the market with the tightest bid/ask spread."""
        best_market = None
        best_spread = float("inf")

        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue
            try:
                orderbook = self.client.get_orderbook(ticker)
                yes_bids = orderbook.get("yes") or []
                no_bids = orderbook.get("no") or []

                if not yes_bids or not no_bids:
                    continue

                best_bid = max((lvl.get("price", 0) for lvl in yes_bids), default=0)
                best_ask = 100 - min((lvl.get("price", 0) for lvl in no_bids), default=100)

                spread = best_ask - best_bid
                if 0 < spread < best_spread:
                    best_spread = spread
                    best_market = market
            except Exception:
                continue

        if best_market:
            console.print(
                f"[green]Best liquidity: {best_market.get('ticker', '?')} "
                f"(spread: {best_spread}c)[/green]"
            )
        return best_market


def _parse_close_ts(market: dict) -> Optional[int]:
    """Extract the closing timestamp from a market dict."""
    close_str = market.get("close_time") or market.get("expiration_time")
    if close_str is None:
        return None
    if isinstance(close_str, (int, float)):
        return int(close_str)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(close_str.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None
