"""Shared logging configuration for the entire project.

Call configure_logging() once at startup (or let run_pipeline() do it).
Safe to call multiple times — handlers are added only once per session.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

_LOGS_DIR = Path(__file__).parent.parent / "logs"
_LOG_PREFIX = "data_pipeline_"
_LOG_PATTERN = re.compile(r"^data_pipeline_\d{8}_\d{6}\.log$")
_MAX_LOG_FILES = 2

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> Path:
    """Set up console + timestamped file logging on the root logger.

    Returns the path of the log file created for this session.
    Idempotent: a second call returns the existing file path unchanged.
    """
    root = logging.getLogger()

    # Idempotency: return the existing pipeline file handler if present.
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and _LOG_PATTERN.match(
            Path(h.baseFilename).name
        ):
            return Path(h.baseFilename)

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOGS_DIR / f"{_LOG_PREFIX}{timestamp}.log"

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    root.setLevel(logging.INFO)

    # Console handler — skip if one already exists (e.g. from basicConfig).
    # Use exact type check to avoid mistaking pytest's LogCaptureHandler for our handler.
    has_stream = any(type(h) is logging.StreamHandler for h in root.handlers)
    if not has_stream:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Keep third-party HTTP logs concise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _cleanup_old_logs()

    return log_path


def _cleanup_old_logs() -> None:
    """Delete all but the two most recent data_pipeline_*.log files."""
    candidates = sorted(
        (f for f in _LOGS_DIR.iterdir() if _LOG_PATTERN.match(f.name)),
        key=lambda f: f.name,
        reverse=True,
    )
    for old in candidates[_MAX_LOG_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass
