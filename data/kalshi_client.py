"""
Kalshi API Client — talks to Kalshi's servers directly via HTTP.

We bypass the kalshi-python SDK's model deserialization because the SDK
is outdated and chokes on new status values like 'finalized' that Kalshi
added after the SDK was last updated.

Instead, we:
1. Handle RSA authentication ourselves (same algorithm the SDK uses)
2. Make raw HTTP requests with the `requests` library
3. Return plain Python dicts and lists — no fragile model classes

Authentication works like this:
- Every request must be signed with your RSA private key
- The signature covers: timestamp + HTTP method + URL path
- Three headers carry the auth: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
"""
import base64
import json
import time
from typing import Optional
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from rich.console import Console

from config.settings import KALSHI_API_HOST, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH

console = Console()


class KalshiAPIClient:
    """Your connection to Kalshi. All API calls go through here."""

    def __init__(self, api_key_id: str = "", private_key_path: str = ""):
        self.key_id = api_key_id or KALSHI_API_KEY_ID
        self.key_path = private_key_path or KALSHI_PRIVATE_KEY_PATH
        self.base_url = KALSHI_API_HOST
        self.session = requests.Session()

        if not self.key_id or not self.key_path:
            raise ValueError(
                "Missing Kalshi API credentials!\n"
                "1. Make sure your .env file exists\n"
                "2. Fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n"
                "3. Get these from: https://kalshi.com/account/settings"
            )

        # Load the private key once at startup
        with open(self.key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

        console.print("[green]Connected to Kalshi API[/green]")

    def _sign(self, method: str, full_url: str) -> dict:
        """
        Generate the RSA authentication headers required by Kalshi.

        The signature covers: timestamp_ms + METHOD + /full/url/path
        The path must be the full URL path (e.g., /trade-api/v2/markets),
        NOT just the endpoint suffix (/markets).
        """
        from urllib.parse import urlparse
        ts_ms = str(int(time.time() * 1000))
        parsed = urlparse(full_url)
        clean_path = parsed.path  # e.g., "/trade-api/v2/portfolio/balance"
        message = ts_ms + method.upper() + clean_path

        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        """Make an authenticated GET request. Returns parsed JSON."""
        url = self.base_url + path
        headers = self._sign("GET", url)
        response = self.session.get(url, headers=headers, params=params or {}, timeout=10)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict = None) -> dict:
        """Make an authenticated POST request. Returns parsed JSON."""
        url = self.base_url + path
        headers = self._sign("POST", url)
        response = self.session.post(url, headers=headers, json=body or {}, timeout=10)
        response.raise_for_status()
        return response.json()

    def _delete(self, path: str) -> dict:
        """Make an authenticated DELETE request. Returns parsed JSON."""
        url = self.base_url + path
        headers = self._sign("DELETE", url)
        response = self.session.delete(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()

    # ── Market Discovery ────────────────────────────────────────────

    def get_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        min_close_ts: Optional[int] = None,
        max_close_ts: Optional[int] = None,
    ):
        """
        Get a list of available markets.

        Returns plain dicts — not SDK model objects, so no enum validation issues.
        """
        params = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if max_close_ts:
            params["max_close_ts"] = max_close_ts

        data = self._get("/markets", params=params)
        markets = data.get("markets", [])
        next_cursor = data.get("cursor")
        return markets, next_cursor

    def get_market(self, ticker: str) -> dict:
        """Get details for a single market by its ticker."""
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    # ── Price Data ──────────────────────────────────────────────────

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        period_interval: int = 1,
    ) -> list:
        """
        Get candlestick (OHLCV) price data for a market.

        Returns a list of dicts with keys: open, high, low, close, volume, end_period_ts
        """
        if end_ts is None:
            end_ts = int(time.time())
        if start_ts is None:
            start_ts = end_ts - (2 * 60 * 60)

        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        data = self._get(f"/series/{series_ticker}/markets/{market_ticker}/candlesticks", params=params)
        return data.get("candlesticks", [])

    def get_orderbook(self, ticker: str) -> dict:
        """
        Get the order book for a market.

        Returns a dict with 'yes' and 'no' keys, each a list of {price, delta} dicts.
        """
        data = self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook", data)

    def get_trades(self, ticker: str, limit: int = 100, cursor: Optional[str] = None):
        """Get recent trades for a market."""
        params = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._get("/markets/trades", params=params)
        return data.get("trades", []), data.get("cursor")

    # ── Order Management ────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
        expiration_ts: Optional[int] = None,
    ) -> dict:
        """
        Place a limit order on Kalshi.

        Always use order_type='limit' — maker fees are 4x cheaper than market orders.
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        if expiration_ts is not None:
            body["expiration_ts"] = expiration_ts

        data = self._post("/portfolio/orders", body=body)
        return data.get("order", data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = None) -> list:
        """
        Get your orders.

        status options: "resting" (open), "canceled", "executed"
        """
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = self._get("/portfolio/orders", params=params)
        return data.get("orders", [])

    def cancel_all_orders(self, ticker: Optional[str] = None) -> int:
        """Cancel all open orders, optionally filtered to one market."""
        open_orders = self.get_orders(status="resting")
        canceled = 0
        for order in open_orders:
            if ticker is None or order.get("ticker") == ticker:
                try:
                    self.cancel_order(order["order_id"])
                    canceled += 1
                except Exception:
                    pass
        return canceled

    # ── Portfolio ───────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Get your account balance in dollars."""
        data = self._get("/portfolio/balance")
        return data.get("balance", 0) / 100.0

    def get_positions(self, ticker: Optional[str] = None) -> list:
        """Get your current open positions."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/positions", params=params)
        return data.get("market_positions", [])

    def get_fills(self, ticker: Optional[str] = None, limit: int = 100) -> list:
        """Get your recent trade fills (executed orders)."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/fills", params=params)
        return data.get("fills", [])
