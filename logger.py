"""
logger.py
─────────
Configures the root logger (console + rotating file).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from config import cfg


def setup_logging(level: int = logging.INFO) -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), level)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        Path(cfg.log_file),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


_log = logging.getLogger("notify")


def log_trade(
    *,
    action: str,
    question: str,
    side: str,
    price: float,
    usdc_size: float,
    bankroll: float,
    confidence: float,
    dry_run: bool,
) -> None:
    pct = usdc_size / bankroll * 100 if bankroll > 0 else 0
    mode = "DRY RUN" if dry_run else "LIVE"
    _log.info(
        f"[{mode}] {action} | side={side} price={price:.3f} "
        f"size=${usdc_size:.2f} ({pct:.1f}%) conf={confidence:.2%} | {question[:80]}"
    )
