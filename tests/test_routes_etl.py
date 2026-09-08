"""Tests for RoutesETL: transform, load, extract (limitation), and run."""

import pytest

from data_pipeline.config import LONG_HAUL_MILES
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.routes_etl import RoutesETL

# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_transform_basic_aggregation(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {
            "YEAR": "2025", "MONTH": "1", "ORIGIN": "ANC", "DEST": "LAX",
            "DISTANCE": "3270", "DEPARTURES_SCHEDULED": "10", "DEPARTURES_PERFORMED": "9",
            "PASSENGERS": "1000", "SEATS": "1100", "CLASS": "F",
        },
        {
            "YEAR": "2025", "MONTH": "1", "ORIGIN": "ANC", "DEST": "LAX",
            "DISTANCE": "3270", "DEPARTURES_SCHEDULED": "20", "DEPARTURES_PERFORMED": "20",
            "PASSENGERS": "2000", "SEATS": "2200", "CLASS": "F",
        },
    ]
    rows = etl.transform(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["origin_airport"] == "ANC"
    assert r["destination_airport"] == "LAX"
    assert r["scheduled_departures"] == 30
    assert r["performed_departures"] == 29
    assert r["passengers"] == 3000
    assert r["distance_miles"] == pytest.approx(3270.0)


def test_transform_filters_non_passenger_class(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {
            "YEAR": "2025", "MONTH": "1", "ORIGIN": "ANC", "DEST": "SEA",
            "DISTANCE": "1400", "DEPARTURES_SCHEDULED": "5", "DEPARTURES_PERFORMED": "5",
            "PASSENGERS": "0", "SEATS": "0", "CLASS": "G",  # cargo
        }
    ]
    assert etl.transform(raw) == []


def test_transform_long_haul_threshold() -> None:
    assert LONG_HAUL_MILES == 2_500.0


def test_transform_skips_missing_origin_or_dest(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {
            "YEAR": "2025", "MONTH": "1", "ORIGIN": "", "DEST": "LAX",
            "DISTANCE": "500", "DEPARTURES_SCHEDULED": "5", "DEPARTURES_PERFORMED": "5",
            "PASSENGERS": "100", "SEATS": "110", "CLASS": "F",
        }
    ]
    assert etl.transform(raw) == []


def test_transform_different_months_not_merged(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {
            "YEAR": "2025", "MONTH": "1", "ORIGIN": "SFO", "DEST": "LAX",
            "DISTANCE": "337", "DEPARTURES_SCHEDULED": "100", "DEPARTURES_PERFORMED": "98",
            "PASSENGERS": "9000", "SEATS": "10000", "CLASS": "F",
        },
        {
            "YEAR": "2025", "MONTH": "2", "ORIGIN": "SFO", "DEST": "LAX",
            "DISTANCE": "337", "DEPARTURES_SCHEDULED": "90", "DEPARTURES_PERFORMED": "88",
            "PASSENGERS": "8000", "SEATS": "9000", "CLASS": "F",
        },
    ]
    assert len(etl.transform(raw)) == 2


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_upserts_routes(dal: AviationDAL, tmp_db: SQLiteHandler) -> None:
    etl = RoutesETL(dal, None)
    rows = etl.transform(
        [
            {
                "YEAR": "2025", "MONTH": "1", "ORIGIN": "ANC", "DEST": "LAX",
                "DISTANCE": "3270", "DEPARTURES_SCHEDULED": "30",
                "DEPARTURES_PERFORMED": "29", "PASSENGERS": "5000",
                "SEATS": "5400", "CLASS": "F",
            }
        ]
    )
    count = etl.load(rows)
    assert count == 1
    stored = tmp_db.fetchall("SELECT * FROM routes")
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# extract() — documented source limitation
# ---------------------------------------------------------------------------


async def test_extract_raises_runtime_error(dal: AviationDAL) -> None:
    """extract() always raises RuntimeError because the T-100 source cannot be automated."""
    import httpx

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        with pytest.raises(RuntimeError, match="T-100 Segment data cannot be downloaded"):
            await etl.extract()


# ---------------------------------------------------------------------------
# run() — error is recorded, existing data is preserved
# ---------------------------------------------------------------------------


async def test_run_records_error_in_sync_state(dal: AviationDAL) -> None:
    import httpx

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        await etl.run()

    state = dal.get_sync_state("routes")
    assert state is not None
    assert state["status"] == "error"
    assert "T-100" in (state["error_message"] or "")


async def test_run_preserves_existing_routes_on_failure(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    """Pre-existing route rows must survive a failed run()."""
    import httpx

    # Load one route directly via the DAL
    dal.upsert_routes(
        [
            {
                "origin_airport": "ANC", "destination_airport": "LAX",
                "year": 2024, "month": 1, "distance_miles": 3270.0,
                "scheduled_departures": 30, "performed_departures": 29,
                "passengers": 5000, "seats": 5400,
            }
        ]
    )

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        await etl.run()

    stored = tmp_db.fetchall("SELECT * FROM routes")
    assert len(stored) == 1, "Pre-existing route was wiped by a failed run()"
