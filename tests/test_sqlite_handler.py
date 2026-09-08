"""Tests for SQLiteHandler: schema, transactions, and rollback."""

import pytest

from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler


def test_schema_created(tmp_db: SQLiteHandler) -> None:
    tables = {
        row["name"]
        for row in tmp_db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"airports", "airport_traffic", "routes", "airport_operations", "sync_state"} <= tables


def test_executemany_returns_row_count(tmp_db: SQLiteHandler) -> None:
    rows = [
        {
            "airport_code": "LAX",
            "icao_code": "KLAX",
            "airport_name": "Los Angeles Intl",
            "city": "Los Angeles",
            "state_code": "CA",
            "state_name": "California",
            "region": None,
            "latitude": 33.94,
            "longitude": -118.40,
            "source_effective_date": "2024-01-01",
        }
    ]
    sql = """
    INSERT INTO airports (
        airport_code, icao_code, airport_name, city, state_code, state_name,
        region, latitude, longitude, source_effective_date
    ) VALUES (
        :airport_code, :icao_code, :airport_name, :city, :state_code, :state_name,
        :region, :latitude, :longitude, :source_effective_date
    )
    """
    count = tmp_db.executemany(sql, rows)
    assert count == 1


def test_executemany_upsert_does_not_duplicate(tmp_db: SQLiteHandler) -> None:
    sql = """
    INSERT INTO airports (airport_code, airport_name)
    VALUES (:airport_code, :airport_name)
    ON CONFLICT(airport_code) DO UPDATE SET airport_name = excluded.airport_name
    """
    tmp_db.executemany(sql, [{"airport_code": "SFO", "airport_name": "Original"}])
    tmp_db.executemany(sql, [{"airport_code": "SFO", "airport_name": "Updated"}])

    row = tmp_db.fetchone("SELECT airport_name FROM airports WHERE airport_code='SFO'")
    assert row is not None
    assert row["airport_name"] == "Updated"
    rows = tmp_db.fetchall("SELECT * FROM airports WHERE airport_code='SFO'")
    assert len(rows) == 1


def test_rollback_on_error(tmp_db: SQLiteHandler) -> None:
    # Insert a valid row first
    tmp_db.executemany(
        "INSERT INTO airports (airport_code, airport_name) VALUES (:airport_code, :airport_name)",
        [{"airport_code": "BOS", "airport_name": "Boston Logan"}],
    )
    # Bad SQL should raise and not leave partial state
    with pytest.raises(Exception):
        tmp_db.executemany("THIS IS NOT SQL", [{}])

    # Original row still present
    row = tmp_db.fetchone("SELECT airport_code FROM airports WHERE airport_code='BOS'")
    assert row is not None


def test_fetchone_returns_none_when_missing(tmp_db: SQLiteHandler) -> None:
    row = tmp_db.fetchone("SELECT * FROM airports WHERE airport_code='XXXX'")
    assert row is None
