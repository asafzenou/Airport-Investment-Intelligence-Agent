"""Tests for the centralized logging configuration."""

import logging
from pathlib import Path

import pytest

import data_pipeline.logger as logger_module
from data_pipeline.logger import _LOG_PATTERN, configure_logging

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _LOGS_DIR to a temp directory and reset root logger handlers."""
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(logger_module, "_LOGS_DIR", logs_dir)

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    root.setLevel(logging.NOTSET)

    yield logs_dir

    for h in root.handlers:
        h.close()
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Handler presence tests
# ---------------------------------------------------------------------------


def test_console_and_file_handlers_are_added(isolated_logs: Path) -> None:
    log_path = configure_logging()

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    # Use exact type to exclude pytest's LogCaptureHandler (a StreamHandler subclass).
    stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]

    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1
    assert log_path.exists()


def test_timestamped_log_file_name(isolated_logs: Path) -> None:
    log_path = configure_logging()
    assert _LOG_PATTERN.match(log_path.name), f"unexpected name: {log_path.name}"
    assert log_path.exists()


# ---------------------------------------------------------------------------
# Idempotency / duplicate-handler prevention
# ---------------------------------------------------------------------------


def test_no_duplicate_handlers_on_repeated_call(isolated_logs: Path) -> None:
    configure_logging()
    configure_logging()  # second call must be a no-op

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]

    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1


def test_repeated_call_returns_same_path(isolated_logs: Path) -> None:
    path1 = configure_logging()
    path2 = configure_logging()
    assert path1 == path2


# ---------------------------------------------------------------------------
# Log-file retention
# ---------------------------------------------------------------------------


def test_only_two_latest_log_files_kept(isolated_logs: Path) -> None:
    # Pre-create three old log files with ascending timestamps.
    isolated_logs.mkdir(parents=True, exist_ok=True)
    for ts in ["20260101_000000", "20260102_000000", "20260103_000000"]:
        (isolated_logs / f"data_pipeline_{ts}.log").write_text("old")

    configure_logging()  # creates a 4th (newest) file, then cleans up

    remaining = [f for f in isolated_logs.iterdir() if _LOG_PATTERN.match(f.name)]
    assert len(remaining) == 2, f"expected 2, got {len(remaining)}: {[f.name for f in remaining]}"
    # The two newest by name must survive.
    names = sorted(f.name for f in remaining)
    assert "data_pipeline_20260101_000000.log" not in names
    assert "data_pipeline_20260102_000000.log" not in names


def test_cleanup_does_not_touch_unrelated_files(isolated_logs: Path) -> None:
    isolated_logs.mkdir(parents=True, exist_ok=True)
    unrelated = isolated_logs / "other_log.log"
    unrelated.write_text("keep me")

    configure_logging()

    assert unrelated.exists(), "cleanup must not delete unrelated files"


# ---------------------------------------------------------------------------
# Exception logging
# ---------------------------------------------------------------------------


def test_exception_logged_with_traceback_in_file(isolated_logs: Path) -> None:
    log_path = configure_logging()
    logger = logging.getLogger("test.exc")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("Something went wrong")

    # Flush all handlers.
    for h in logging.getLogger().handlers:
        h.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "Something went wrong" in content
    assert "ValueError: boom" in content
    assert "Traceback" in content


# ---------------------------------------------------------------------------
# Airport-operations per-month progress messages
# ---------------------------------------------------------------------------

_CSV_HEADER = (
    "Year,Month,Origin,DepDelay,ArrDelay,Cancelled,Diverted,"
    "CarrierDelay,WeatherDelay,NASDelay,SecurityDelay,LateAircraftDelay"
)
_TEST_MONTHS = [(2026, 6), (2026, 5)]


def _ops_index_html(months: list[tuple[int, int]]) -> str:
    lines = []
    for y, m in months:
        fn = (
            f"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{y}_{m}.zip"
        )
        lines.append(f'<a href="/PREZIP/{fn}">{fn}</a>')
    return "\n".join(lines)


