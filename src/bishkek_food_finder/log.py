"""Logging setup for all modules.

Two modes:
- setup_logging(): Batch scripts (scrapers, indexers). Timestamped log files.
- setup_service_logging(): Long-running services (bot, agent). Rotating log files.
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

NOISY_LOGGERS = ["httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers"]


def setup_logging(
    script_name: str,
    log_dir: str = LOG_DIR,
    console_level: int = logging.INFO,
) -> logging.Logger:
    """Configure logging for batch scripts.

    Creates: logs/{script_name}_{timestamp}.log
    File: DEBUG, Console: console_level (default INFO).
    """
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, console_handler],
        force=True,
    )

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    print(f"Logging to: {log_file}")
    return logging.getLogger("bishkek_food_finder")


def setup_service_logging(
    service_name: str,
    log_dir: str = LOG_DIR,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Configure logging for long-running services (bot, agent).

    Creates: logs/{service_name}.log (rotating, 10MB x 3 backups).
    File: DEBUG, Console: WARNING.
    """
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"{service_name}.log")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    logger = logging.getLogger(f"bishkek_food_finder.{service_name}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
