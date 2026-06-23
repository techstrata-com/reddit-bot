import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LA_TZ = ZoneInfo("America/Los_Angeles")


def log_dir() -> Path:
    configured = os.getenv("JOB_LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT.parent / "reddit-bot-logs"


class DailyJobLogger:
    """One log file per calendar day (LA timezone), next to the project directory."""

    def __init__(self, calendar_date: str | None = None):
        self.calendar_date = calendar_date or datetime.now(LA_TZ).strftime("%Y-%m-%d")
        self.log_path = log_dir() / f"{self.calendar_date}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(f"reddit-bot.job.{self.calendar_date}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()
        self._logger.propagate = False

        file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        self._logger.addHandler(file_handler)

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(
            logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")
        )
        self._logger.addHandler(console)

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warning(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str, *, exc: Exception | None = None) -> None:
        if exc:
            self._logger.error(f"{message}: {exc}", exc_info=True)
        else:
            self._logger.error(message)

    def section(self, title: str) -> None:
        line = "=" * 60
        self._logger.info(line)
        self._logger.info(title)
        self._logger.info(line)

    @property
    def path(self) -> Path:
        return self.log_path
