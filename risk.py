"""
risk.py
───────
Kelly position sizing + daily drawdown guard.

For high-certainty markets (90-95%), the Kelly edge is thin.
We assume a small confidence premium above market price to generate
positive expected value. The premium is configurable.

Kelly (binary):
    full_kelly = (p_est - price) / (1 - price)
    frac_kelly = full_kelly * kelly_fraction
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path
from typing import Optional

from config import cfg

log = logging.getLogger("risk")

_BANKROLL_FILE = Path(cfg.bankroll_file)

# ── In-memory drawdown pause ──────────────────────────────────────
_pause_until: float = 0.0


# ─────────────────────────────────────────────────────────────────
#  Bankroll persistence
# ─────────────────────────────────────────────────────────────────

def _fetch_onchain_usdc() -> Optional[float]:
    if not cfg.poly_funder_address or not cfg.polygon_rpc:
        return None
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(cfg.polygon_rpc, request_kwargs={"timeout": 5}))
        if not w3.is_connected():
            return None
        abi = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                "inputs": [{"name": "account", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}]}]
        funder = Web3.to_checksum_address(cfg.poly_funder_address)
        for addr in (
            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC native
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e
        ):
            bal = w3.eth.contract(address=addr, abi=abi).functions.balanceOf(funder).call()
            if bal > 0:
                return round(bal / 1e6, 4)
        return None
    except Exception as e:
        log.debug(f"_fetch_onchain_usdc: {e}")
        return None


def load_bankroll() -> float:
    if _BANKROLL_FILE.exists():
        try:
            data = json.loads(_BANKROLL_FILE.read_text())
            amount = float(data["current_bankroll"])
            log.info(f"[Bankroll] Loaded from file: ${amount:.2f}")
            return amount
        except Exception as e:
            log.warning(f"Could not read bankroll file: {e}")

    onchain = _fetch_onchain_usdc()
    if onchain is not None:
        log.info(f"[Bankroll] Seeded from on-chain: ${onchain:.2f}")
        save_bankroll(onchain, day_start=onchain)
        return onchain

    log.info(f"[Bankroll] Using config default: ${cfg.initial_bankroll:.2f}")
    return cfg.initial_bankroll


def save_bankroll(amount: float, day_start: Optional[float] = None) -> None:
    existing: dict = {}
    if _BANKROLL_FILE.exists():
        try:
            existing = json.loads(_BANKROLL_FILE.read_text())
        except Exception:
            pass
    existing["current_bankroll"] = round(amount, 4)
    existing["last_updated"] = time.time()
    if day_start is not None:
        existing["day_start_bankroll"] = round(day_start, 4)
        existing["day_start_ts"] = time.time()
    _BANKROLL_FILE.write_text(json.dumps(existing, indent=2))


def get_day_start_bankroll() -> float:
    if not _BANKROLL_FILE.exists():
        return cfg.initial_bankroll
    try:
        data = json.loads(_BANKROLL_FILE.read_text())
        stored_day = datetime.datetime.utcfromtimestamp(data.get("day_start_ts", 0)).date()
        today = datetime.datetime.utcnow().date()
        if stored_day == today:
            return float(data.get("day_start_bankroll", cfg.initial_bankroll))
        current = float(data.get("current_bankroll", cfg.initial_bankroll))
        save_bankroll(current, day_start=current)
        return current
    except Exception as e:
        log.warning(f"get_day_start_bankroll: {e}")
        return cfg.initial_bankroll


# ─────────────────────────────────────────────────────────────────
#  Drawdown guard
# ─────────────────────────────────────────────────────────────────

def is_paused() -> bool:
    return time.time() < _pause_until


def check_drawdown(current_bankroll: float) -> bool:
    """Return True if trading should be paused due to daily drawdown."""
    global _pause_until

    if is_paused():
        remaining = (_pause_until - time.time()) / 3600
        log.warning(f"[RISK] Paused – {remaining:.1f}h remaining")
        return True

    day_start = get_day_start_bankroll()
    if day_start <= 0:
        return False

    drawdown_pct = (day_start - current_bankroll) / day_start
    if drawdown_pct >= cfg.daily_drawdown_limit:
        _pause_until = time.time() + cfg.drawdown_pause_hours * 3600
        log.error(
            f"[RISK] Drawdown {drawdown_pct:.1%} ≥ {cfg.daily_drawdown_limit:.1%}. "
            f"Pausing {cfg.drawdown_pause_hours}h."
        )
        return True

    return False


# ─────────────────────────────────────────────────────────────────
#  Kelly sizing
# ─────────────────────────────────────────────────────────────────

def kelly_size(
    bankroll: float,
    token_price: float,
) -> float:
    """
    Compute USDC amount to wager.

    p_est = token_price + confidence_premium
    Kelly  = (p_est - token_price) / (1 - token_price) * kelly_fraction

    Since p_est - token_price = confidence_premium always, this simplifies to:
        Kelly = premium / (1 - price) * fraction
    """
    if token_price <= 0 or token_price >= 1:
        return 0.0

    p_est = min(token_price + cfg.confidence_premium, 0.99)
    edge = p_est - token_price
    if edge <= 0:
        return 0.0

    full_kelly = edge / (1.0 - token_price)
    frac_kelly = full_kelly * cfg.kelly_fraction

    clamped_pct = max(cfg.min_pct_per_trade, min(frac_kelly, cfg.max_pct_per_trade))
    usdc = round(bankroll * clamped_pct, 2)

    log.debug(
        f"[KELLY] price={token_price:.3f} p_est={p_est:.3f} "
        f"full_k={full_kelly:.4f} frac_k={frac_kelly:.4f} → ${usdc:.2f}"
    )
    return usdc
