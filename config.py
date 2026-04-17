"""
config.py
─────────
Loads .env, exposes a single frozen Config object used by every module.
All thresholds can be overridden via environment variables.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))

def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).strip().lower()
    return val in ("1", "true", "yes")

def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _address(key: str) -> str:
    val = _str(key)
    if val.startswith("0x") and len(val) > 42:
        val = val[:42]
    return val


@dataclass(frozen=True)
class Config:
    # ── Wallet / Auth ──────────────────────────────────────────────
    wallet_private_key: str  = field(default_factory=lambda: _str("WALLET_PRIVATE_KEY"))
    poly_funder_address: str = field(default_factory=lambda: _address("POLY_FUNDER_ADDRESS"))
    poly_api_key: str        = field(default_factory=lambda: _str("POLY_API_KEY"))
    poly_api_secret: str     = field(default_factory=lambda: _str("POLY_API_SECRET"))
    poly_api_passphrase: str = field(default_factory=lambda: _str("POLY_API_PASSPHRASE"))
    polygon_rpc: str         = field(default_factory=lambda: _str("POLYGON_RPC", "https://polygon-rpc.com"))

    # ── Capital ────────────────────────────────────────────────────
    initial_bankroll: float = field(default_factory=lambda: _float("INITIAL_BANKROLL_USDC", 1000.0))

    # ── Mode ───────────────────────────────────────────────────────
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))

    # ── Strategy: certainty band ───────────────────────────────────
    # We enter any market where YES or NO price is in [cert_lo, cert_hi]
    cert_lo: float = field(default_factory=lambda: _float("CERT_LO", 0.90))
    cert_hi: float = field(default_factory=lambda: _float("CERT_HI", 0.95))

    # How much above the market price we assume our true p_est is
    # (small premium to generate positive Kelly edge on high-certainty markets)
    confidence_premium: float = field(default_factory=lambda: _float("CONFIDENCE_PREMIUM", 0.025))

    # ── Kelly / Sizing ─────────────────────────────────────────────
    kelly_fraction: float       = field(default_factory=lambda: _float("KELLY_FRACTION", 0.25))
    max_pct_per_trade: float    = field(default_factory=lambda: _float("MAX_PCT_PER_TRADE", 0.05))
    min_pct_per_trade: float    = field(default_factory=lambda: _float("MIN_PCT_PER_TRADE", 0.005))
    min_order_usdc: float       = field(default_factory=lambda: _float("MIN_ORDER_USDC", 2.0))

    # ── Risk management ────────────────────────────────────────────
    daily_drawdown_limit: float  = field(default_factory=lambda: _float("DAILY_DRAWDOWN_LIMIT", 0.20))
    drawdown_pause_hours: float  = field(default_factory=lambda: _float("DRAWDOWN_PAUSE_HOURS", 4.0))
    max_open_positions: int      = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 20))

    # ── Scanning ───────────────────────────────────────────────────
    scan_interval_sec: int  = field(default_factory=lambda: _int("SCAN_INTERVAL_SEC", 60))
    gamma_page_size: int    = field(default_factory=lambda: _int("GAMMA_PAGE_SIZE", 100))
    # Minimum USDC liquidity in the market (avoids illiquid markets)
    min_liquidity_usdc: float = field(default_factory=lambda: _float("MIN_LIQUIDITY_USDC", 500.0))
    # Maximum spread allowed (bid-ask gap on the token we buy)
    max_spread: float = field(default_factory=lambda: _float("MAX_SPREAD", 0.03))
    # Skip markets closing in less than this many hours
    min_hours_to_close: float = field(default_factory=lambda: _float("MIN_HOURS_TO_CLOSE", 0.0))
    # Skip markets closing in more than this many hours (targets short-expiry markets)
    max_hours_to_close: float = field(default_factory=lambda: _float("MAX_HOURS_TO_CLOSE", 0.5))

    # ── Polymarket endpoints ───────────────────────────────────────
    clob_api_url: str  = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"

    # ── Misc ───────────────────────────────────────────────────────
    bankroll_file: str = "bankroll.json"
    positions_file: str = "positions.json"
    log_file: str      = "log.txt"
    chain_id: int      = 137  # Polygon mainnet

    def validate(self) -> None:
        if not self.wallet_private_key or self.wallet_private_key.startswith("0xYOUR"):
            raise ValueError("WALLET_PRIVATE_KEY not set in .env")
        if not self.poly_funder_address or self.poly_funder_address.startswith("0xYOUR"):
            raise ValueError("POLY_FUNDER_ADDRESS not set in .env")
        if not self.dry_run:
            for attr in ("poly_api_key", "poly_api_secret", "poly_api_passphrase"):
                if not getattr(self, attr) or getattr(self, attr).startswith("your_"):
                    raise ValueError(f"{attr.upper()} not set – required for live trading")
        if self.initial_bankroll <= 0:
            raise ValueError("INITIAL_BANKROLL_USDC must be > 0")
        if not (0 < self.cert_lo < self.cert_hi < 1):
            raise ValueError("Invalid CERT_LO / CERT_HI range")


cfg = Config()
