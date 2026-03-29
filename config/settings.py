"""
Settings for your Kalshi trading bot.
Change these values to control how the bot trades.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Kalshi API Credentials ──────────────────────────────────────────
# These come from your .env file (never hardcode them here)
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# ── API Endpoint ────────────────────────────────────────────────────
# Live trading (real money)
KALSHI_API_HOST = "https://api.elections.kalshi.com/trade-api/v2"

# ── Trading Sizes ───────────────────────────────────────────────────
# How many contracts to buy per trade (start small!)
DEFAULT_TRADE_SIZE = 1          # 1 contract = ~$0.01 to $0.99 risk

# Maximum contracts in a single order
MAX_POSITION_SIZE = int(os.getenv("MAX_POSITION_SIZE", "5"))

# ── Risk Management ─────────────────────────────────────────────────
# Stop trading for the day if you lose this many dollars
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10.0"))

# Maximum number of open positions at once
MAX_OPEN_POSITIONS = 3

# Maximum % of your account balance to risk on one trade
MAX_RISK_PER_TRADE_PCT = 5.0    # 5% of your balance

# ── Strategy Settings ───────────────────────────────────────────────
# Crypto Momentum Strategy
MACD_FAST = 3
MACD_SLOW = 15
MACD_SIGNAL = 3
RSI_PERIOD = 14
RSI_OVERSOLD = 30       # Buy signal when RSI drops below this
RSI_OVERBOUGHT = 70     # Sell signal when RSI goes above this

# Weather Edge Strategy
# (thresholds live in strategies/weather_edge.py — MIN_EDGE = 0.20)

# ── Timing ──────────────────────────────────────────────────────────
# How often the bot checks for new signals (in seconds)
POLL_INTERVAL_SECONDS = 30

# How many minutes of candlestick data to look at
CANDLESTICK_LOOKBACK_MINUTES = 120  # 2 hours of data

# Candlestick interval for crypto strategy
CANDLESTICK_INTERVAL = 1        # 1-minute candles (Kalshi API expects integer minutes)
