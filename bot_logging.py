"""Shared logging setup for the local bot scripts."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent / "logs"


def _time_gmtime(*args):
    return time.gmtime(*args)


def get_bot_logger(bot_name: str) -> logging.Logger:
    safe_name = "".join(
        character if character.isalnum() or character in ("-", "_") else "_"
        for character in (bot_name or "bot").strip().lower()
    )
    logger = logging.getLogger(f"bots.{safe_name or 'bot'}")

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        fmt="[%(asctime)s UTC] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter.converter = _time_gmtime

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    LOG_DIR.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / f"{safe_name or 'bot'}.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger
