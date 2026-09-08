"""Shared fixtures for all test modules."""

import io
import zipfile
from pathlib import Path

import pytest

from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler


@pytest.fixture()
def tmp_db(tmp_path: Path) -> SQLiteHandler:
    return SQLiteHandler(tmp_path / "test.db")


@pytest.fixture()
def dal(tmp_db: SQLiteHandler) -> AviationDAL:
    return AviationDAL(tmp_db)


def make_zip(filename: str, csv_content: str) -> bytes:
    """Return an in-memory ZIP containing one CSV file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, csv_content)
    return buf.getvalue()
