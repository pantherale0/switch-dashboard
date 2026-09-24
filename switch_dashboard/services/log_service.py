import os
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from switch_dashboard.config import LOG_FILE_PATH, LOG_DIR, get_setting

logger = logging.getLogger("switch_dashboard.services.log")

LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "NONE": 99,
}


def setup_logging(level_name: Optional[str] = None):
    if not level_name:
        level_name = get_setting("log_level", "INFO")

    level = LEVEL_MAP.get(str(level_name).upper(), logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(level)
    logging.getLogger("werkzeug").setLevel(level)
    logging.getLogger("scraper").setLevel(level)
    logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
    logging.getLogger("scapy.loading").setLevel(logging.ERROR)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: could not initialize file logger: {e}")


def get_current_log_level() -> str:
    level_no = logging.getLogger().level
    for name, val in LEVEL_MAP.items():
        if val == level_no:
            return name
    return "INFO"


def set_log_level(level_name: str):
    level = LEVEL_MAP.get(level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers:
        h.setLevel(level)


def read_recent_logs(max_lines: int = 500) -> List[str]:
    if not os.path.exists(LOG_FILE_PATH):
        return []
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return [line.rstrip("\r\n") for line in lines[-max_lines:]]
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return [f"Error reading logs: {e}"]


def clear_logs() -> bool:
    try:
        if os.path.exists(LOG_FILE_PATH):
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                f.truncate(0)
        return True
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        return False
