"""
Trader — live execution engines for both strategies.

WeatherTrader  — primary. Full 4-layer model, smart limit pricing,
                 pending order chase logic, settlement P&L summary.
                 Port of Taylor's trader.py to our modular structure.

Trader         — legacy wrapper for the crypto momentum strategy.
                 Kept for backward compatibility with run_bot.py.
"""
import csv
import json
import os
import time
import fcntl
import atexit
import logging
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from rich.console import Console
from rich.panel import Panel

from data.kalshi_client import KalshiAPIClient
from data.weather_data import (
    SERIES_CONFIG, ALL_SERIES,
    fetch_all_forecasts, fetch_metar_observations, fetch_ensemble_forecasts,
)
from strategies.weather_edge import (
    WeatherSignal, parse_open_market,
    evaluate_all_markets, current_sigma,
    aggressive_limit_price, kelly_contracts,
    MIN_EDGE, MIN_VOLUME,
)
from config.settings import POLL_INTERVAL_SECONDS, DEFAULT_TRADE_SIZE

console = Console()
ET = ZoneInfo("America/New_York")

BASE_URL          = "https://api.elections.kalshi.com/trade-api/v2"
PENDING_FILE      = "data/pending_orders.json"
PID_FILE          = "data/trader.pid"
CSV_FILE          = "data/trades.csv"
FILL_TIMEOUT_MINS = 30   # cancel + chase after this long unfilled

CSV_HEADERS = [
    "date", "ticker", "city", "temp_type", "threshold", "strike_type",
    "side", "entry_mode", "price_cents", "contracts", "entry_cost",
    "model_prob", "effective_edge", "source", "notes",
    "order_id", "placed_at", "fee", "result", "pnl",
]


# ── WeatherTrader ─────────────────────────────────────────────────────────────

