"""
Kalshi Weather Bot — Trading Dashboard
=======================================
Flask app serving live P&L data from trades.csv + Kalshi API.

Run:
    cd /Users/alexgarrison/KalshiBotClaude
    python dashboard/app.py
Then open http://localhost:5001
"""
import csv
import os
import sys
from datetime import date, datetime, timezone
from typing import Optional
from pathlib import Path

from flask import Flask, jsonify, render_template

_MONTH_MAP = {m: i+1 for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
)}

def _event_date_from_ticker(ticker: str) -> Optional[date]:
    """Parse event date from ticker like KXHIGHTDAL-26APR06-B72.5 → 2026-04-06."""
    for seg in ticker.split("-"):
        if len(seg) == 7 and seg[:2].isdigit() and seg[5:7].isdigit():
            try:
                yy, mon, dd = seg[:2], seg[2:5].upper(), seg[5:7]
                m = _MONTH_MAP.get(mon)
                if m:
                    return date(2000 + int(yy), m, int(dd))
            except (ValueError, KeyError):
                pass
    return None

# ── Path setup so we can import the bot's own modules ────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from data.kalshi_client import KalshiAPIClient as KalshiClient  # noqa: E402

app = Flask(__name__)

TRADES_CSV = ROOT / "data" / "trades.csv"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with open(TRADES_CSV, newline="") as f:
        return list(csv.DictReader(f))


def _kalshi_current_price(client: KalshiClient, ticker: str) -> tuple[float, float]:
    """
    Return (yes_bid, yes_ask) for a ticker, or (None, None) on error.
    """
    try:
        m = client.get_market(ticker)
        bid = m.get("yes_bid") or m.get("yes_bid_dollars")
        ask = m.get("yes_ask") or m.get("yes_ask_dollars")
        if bid is not None and ask is not None:
            bid, ask = float(bid), float(ask)
            # Kalshi sometimes returns cents (int), sometimes dollars (float < 1)
            if bid > 1:
                bid /= 100
                ask /= 100
            return bid, ask
    except Exception:
        pass
    return None, None


def _market_value_and_prob(side: str, bid: float | None, ask: float | None) -> tuple[float, float]:
    """
    Given current YES bid/ask and our trade side, return:
        market_value  — current liquidation value per contract (0–1)
        current_prob  — implied YES probability (mid)
    """
    if bid is None or ask is None:
        return None, None
    mid = (bid + ask) / 2.0
    if side == "yes":
        return mid, mid            # value of a YES contract = current YES mid
    else:
        return 1.0 - mid, mid     # value of a NO contract = 1 - YES mid


# ── API: Today ────────────────────────────────────────────────────────────────

