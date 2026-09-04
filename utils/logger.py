from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional

# Reconfigure stdout / stderr to UTF-8 on Windows consoles to prevent charmap UnicodeEncodeErrors
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class SensitiveDataFilter(logging.Filter):
    """
    Security filter that intercepts log records and redacts any sensitive patterns,
    such as Telegram Bot API tokens, passwords, cookies, or auth headers.
    """

    PATTERNS = [
        # Telegram Bot Token format: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
        (re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{35}\b'), '[REDACTED_TELEGRAM_TOKEN]'),
        # Generic API tokens or secrets in URLs
        (re.compile(r'(bot)(\d{8,10}:[A-Za-z0-9_-]{35})', re.IGNORECASE), r'\1[REDACTED_TOKEN]'),
        # Password / secret keys in key-value strings
        (re.compile(r'(password|secret|token|api_key|cookie|pin|otp)=([^&\s]+)', re.IGNORECASE), r'\1=[REDACTED]'),
        # Bearer tokens
        (re.compile(r'Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*', re.IGNORECASE), 'Bearer [REDACTED]'),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, replacement in self.PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        if record.args:
            if isinstance(record.args, dict):
                sanitized_args = {}
                for k, v in record.args.items():
                    if any(s in k.lower() for s in ('password', 'token', 'secret', 'cookie', 'otp', 'pin')):
                        sanitized_args[k] = '[REDACTED]'
                    elif isinstance(v, str):
                        val = v
                        for pattern, replacement in self.PATTERNS:
                            val = pattern.sub(replacement, val)
                        sanitized_args[k] = val
                    else:
                        sanitized_args[k] = v
                record.args = sanitized_args
            elif isinstance(record.args, tuple):
                sanitized_list = []
                for v in record.args:
                    if isinstance(v, str):
                        val = v
                        for pattern, replacement in self.PATTERNS:
                            val = pattern.sub(replacement, val)
                        sanitized_list.append(val)
                    else:
                        sanitized_list.append(v)
                record.args = tuple(sanitized_list)
        return True


def setup_logger(
    log_level: str = "INFO",
    log_file_path: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configures and initializes the root application logger with both console
    and rotating file handlers, equipped with sensitive data sanitization.
    """
    logger = logging.getLogger("broker_monitor")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Avoid duplicate handlers if setup is called multiple times
    if logger.handlers:
        return logger

    # Log format with timestamp, level, module name, and message
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sensitive_filter = SensitiveDataFilter()

    # 1. Console Handler (Standard Output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    logger.addHandler(console_handler)

    # 2. File Handler (Rotating log file)
    if log_file_path:
        log_path = Path(log_file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(sensitive_filter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Returns a child logger for a specific module under 'broker_monitor'.
    """
    return logging.getLogger(f"broker_monitor.{name}")
