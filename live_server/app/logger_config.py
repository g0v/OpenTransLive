# This file is part of g0v/realtime_transcribe.
# Copyright (c) 2025 Sean Gau
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import logging
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

_TW_TZ = timezone(timedelta(hours=8))


class _TaiwanFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_TW_TZ)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


def setup_logger(name: str = __name__, log_file: str | Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """
    Setup centralized logging configuration.

    Args:
        name: Logger name
        log_file: Optional log file path (str or Path). If None, creates default log file
        level: Logging level (default: INFO)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    # Create log directory if needed
    if log_file is None:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"opentranslive_{datetime.now().strftime('%Y%m%d')}.log"

    # File handler - logs everything including sensitive details
    file_formatter = _TaiwanFormatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console handler - logs only non-sensitive info
    console_formatter = _TaiwanFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger


def log_exception(logger: logging.Logger, exc: Exception, context: str = "") -> None:
    """
    Log exception details server-side only.

    Args:
        logger: Logger instance
        exc: Exception to log
        context: Additional context string
    """
    if context:
        logger.error(f"{context}: {type(exc).__name__}: {str(exc)}", exc_info=True)
    else:
        logger.error(f"{type(exc).__name__}: {str(exc)}", exc_info=True)


def get_generic_error_message(exc: Exception | None = None) -> str:
    """
    Return a generic error message for client responses.

    Args:
        exc: Optional exception (not included in message)

    Returns:
        Generic error message string
    """
    return "An internal error occurred. Please try again later."


def get_generic_error_dict(error_type: str = "internal_error") -> dict:
    """
    Return a generic error dictionary for JSON responses.

    Args:
        error_type: Error type identifier (generic, not detailed)

    Returns:
        Generic error dictionary
    """
    return {
        "error": error_type,
        "message": "An error occurred while processing your request."
    }
