"""
positions.py
────────────
Persistent position store (JSON-backed) and resolution checker.

Each position tracks:
  - What we bought and at what price
  - Whether it has resolved (and how)
  - PnL contribution
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from config import cfg

log = logging.getLogger("positions")

_POSITIONS_FILE = Path(cfg.positions_file)


# ─────────────────────────────────────────────────────────────────
#  Data model
# ─────────────────────────────────────────────────────────────────

def _load_all() -> Dict[str, dict]:
    if not _POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(_POSITIONS_FILE.read_text())
    except Exception:
        return {}


def _save_all(positions: Dict[str, dict]) -> None:
    _POSITIONS_FILE.write_text(json.dumps(positions, indent=2))


def get_open_positions() -> Dict[str, dict]:
    return {k: v for k, v in _load_all().items() if not v.get("resolved")}


def count_open() -> int:
    return len(get_open_positions())


def already_entered(condition_id: str) -> bool:
    """True if we already have an open (unresolved) position in this market."""
    positions = _load_all()
    pos = positions.get(condition_id)
    return pos is not None and not pos.get("resolved")


def record_entry(
    condition_id: str,
    question: str,
    slug: str,
    side: str,
    token_id: str,
    fill_price: float,
    usdc_size: float,
    shares: float,
) -> None:
    positions = _load_all()
    positions[condition_id] = {
        "condition_id": condition_id,
        "question": question,
        "slug": slug,
        "side": side,
        "token_id": token_id,
        "fill_price": fill_price,
        "usdc_size": usdc_size,
        "shares": shares,
        "entered_at": time.time(),
        "resolved": False,
        "outcome": None,
        "pnl": None,
    }
    _save_all(positions)
    log.info(f"[Positions] Recorded entry: {question[:60]} [{side}@{fill_price:.3f}] ${usdc_size:.2f}")


def record_resolution(condition_id: str, won: bool, pnl: float) -> None:
    positions = _load_all()
    if condition_id not in positions:
        log.warning(f"[Positions] resolve called for unknown position {condition_id[:10]}")
        return
    positions[condition_id]["resolved"] = True
    positions[condition_id]["outcome"] = "win" if won else "loss"
    positions[condition_id]["pnl"] = round(pnl, 4)
    positions[condition_id]["resolved_at"] = time.time()
    _save_all(positions)
    emoji = "WIN" if won else "LOSS"
    log.info(
        f"[Positions] {emoji}: {positions[condition_id]['question'][:60]} "
        f"pnl=${pnl:+.2f}"
    )


def pnl_summary() -> dict:
    positions = _load_all()
    resolved = [p for p in positions.values() if p.get("resolved")]
    wins   = [p for p in resolved if p.get("outcome") == "win"]
    losses = [p for p in resolved if p.get("outcome") == "loss"]
    total_pnl = sum(p.get("pnl", 0) for p in resolved)
    return {
        "total_trades": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(resolved) if resolved else 0,
        "total_pnl": round(total_pnl, 2),
    }


# ─────────────────────────────────────────────────────────────────
#  Resolution checker
# ─────────────────────────────────────────────────────────────────

async def check_resolutions(
    gamma_session: aiohttp.ClientSession,
    bankroll: float,
) -> float:
    """
    For all open positions, check if the market has resolved.
    Returns updated bankroll after applying PnL.
    """
    open_pos = get_open_positions()
    if not open_pos:
        return bankroll

    updated_bankroll = bankroll

    for condition_id, pos in list(open_pos.items()):
        slug = pos.get("slug", "")
        if not slug:
            continue

        try:
            async with gamma_session.get(
                f"/markets?slug={slug}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    continue
                market = markets[0]

        except Exception as e:
            log.debug(f"[Positions] Resolution check error for {slug}: {e}")
            continue

        # Market has resolved when closed=True and a winner is set
        if not market.get("closed"):
            continue

        # Determine winning outcome
        outcomes = _parse_list(market.get("outcomes"), [])
        prices   = _parse_list(market.get("outcomePrices"), [])

        if len(outcomes) < 2 or len(prices) < 2:
            continue

        try:
            yes_price_final = float(prices[0])
        except (ValueError, TypeError):
            continue

        side = pos["side"]
        shares = pos["shares"]
        usdc_spent = pos["usdc_size"]

        # A resolved YES market has YES price = 1.0 (or very close)
        yes_won = yes_price_final >= 0.99

        if side == "YES":
            won = yes_won
        else:  # NO
            won = not yes_won

        if won:
            proceeds = shares  # each share pays $1.00
            pnl = proceeds - usdc_spent
        else:
            proceeds = 0.0
            pnl = -usdc_spent

        updated_bankroll += pnl
        record_resolution(condition_id, won=won, pnl=pnl)

        from risk import save_bankroll
        save_bankroll(updated_bankroll)

    return updated_bankroll


def _parse_list(value, default=None):
    if default is None:
        default = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else default
        except (json.JSONDecodeError, ValueError):
            return default
    return default