@app.route("/api/today")
def api_today():
    today = date.today()
    today_str = today.isoformat()
    all_trades = _load_trades()

    # "Today" = any unsettled trade whose event date is today or earlier
    # (covers trades placed yesterday for today's markets)
    def _is_today_trade(t):
        result = t.get("result", "").strip()
        if result in ("yes", "no"):
            return False  # settled → belongs in history
        ev = _event_date_from_ticker(t.get("ticker", ""))
        if ev is None:
            ev = date.fromisoformat(t.get("date", today_str))
        return ev <= today

    today_trades = [t for t in all_trades if _is_today_trade(t)]

    client = KalshiClient()
    live_trades = []

    for t in today_trades:
        ticker   = t["ticker"]
        side     = t["side"]
        contracts = int(t.get("contracts") or 1)
        price_c  = int(t.get("price_cents") or t.get("fill_price_cents") or 0)
        cost     = float(t.get("entry_cost") or 0) * contracts
        model_p  = float(t.get("model_prob") or 0.5)
        edge_c   = float(t.get("effective_edge") or 0) * 100  # in cents
        source   = t.get("source", "NWS")
        city     = t.get("city", "")
        temp_type = t.get("temp_type", "")
        threshold = t.get("threshold", "")
        strike_type = t.get("strike_type", "")

        # Settled trades have result "yes" or "no" — guard against column-shift timestamps
        result   = t.get("result", "").strip()
        if result not in ("yes", "no"):
            result = ""
        pnl      = t.get("pnl", "").strip()
        is_settled = result in ("yes", "no")

        # Fetch live price from Kalshi
        bid, ask = _kalshi_current_price(client, ticker)
        cur_val, cur_prob = _market_value_and_prob(side, bid, ask)

        market_value = cur_val * contracts if cur_val is not None else None
        projected_pnl = (market_value - cost) if market_value is not None else None

        # Build human-readable description
        if strike_type == "between":
            cap = t.get("cap_strike") or ""
            desc = f"{city} {temp_type.upper()} {threshold}°–{cap}° (between)"
        elif strike_type == "greater":
            desc = f"{city} {temp_type.upper()} > {threshold}°"
        else:
            desc = f"{city} {temp_type.upper()} ≤ {threshold}°"

        live_trades.append({
            "ticker":         ticker,
            "desc":           desc,
            "city":           city,
            "side":           side.upper(),
            "contracts":      contracts,
            "entry_price_c":  price_c,
            "cost":           round(cost, 2),
            "model_prob_pct": round(model_p * 100, 1),
            "edge_cents":     round(edge_c, 1),
            "source":         source,
            "market_value":   round(market_value, 2) if market_value is not None else None,
            "projected_pnl":  round(projected_pnl, 2) if projected_pnl is not None else None,
            "cur_yes_mid_pct": round(cur_prob * 100, 1) if cur_prob is not None else None,
            "is_settled":     is_settled,
            "result":         result,
            "pnl":            float(pnl) if pnl else None,
        })

    # Aggregate totals (live only — not yet settled)
    open_trades   = [t for t in live_trades if not t["is_settled"]]
    closed_trades = [t for t in live_trades if t["is_settled"]]

    total_cost   = sum(t["cost"]         for t in open_trades)
    total_mv     = sum(t["market_value"] for t in open_trades if t["market_value"] is not None)
    total_pnl    = sum(t["projected_pnl"] for t in open_trades if t["projected_pnl"] is not None)
    realized_pnl = sum(t["pnl"] for t in closed_trades if t["pnl"] is not None)

    return jsonify({
        "date":          today_str,
        "n_open":        len(open_trades),
        "n_closed":      len(closed_trades),
        "total_cost":    round(total_cost, 2),
        "total_mv":      round(total_mv, 2),
        "total_pnl":     round(total_pnl, 2),
        "realized_pnl":  round(realized_pnl, 2),
        "trades":        live_trades,
    })


# ── API: History ──────────────────────────────────────────────────────────────

@app.route("/api/history")
def api_history():
    today = date.today()
    all_trades = _load_trades()

    # History = only settled trades, grouped by event date
    by_date: dict[str, dict] = {}
    for t in all_trades:
        result = t.get("result", "").strip()
        if result not in ("yes", "no"):
            continue  # unsettled → belongs in Today, not History

        pnl   = t.get("pnl", "").strip()
        cost  = float(t.get("entry_cost") or 0)

        # Group by event date (from ticker), fall back to placement date
        ev = _event_date_from_ticker(t.get("ticker", ""))
        d  = ev.isoformat() if ev else t.get("date", "")

        if d not in by_date:
            by_date[d] = {"date": d, "trades": [], "total_pnl": 0.0, "n_trades": 0, "n_wins": 0, "n_losses": 0, "total_cost": 0.0}

        by_date[d]["n_trades"]  += 1
        by_date[d]["total_cost"] = round(by_date[d]["total_cost"] + cost, 2)
        if pnl:
            pnl_f = float(pnl)
            by_date[d]["total_pnl"] = round(by_date[d]["total_pnl"] + pnl_f, 2)
            if pnl_f > 0:
                by_date[d]["n_wins"] += 1
            elif pnl_f < 0:
                by_date[d]["n_losses"] += 1

        by_date[d]["trades"].append({
            "ticker":   t["ticker"],
            "city":     t.get("city", ""),
            "side":     t.get("side", "").upper(),
            "contracts": int(t.get("contracts") or 1),
            "price_c":  int(t.get("price_cents") or 0),
            "cost":     round(cost, 2),
            "result":   result,
            "pnl":      float(pnl) if pnl else None,
            "source":   t.get("source", ""),
        })

    days = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)
    overall_pnl = sum(d["total_pnl"] for d in days)
    return jsonify({"days": days, "overall_pnl": round(overall_pnl, 2)})


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/today")
def today_page():
    return render_template("index.html", active="today")


@app.route("/history")
def history_page():
    return render_template("index.html", active="history")


if __name__ == "__main__":
    app.run(port=5001, debug=False)
