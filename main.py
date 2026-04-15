"""
main.py
───────
Polymarket certainty-band bot.

Strategy:
  Every SCAN_INTERVAL_SEC seconds, scan ALL active Polymarket markets.
  Enter any market where YES or NO token price is in [CERT_LO, CERT_HI].
  Position size via fractional Kelly (with small confidence premium).
  Resolve and settle positions in a background task.

Architecture (asyncio):
  task_scan_and_trade()   — scan → filter → enter new positions
  task_resolve()          — check all open positions for resolution
  task_heartbeat()        — periodic status log
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from config import cfg
from logger import setup_logging, log_trade
from positions import (
    already_entered,
    check_resolutions,
    count_open,
    pnl_summary,
    record_entry,
)
from risk import check_drawdown, kelly_size, load_bankroll, save_bankroll
from scanner import Opportunity, check_live_spread, scan_all_markets
from trader import build_clob_client, place_market_buy

log = logging.getLogger("main")

# ─────────────────────────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────────────────────────

bankroll: float = 0.0
_bankroll_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────
#  Task 1 – Scan & Trade
# ─────────────────────────────────────────────────────────────────

async def task_scan_and_trade(
    gamma_session: aiohttp.ClientSession,
    clob_session: aiohttp.ClientSession,
    clob_client,
) -> None:
    global bankroll

    log.info("[ScanTrade] Started.")
    while True:
        try:
            await _scan_cycle(gamma_session, clob_session, clob_client)
        except Exception as e:
            log.error(f"[ScanTrade] Unhandled error: {e}", exc_info=True)

        await asyncio.sleep(cfg.scan_interval_sec)


async def _scan_cycle(
    gamma_session: aiohttp.ClientSession,
    clob_session: aiohttp.ClientSession,
    clob_client,
) -> None:
    global bankroll

    async with _bankroll_lock:
        current_bankroll = bankroll

    # Risk guards
    if check_drawdown(current_bankroll):
        return

    open_count = count_open()
    if open_count >= cfg.max_open_positions:
        log.info(f"[ScanTrade] Max positions reached ({open_count}/{cfg.max_open_positions}), skipping scan.")
        return

    # Scan
    opportunities = await scan_all_markets(gamma_session)

    # Process each opportunity
    entered_this_cycle = 0
    for opp in opportunities:
        if open_count + entered_this_cycle >= cfg.max_open_positions:
            break

        if already_entered(opp.condition_id):
            continue

        await _try_enter(opp, clob_session, clob_client, current_bankroll)
        entered_this_cycle += 1


async def _try_enter(
    opp: Opportunity,
    clob_session: aiohttp.ClientSession,
    clob_client,
    current_bankroll: float,
) -> None:
    global bankroll

    # Verify live spread before committing
    live_ask = await check_live_spread(clob_session, opp)
    if live_ask is None:
        log.debug(f"[ScanTrade] Spread check failed: {opp.label}")
        return

    # Use live ask as fill price (slightly different from Gamma mid)
    fill_price = live_ask

    # Re-check price is still in our band after live book check
    if not (cfg.cert_lo <= fill_price <= cfg.cert_hi):
        log.debug(
            f"[ScanTrade] Live ask {fill_price:.3f} outside band for {opp.question[:50]}"
        )
        return

    # Size
    usdc = kelly_size(current_bankroll, fill_price)
    if usdc < cfg.min_order_usdc:
        log.debug(f"[ScanTrade] Kelly size ${usdc:.2f} < minimum, skipping.")
        return

    shares = round(usdc / fill_price, 4)

    log_trade(
        action="ENTER",
        question=opp.question,
        side=opp.side,
        price=fill_price,
        usdc_size=usdc,
        bankroll=current_bankroll,
        confidence=fill_price + cfg.confidence_premium,
        dry_run=cfg.dry_run,
    )

    # Place order
    response = place_market_buy(clob_client, opp.token_id, usdc, fill_price)
    if response is None:
        return

    # Record position
    record_entry(
        condition_id=opp.condition_id,
        question=opp.question,
        slug=opp.slug,
        side=opp.side,
        token_id=opp.token_id,
        fill_price=fill_price,
        usdc_size=usdc,
        shares=shares,
    )

    # Deduct from bankroll optimistically
    async with _bankroll_lock:
        bankroll -= usdc
        save_bankroll(bankroll)

    log.info(
        f"[ScanTrade] Entered: {opp.label} "
        f"${usdc:.2f} | {opp.seconds_to_close/3600:.1f}h to close"
    )


# ─────────────────────────────────────────────────────────────────
#  Task 2 – Position resolver
# ─────────────────────────────────────────────────────────────────

async def task_resolve(gamma_session: aiohttp.ClientSession) -> None:
    global bankroll

    log.info("[Resolver] Started.")
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            async with _bankroll_lock:
                current = bankroll

            updated = await check_resolutions(gamma_session, current)

            async with _bankroll_lock:
                bankroll = updated
        except Exception as e:
            log.error(f"[Resolver] Error: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────
#  Task 3 – Heartbeat logger
# ─────────────────────────────────────────────────────────────────

async def task_heartbeat() -> None:
    while True:
        await asyncio.sleep(3600)  # every hour
        async with _bankroll_lock:
            br = bankroll
        summary = pnl_summary()
        log.info(
            f"[Heartbeat] Bankroll=${br:.2f} | "
            f"Open={count_open()} | "
            f"Trades={summary['total_trades']} W={summary['wins']} L={summary['losses']} "
            f"PnL=${summary['total_pnl']:+.2f} WR={summary['win_rate']:.1%}"
        )


# ─────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────

async def main() -> None:
    global bankroll

    setup_logging()
    cfg.validate()

    bankroll = load_bankroll()
    log.info(f"[Main] Starting. Bankroll=${bankroll:.2f} DRY_RUN={cfg.dry_run}")
    log.info(f"[Main] Certainty band: {cfg.cert_lo:.0%} – {cfg.cert_hi:.0%}")
    log.info(f"[Main] Max positions: {cfg.max_open_positions} | Scan interval: {cfg.scan_interval_sec}s")

    clob_client = build_clob_client()

    async with (
        aiohttp.ClientSession(base_url=cfg.gamma_api_url) as gamma_session,
        aiohttp.ClientSession(base_url=cfg.clob_api_url)  as clob_session,
    ):
        await asyncio.gather(
            task_scan_and_trade(gamma_session, clob_session, clob_client),
            task_resolve(gamma_session),
            task_heartbeat(),
        )


if __name__ == "__main__":
    asyncio.run(main())
