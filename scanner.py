"""
scanner.py
──────────
Scans Polymarket (via Gamma API) for markets where YES or NO price
falls in the configured certainty band [cert_lo, cert_hi].

Returns a list of Opportunity objects, each ready for position entry.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional

import aiohttp

from config import cfg

log = logging.getLogger("scanner")


@dataclass
class Opportunity:
    """A qualifying market where we have an edge signal."""
    condition_id: str
    question: str
    slug: str
    # The side we want to buy: "YES" or "NO"
    side: str
    # Current market price of the token we'll buy (0-1)
    token_price: float
    # Token ID for the side we're buying
    token_id: str
    # Liquidity in USDC (volume proxy)
    liquidity: float
    # Seconds until market closes
    seconds_to_close: float

    @property
    def label(self) -> str:
        return f"{self.question[:60]} [{self.side}@{self.token_price:.3f}]"


def _parse_list(value, default=None):
    """Safely parse a field that Gamma API may return as JSON-encoded string."""
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


def _parse_end_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _qualify_market(market: dict) -> Optional[Opportunity]:
    """
    Check if a market falls in the certainty band.
    Returns an Opportunity if it qualifies, None otherwise.
    """
    # Must be active and not closed
    if not market.get("active") or market.get("closed"):
        return None

    # Skip markets that don't have a clear binary yes/no
    outcomes = _parse_list(market.get("outcomes"), [])
    clob_ids = _parse_list(market.get("clobTokenIds"), [])
    prices   = _parse_list(market.get("outcomePrices"), [])

    if len(outcomes) != 2 or len(clob_ids) != 2 or len(prices) != 2:
        return None

    # Parse prices
    try:
        yes_price = float(prices[0])
        no_price  = float(prices[1])
    except (ValueError, TypeError):
        return None

    # Sanity: prices should sum close to 1
    if abs(yes_price + no_price - 1.0) > 0.05:
        return None

    # Check time window
    end_date = _parse_end_date(market.get("endDate") or market.get("endDateIso"))
    if end_date is None:
        return None

    now_utc = datetime.now(timezone.utc)
    seconds_to_close = (end_date - now_utc).total_seconds()

    min_sec = cfg.min_hours_to_close * 3600
    max_sec = cfg.max_hours_to_close * 3600
    if not (min_sec <= seconds_to_close <= max_sec):
        return None

    # Check liquidity
    liquidity = float(market.get("liquidity", 0) or 0)
    if liquidity < cfg.min_liquidity_usdc:
        return None

    # Find qualifying side
    qualifying_side: Optional[str] = None
    token_price: float = 0.0
    token_id: str = ""

    lo, hi = cfg.cert_lo, cfg.cert_hi

    if lo <= yes_price <= hi:
        qualifying_side = "YES"
        token_price = yes_price
        token_id = clob_ids[0] if clob_ids else ""
    elif lo <= no_price <= hi:
        qualifying_side = "NO"
        token_price = no_price
        token_id = clob_ids[1] if len(clob_ids) > 1 else ""

    if qualifying_side is None or not token_id:
        return None

    return Opportunity(
        condition_id=market.get("conditionId", ""),
        question=market.get("question", ""),
        slug=market.get("slug", ""),
        side=qualifying_side,
        token_price=token_price,
        token_id=token_id,
        liquidity=liquidity,
        seconds_to_close=seconds_to_close,
    )


async def scan_all_markets(session: aiohttp.ClientSession) -> List[Opportunity]:
    """
    Paginate through all active Gamma markets and return qualifying opportunities.
    """
    opportunities: List[Opportunity] = []
    offset = 0
    limit = cfg.gamma_page_size
    total_scanned = 0

    while True:
        try:
            async with session.get(
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                    "order": "liquidity",
                    "ascending": "false",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[Scanner] Gamma returned HTTP {resp.status} at offset={offset}")
                    break

                markets = await resp.json(content_type=None)
                if not isinstance(markets, list):
                    markets = markets.get("markets", []) if isinstance(markets, dict) else []

                if not markets:
                    break  # no more pages

                for m in markets:
                    opp = _qualify_market(m)
                    if opp:
                        opportunities.append(opp)

                total_scanned += len(markets)
                offset += limit

                # If we got fewer than a full page, we've reached the end
                if len(markets) < limit:
                    break

        except aiohttp.ClientError as e:
            log.error(f"[Scanner] HTTP error at offset={offset}: {e}", exc_info=True)
            break
        except Exception as e:
            log.error(f"[Scanner] Unexpected error at offset={offset}: {e}", exc_info=True)
            break

    log.info(
        f"[Scanner] Scanned {total_scanned} markets → "
        f"{len(opportunities)} qualifying ({cfg.cert_lo:.0%}–{cfg.cert_hi:.0%})"
    )
    return opportunities


async def check_live_spread(
    session: aiohttp.ClientSession,
    opp: Opportunity,
) -> Optional[float]:
    """
    Fetch CLOB order book for the token and return the best ask.
    Returns None if spread is too wide or book is empty.
    """
    try:
        async with session.get(
            f"/book?token_id={opp.token_id}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)

        spread = best_ask - best_bid
        if spread > cfg.max_spread:
            log.debug(
                f"[Scanner] Spread too wide {spread:.3f} for {opp.question[:50]}"
            )
            return None

        return best_ask

    except Exception as e:
        log.debug(f"[Scanner] Book fetch error for {opp.token_id[:10]}: {e}")
        return None
