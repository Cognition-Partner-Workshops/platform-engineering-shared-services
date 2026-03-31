"""Logging configuration for Oracle DB Agent."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from oracle_db_agent.config import AgentConfig


def setup_logging(config: AgentConfig, verbose: bool = False) -> logging.Logger:
    """Configure logging with rotating file handler and console output."""
    root_logger = logging.getLogger("oracle_db_agent")
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Clear existing handlers
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (rotating)
    log_file = config.alerts.log_file
    log_dir = Path(log_file).parent
    if log_dir.exists() or _try_create_log_dir(log_dir):
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=config.alerts.log_max_bytes,
            backupCount=config.alerts.log_backup_count,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    else:
        root_logger.warning("Could not create log directory %s; file logging disabled.", log_dir)

    return root_logger


def _try_create_log_dir(log_dir: Path) -> bool:
    """Attempt to create the log directory."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        return True
    except OSError:
        return False