class WeatherTrader:
    """
    Scans all weather series every 30 seconds. For each scan:
      1. Refreshes NWS forecasts + Open-Meteo ensemble every ~10 min
      2. Refreshes METAR observations every scan
      3. Fetches all open weather markets across configured series
      4. Evaluates each market with the 4-layer signal model
      5. Places limit orders for signals above the edge threshold
      6. Tracks pending orders; cancels + chases any unfilled after 30 min
      7. Logs settlement P&L for filled positions at the end

    Usage:
        trader = WeatherTrader(client, dry_run=True)
        trader.run()                    # loops forever
        trader.run(loop=False)          # one scan and exit
        trader.run(max_hours=8)         # stop after 8 hours
    """

    def __init__(
        self,
        client: KalshiAPIClient,
        dry_run: bool = True,
        trade_size: int = 1,
        poll_interval: int = POLL_INTERVAL_SECONDS,
    ):
        self.client       = client
        self.dry_run      = dry_run
        self.trade_size   = trade_size
        self.poll_interval = poll_interval

        os.makedirs("logs", exist_ok=True)
        os.makedirs("data", exist_ok=True)
        log_path = f"logs/trades_{date.today().isoformat()}.log"
        self.log = self._setup_logger(log_path)
        self._init_csv()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_logger(self, log_path: str) -> logging.Logger:
        log = logging.getLogger("weather_trader")
        if log.handlers:
            return log
        log.setLevel(logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S UTC",
        )
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
        log.propagate = False
        return log

    # ── CSV trade log ─────────────────────────────────────────────────────────

    def _init_csv(self):
        """Create trades.csv with headers if it doesn't exist yet."""
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()

    def _log_trade_csv(self, s: WeatherSignal, order_id: str, contracts: int):
        """Append one row when an order is placed."""
        row = {
            "date":           date.today().isoformat(),
            "ticker":         s.market.ticker,
            "city":           s.market.city,
            "temp_type":      s.market.temp_type,
            "threshold":      s.market.threshold,
            "strike_type":    s.market.strike_type,
            "side":           s.side,
            "entry_mode":     s.entry_mode,
            "price_cents":    s.yes_price_cents,
            "contracts":      contracts,
            "entry_cost":     round(s.entry_cost * contracts, 4),
            "model_prob":     round(s.model_prob, 4),
            "effective_edge": round(s.effective_edge, 4),
            "source":         s.source,
            "notes":          " | ".join(s.notes),
            "order_id":       order_id,
            "placed_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fee":            "",
            "result":         "",
            "pnl":            "",
        }
        with open(CSV_FILE, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)

    def _update_csv_result(self, ticker: str, result: str, pnl: float, fee: float):
        """Fill in result/pnl/fee for the most recent unfilled row for this ticker."""
        rows = []
        updated = False
        with open(CSV_FILE, newline="") as f:
            for row in csv.DictReader(f):
                if not updated and row["ticker"] == ticker and row["result"] == "":
                    row["result"] = result
                    row["pnl"]    = round(pnl, 4)
                    row["fee"]    = round(fee, 4)
                    updated = True
                rows.append(row)
        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

    def _acquire_pid_lock(self):
        """Prevent two instances running at once."""
        self._pid_handle = open(PID_FILE, "w")
        try:
            fcntl.flock(self._pid_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.log.error("Another trader instance is already running. Exiting.")
            self._pid_handle.close()
            return False
        self._pid_handle.write(str(os.getpid()))
        self._pid_handle.flush()
        atexit.register(self._release_pid_lock)
        return True

    def _release_pid_lock(self):
        try:
            fcntl.flock(self._pid_handle, fcntl.LOCK_UN)
            self._pid_handle.close()
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass

    # ── Market fetching ───────────────────────────────────────────────────────

    def _fetch_open_markets_for_series(self, series: str) -> list:
        """Return parsed WeatherMarket objects for one series."""
        resp = self.client._get(
            "/markets",
            params={"series_ticker": series, "status": "open", "limit": 50},
        )
        markets = []
        for m in resp.get("markets", []):
            wm = parse_open_market(series, m)
            if wm:
                markets.append(wm)
        return markets

    def _fetch_all_open_markets(self) -> list:
        """Scan all configured series, return combined list sorted by volume."""
        all_markets = []
        for series in ALL_SERIES:
            try:
                batch = self._fetch_open_markets_for_series(series)
                all_markets.extend(batch)
                time.sleep(0.3)
            except Exception as e:
                if "429" in str(e):
                    time.sleep(5)
                    try:
                        all_markets.extend(self._fetch_open_markets_for_series(series))
                    except Exception:
                        self.log.warning(f"Market fetch failed for {series}: {e}")
                else:
                    self.log.warning(f"Market fetch failed for {series}: {e}")
        all_markets.sort(key=lambda m: m.volume, reverse=True)
        return all_markets

    # ── Order management ──────────────────────────────────────────────────────

    def _place_order(
        self, ticker: str, side: str, yes_price_cents: int, entry_mode: str
    ) -> dict:
        yes_price_cents = max(1, min(99, yes_price_cents))
        if self.dry_run:
            cost = yes_price_cents / 100 if side == "yes" else (100 - yes_price_cents) / 100
            return {"dry_run": True, "order_id": "DRY-RUN",
                    "entry_mode": entry_mode, "cost_usd": round(cost, 2)}
        result = self.client._post("/portfolio/orders", {
            "ticker":    ticker,
            "action":    "buy",
            "side":      side,
            "type":      "limit",
            "count":     self.trade_size,
            "yes_price": yes_price_cents,
        })
        return result.get("order", result)

    def _cancel_order(self, order_id: str):
        if self.dry_run or order_id == "DRY-RUN":
            return
        try:
            self.client._delete(f"/portfolio/orders/{order_id}")
        except Exception as e:
            self.log.warning(f"Cancel failed for {order_id}: {e}")

    def _check_order_status(self, order_id: str) -> dict:
        result = self.client._get(f"/portfolio/orders/{order_id}")
        return result.get("order", result)

    def _fetch_market_result(self, ticker: str) -> Optional[str]:
        """Return 'yes', 'no', or None if not yet settled."""
        try:
            data   = self.client._get(f"/markets/{ticker}")
            market = data.get("market", data)
            status = market.get("status", "")
            result = market.get("result", "")
            if status == "finalized" and result in ("yes", "no"):
                return result
        except Exception:
            pass
        return None

    # ── Pending order persistence ─────────────────────────────────────────────

    @staticmethod
    def _json_default(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj).__name__}")

    def _save_pending(self, pending: dict):
        try:
            with open(PENDING_FILE, "w") as f:
                json.dump(pending, f, default=self._json_default)
        except Exception as e:
            self.log.warning(f"Could not save pending orders: {e}")

    def _load_pending(self, today_str: str) -> dict:
        if not os.path.exists(PENDING_FILE):
            return {}
        try:
            with open(PENDING_FILE) as f:
                saved = json.load(f)
            return {t: p for t, p in saved.items() if p.get("date") == today_str}
        except Exception:
            return {}

    # ── Pending order checker (fill/timeout/chase) ────────────────────────────

    def _check_pending_orders(
        self, pending: dict, filled_positions: dict, today_str: str
    ) -> dict:
        if not pending:
            return pending

        still_pending: dict = {}
        now = time.time()

        for ticker, p in pending.items():
            order_id = p["order_id"]
            signal   = p["signal"]
            age_mins = (now - p["placed_at"]) / 60

            if self.dry_run:
                self.log.info(f"    [DRY] pending {ticker} — simulating fill")
                continue

            try:
                order  = self._check_order_status(order_id)
                status = order.get("status", "")
                filled = float(order.get("fill_count_fp", 0))
            except Exception as e:
                self.log.warning(f"    Could not check order {order_id}: {e}")
                still_pending[ticker] = p
                continue

            if status == "executed" or filled >= 1.0:
                taker_fee = float(order.get("taker_fees_dollars", 0))
                maker_fee = float(order.get("maker_fees_dollars", 0))
                fee_type  = "maker" if taker_fee == 0 else "taker"
                fee_amt   = maker_fee if fee_type == "maker" else taker_fee
                self.log.info(
                    f"    FILLED  {ticker}  ({fee_type} fee ${fee_amt:.4f})"
                    f"  after {age_mins:.0f} min"
                )
                filled_positions[ticker] = {
                    "side":       signal["side"],
                    "entry_cost": signal["entry_cost"],
                    "fee":        fee_amt,
                }
                continue

            if status in ("canceled", "expired"):
                self.log.info(f"    CANCELLED/EXPIRED  {ticker}")
                continue

            if age_mins >= FILL_TIMEOUT_MINS:
                self.log.info(f"    TIMEOUT {ticker} unfilled after {age_mins:.0f} min")
                try:
                    mkt    = self.client._get(f"/markets/{ticker}").get("market", {})
                    fb     = float(mkt.get("yes_bid_dollars", 0))
                    fa     = float(mkt.get("yes_ask_dollars", 1))
                    fm     = (fb + fa) / 2
                    f_edge = signal["model_prob"] - fm

                    if abs(f_edge) < MIN_EDGE:
                        self.log.info(
                            f"    EDGE GONE {ticker}  fresh_mid={fm*100:.0f}¢ — cancelling"
                        )
                        self._cancel_order(order_id)
                        continue

                    chase_price = aggressive_limit_price(signal["side"], fb, fa)
                    self._cancel_order(order_id)
                    result2  = self._place_order(ticker, signal["side"], chase_price, "CHASE")
                    new_id   = result2.get("order_id", "?")
                    chase_cost = (chase_price / 100 if signal["side"] == "yes"
                                  else (100 - chase_price) / 100)
                    self.log.info(
                        f"    CHASE ORDER  {ticker}  {signal['side'].upper()}"
                        f"  price={chase_price}¢  risk=${chase_cost:.2f}  id={new_id}"
                    )
                    still_pending[ticker] = {
                        "order_id":  new_id,
                        "placed_at": time.time(),
                        "date":      today_str,
                        "signal":    signal,
                    }
                except Exception as e:
                    self.log.error(f"    CHASE FAILED  {ticker}  {e}")
                    still_pending[ticker] = p
            else:
                self.log.info(
                    f"    PENDING {ticker}  id={order_id}"
                    f"  age={age_mins:.0f}min  (timeout at {FILL_TIMEOUT_MINS}min)"
                )
                still_pending[ticker] = p

        return still_pending

    # ── Settlement summary ────────────────────────────────────────────────────

    def _log_settlement_summary(self, filled_positions: dict):
        if not filled_positions:
            return
        self.log.info("=" * 66)
        self.log.info("  SETTLEMENT SUMMARY")

        total_cost = total_pnl = 0.0
        settled    = 0

        for ticker in sorted(filled_positions):
            pos  = filled_positions[ticker]
            side = pos["side"]
            cost = pos["entry_cost"]
            fee  = pos.get("fee", 0.0)
            total_cost += cost
            result = self._fetch_market_result(ticker)
            if result is None:
                self.log.info(f"    {ticker:<42} {side.upper()}  cost=${cost:.2f}  — PENDING")
                continue
            won   = (result == side)
            pnl   = (1.0 - cost - fee) if won else -cost
            total_pnl += pnl
            settled   += 1
            self.log.info(
                f"    {ticker:<42} {side.upper()}  cost=${cost:.2f}"
                f"  result={result.upper()}  → {'WIN ' if won else 'LOSS'}"
                f"  P&L={pnl:+.2f}"
            )
            self._update_csv_result(ticker, result, pnl, fee)

        self.log.info("  " + "─" * 62)
        if settled > 0 and total_cost > 0:
            roi = total_pnl / total_cost * 100
            self.log.info(
                f"  Settled {settled}/{len(filled_positions)}  |"
                f"  Invested ${total_cost:.2f}  |"
                f"  Net P&L ${total_pnl:+.2f}  |"
                f"  ROI {roi:+.1f}%"
            )
        elif settled == 0:
            self.log.info(
                f"  0/{len(filled_positions)} markets settled yet"
                "  — check back after market close"
            )
        self.log.info("=" * 66)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, loop: bool = True, max_hours: float = None):
        mode = "DRY-RUN" if self.dry_run else "LIVE TRADING"
        self.log.info("=" * 66)
        self.log.info(f"  WEATHER TRADER  —  {mode}")
        self.log.info(f"  Series   : {len(ALL_SERIES)} weather threshold series")
        self.log.info(f"  Strategy : edge > {MIN_EDGE*100:.0f}¢ | vol ≥ {MIN_VOLUME:,}")
        if self.dry_run:
            self.log.info("  ** Dry-run — no real orders placed. Pass dry_run=False to go live **")
        self.log.info("=" * 66)

        if not self._acquire_pid_lock():
            return

        today_str        = date.today().isoformat()
        traded_today:set = set()
        pending_orders   = self._load_pending(today_str)
        filled_positions: dict = {}
        series_forecasts: dict = {}
        ensemble_data:    dict = {}
        metar_data:       dict = {}
        forecast_age      = 999    # triggers immediate fetch on first scan
        scan_count        = 0
        start_time        = time.time()
        account_balance   = None   # refreshed each scan for Kelly sizing

        # Seed traded_today from today's log to survive restarts
        log_path = f"logs/trades_{today_str}.log"
        if os.path.exists(log_path):
            import re
            pat = re.compile(r"(?<!\[DRY\] )ORDER PLACED\s+(\S+)")
            with open(log_path) as lf:
                for line in lf:
                    m = pat.search(line)
                    if m:
                        traded_today.add(m.group(1))
            if traded_today:
                self.log.info(f"Loaded {len(traded_today)} already-traded ticker(s) from today's log")

        if pending_orders:
            self.log.info(f"Restored {len(pending_orders)} pending order(s) from disk")

        while True:
            if max_hours and (time.time() - start_time) / 3600 >= max_hours:
                self.log.info(f"Max runtime of {max_hours}h reached — stopping.")
                break

            scan_count   += 1
            forecast_age += 1
            sigma         = current_sigma()

            # Refresh NWS forecasts + ensemble every ~10 min (20 × 30s scans)
            if forecast_age >= 20:
                self.log.info("Refreshing forecasts for all cities...")
                try:
                    series_forecasts = fetch_all_forecasts()
                    ensemble_data    = fetch_ensemble_forecasts()
                    forecast_age     = 0
                    n_cities = len(set(cfg["city"] for cfg in SERIES_CONFIG.values()))
                    self.log.info(f"Forecasts + ensemble loaded for {n_cities} cities")
                except Exception as e:
                    self.log.warning(f"Forecast refresh failed: {e}")

            # Refresh balance for Kelly sizing
            try:
                account_balance = self.client.get_balance()
            except Exception:
                pass

            # Refresh METAR every scan (cheap, critical intraday signal)
            try:
                metar_data = fetch_metar_observations()
                self.log.info(f"  METAR observations loaded for {len(metar_data)} cities")
            except Exception as e:
                self.log.warning(f"METAR refresh failed: {e}")
                metar_data = {}

            now_et = datetime.now(ET)
            self.log.info(
                f"Scan #{scan_count}  {now_et.strftime('%H:%M ET')}  "
                f"σ={sigma}°F  traded={len(traded_today)}  pending={len(pending_orders)}"
            )

            # Check pending orders for fills / timeouts
            if pending_orders:
                pending_orders = self._check_pending_orders(
                    pending_orders, filled_positions, today_str
                )
                self._save_pending(pending_orders)

            # Fetch all open weather markets
            try:
                markets = self._fetch_all_open_markets()
                self.log.info(
                    f"  {len(markets)} markets found across"
                    f" {len(ALL_SERIES)} series (vol≥{MIN_VOLUME:,})"
                )
            except Exception as e:
                self.log.warning(f"Market scan failed: {e}")
                if not loop:
                    break
                time.sleep(self.poll_interval)
                continue

            # Evaluate signals
            signals = evaluate_all_markets(
                markets, series_forecasts, sigma, metar_data, ensemble_data
            )

            if not signals:
                self.log.info(f"  No signals — none exceed {MIN_EDGE*100:.0f}¢ edge")
            else:
                self.log.info(f"  {len(signals)} signal(s) found:")
                for s in signals:
                    direction = "BUY YES" if s.side == "yes" else "BUY NO "
                    ttype     = s.market.temp_type.upper()
                    extras    = " ".join(s.notes)
                    self.log.info(
                        f"    [{s.market.city} {ttype}] {s.market.ticker:<34}"
                        f"  src={s.source}"
                        f"  fcst={'>' if s.market.strike_type=='greater' else '<'}"
                        f"{s.market.threshold:.0f}°"
                        f"  model={s.model_prob*100:.1f}%"
                        f"  mid={s.market.mid*100:.0f}¢"
                        f"  edge={s.effective_edge*100:+.1f}¢"
                        f"  → {direction} @ {s.entry_cost*100:.0f}¢"
                        f"  [{s.entry_mode}]"
                        + (f"  {extras}" if extras else "")
                    )

                for s in signals:
                    ticker = s.market.ticker
                    if ticker in traded_today or ticker in pending_orders:
                        self.log.info(f"    SKIP {ticker} — already traded/pending")
                        continue

                    # Kelly sizing: scale contracts with edge, capped at --size
                    if account_balance and not self.dry_run:
                        contracts = min(
                            self.trade_size,
                            kelly_contracts(s.model_prob, s.entry_cost, account_balance),
                        )
                    else:
                        contracts = self.trade_size

                    result = self._place_order(
                        ticker, s.side, s.yes_price_cents, s.entry_mode
                    )
                    order_id = result.get("order_id", "DRY-RUN")
                    total_risk = s.entry_cost * contracts

                    self.log.info(
                        f"    {'[DRY] ' if self.dry_run else ''}ORDER PLACED  {ticker}"
                        f"  {s.side.upper()}  {s.entry_mode}"
                        f"  price={s.yes_price_cents}¢"
                        f"  ×{contracts}"
                        f"  risk=${total_risk:.2f}"
                        f"  id={order_id}"
                    )
                    traded_today.add(ticker)

                    if not self.dry_run:
                        self._log_trade_csv(s, order_id, contracts)

                    if s.entry_mode in ("PASSIVE", "MIDWAY") and not self.dry_run:
                        pending_orders[ticker] = {
                            "order_id":  order_id,
                            "placed_at": time.time(),
                            "date":      today_str,
                            "signal": {
                                "side":       s.side,
                                "entry_cost": s.entry_cost,
                                "model_prob": s.model_prob,
                            },
                        }
                        self._save_pending(pending_orders)

            if not loop:
                break

            time.sleep(self.poll_interval)

        self._log_settlement_summary(filled_positions)


