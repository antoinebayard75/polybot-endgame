"""
trader.py
─────────
Builds the CLOB client and executes orders.

Uses the same auth pattern as polybot (py-clob-client, signature_type=1).
"""

from __future__ import annotations

import logging
from typing import Optional

from config import cfg

log = logging.getLogger("trader")


def build_clob_client():
    """Build and return an authenticated ClobClient."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        raise ImportError("Run: pip install py-clob-client")

    creds = None
    if cfg.poly_api_key and not cfg.poly_api_key.startswith("your_"):
        creds = ApiCreds(
            api_key=cfg.poly_api_key,
            api_secret=cfg.poly_api_secret,
            api_passphrase=cfg.poly_api_passphrase,
        )

    return ClobClient(
        host=cfg.clob_api_url,
        chain_id=cfg.chain_id,
        key=cfg.wallet_private_key,
        creds=creds,
        signature_type=1,
        funder=cfg.poly_funder_address or None,
    )


def place_market_buy(
    clob_client,
    token_id: str,
    usdc_size: float,
    price: float,
) -> Optional[dict]:
    """
    Place a GTC limit buy order at `price` for `usdc_size` USDC.
    Returns the order response dict, or None on failure.

    In dry_run mode, logs the intended order and returns a fake response.
    """
    shares = round(usdc_size / price, 4)

    if cfg.dry_run:
        log.info(
            f"[Trader][DRY RUN] Would BUY {shares:.4f} shares of {token_id[:10]}… "
            f"at ${price:.4f} (${usdc_size:.2f} USDC)"
        )
        return {
            "status": "dry_run",
            "token_id": token_id,
            "price": price,
            "size": shares,
            "usdc": usdc_size,
        }

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=shares,
            side="BUY",
        )
        signed_order = clob_client.create_order(order_args)
        response = clob_client.post_order(signed_order, OrderType.GTC)
        log.info(
            f"[Trader][LIVE] Order placed: {token_id[:10]}… "
            f"price={price:.4f} shares={shares:.4f} usdc=${usdc_size:.2f} | {response}"
        )
        return response

    except Exception as e:
        log.error(f"[Trader] Order failed for {token_id[:10]}…: {e}")
        return None