# ── Legacy Trader (crypto momentum) ──────────────────────────────────────────

class Trader:
    """
    Legacy wrapper kept for crypto momentum strategy compatibility.
    For weather markets use WeatherTrader instead.
    """

    def __init__(
        self,
        client: KalshiAPIClient,
        strategy,
        trade_size: int = DEFAULT_TRADE_SIZE,
    ):
        self.client           = client
        self.strategy         = strategy
        self.trade_size       = trade_size

    def run(self, poll_interval: int = POLL_INTERVAL_SECONDS):
        from data.market_finder import MarketFinder
        from data.price_data import get_market_price_data
        from strategies.base_strategy import Signal
        from bot.risk_manager import RiskManager
        from bot.order_manager import OrderManager
        from bot.position_tracker import PositionTracker
        from config.settings import CANDLESTICK_LOOKBACK_MINUTES, CANDLESTICK_INTERVAL

        risk_mgr  = RiskManager()
        order_mgr = OrderManager(client)
        pos_trk   = PositionTracker()
        finder    = MarketFinder(client)

        console.print(Panel(
            f"[bold green]Crypto Bot Starting[/bold green]\n"
            f"Strategy: {self.strategy.name}\n"
            f"Trade size: {self.trade_size} contracts\n"
            f"Press [bold]Ctrl+C[/bold] to stop.",
            title="Kalshi Trading Bot", border_style="green",
        ))

        try:
            while True:
                markets = finder.find_crypto_hourly_markets("BTC")
                for market in (markets or [])[:3]:
                    ticker = market.get("ticker")
                    if not ticker:
                        continue
                    df = get_market_price_data(
                        self.client, market.get("series_ticker", "KXBTC"),
                        ticker, CANDLESTICK_LOOKBACK_MINUTES, CANDLESTICK_INTERVAL,
                    )
                    if df.empty:
                        continue
                    signal = self.strategy.generate_signal(
                        df=df, ticker=ticker,
                        current_yes_price=50, count=self.trade_size,
                    )
                    if signal.signal == Signal.HOLD:
                        continue
                    balance = self.client.get_balance()
                    allowed, reason = risk_mgr.check_trade(
                        signal.price, signal.count,
                        len(pos_trk.open_positions), balance,
                    )
                    if not allowed:
                        console.print(f"[yellow]Blocked: {reason}[/yellow]")
                        continue
                    order = order_mgr.execute_signal(signal)
                    if order:
                        pos_trk.add_position(
                            order_id=order.get("order_id", str(time.time())),
                            ticker=ticker,
                            side="yes" if signal.signal == Signal.BUY_YES else "no",
                            entry_price=signal.price, count=signal.count,
                            strategy=self.strategy.name, reason=signal.reason,
                        )
                    break
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Bot stopped.[/yellow]")
