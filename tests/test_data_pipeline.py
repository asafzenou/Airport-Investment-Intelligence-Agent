"""Integration tests for data_pipeline: mocked HTTP, real temporary SQLite DB."""

from pathlib import Path

import httpx
import respx

from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.data_pipeline import run_pipeline
from data_pipeline.etls.airport_metadata_etl import ENDPOINT as META_ENDPOINT
from data_pipeline.etls.airport_operations_etl import (
    _PREZIP_INDEX,
)
from data_pipeline.etls.airport_operations_etl import (
    _prezip_url as ops_url,
)
from data_pipeline.etls.airport_traffic_etl import ENDPOINT as TRAFFIC_ENDPOINT
from tests.conftest import make_zip

# 12 months of operations data (newest-first matches PREZIP index ordering).
_OPS_MONTHS = [
    (2026, 6), (2026, 5), (2026, 4), (2026, 3), (2026, 2), (2026, 1),
    (2025, 12), (2025, 11), (2025, 10), (2025, 9), (2025, 8), (2025, 7),
]
_OPS_CSV_HEADER = (
    "Year,Month,Origin,DepDelay,ArrDelay,Cancelled,Diverted,"
    "CarrierDelay,WeatherDelay,NASDelay,SecurityDelay,LateAircraftDelay"
)


def _ops_index_html(months: list[tuple[int, int]]) -> str:
    lines = []
    for y, m in months:
        fn = (
            f"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{y}_{m}.zip"
        )
        lines.append(f'<a href="/PREZIP/{fn}">{fn}</a>')
    return "\n".join(lines)


def _mock_all_sources() -> None:
    """Register respx mocks for metadata, traffic, and operations.

    Routes ETL always raises RuntimeError (documented limitation — no HTTP).
    """
    # Airport metadata
    respx.get(META_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "attributes": {
                            "ARPT_ID": "LAX",
                            "ICAO_ID": "KLAX",
                            "ARPT_NAME": "Los Angeles Intl",
                            "CITY": "Los Angeles",
                            "STATE_CODE": "CA",
                            "STATE_NAME": "California",
                            "LAT_DECIMAL": 33.94,
                            "LONG_DECIMAL": -118.40,
                            "EFF_DATE": "2024-01-01",
                        }
                    },
                    {
                        "attributes": {
                            "ARPT_ID": "BOS",
                            "ICAO_ID": "KBOS",
                            "ARPT_NAME": "Boston Logan Intl",
                            "CITY": "Boston",
                            "STATE_CODE": "MA",
                            "STATE_NAME": "Massachusetts",
                            "LAT_DECIMAL": 42.36,
                            "LONG_DECIMAL": -71.01,
                            "EFF_DATE": "2024-01-01",
                        }
                    },
                ],
                "exceededTransferLimit": False,
            },
        )
    )

    # Airport traffic (Socrata)
    respx.get(TRAFFIC_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "origin_airport_code": "LAX",
                    "year": "2024",
                    "reporting_month": "2024-06-01T00:00:00.000",
                    "total_departures": "10000",
                    "total_passengers": "800000",
                    "total_seats": "900000",
                    "total_load_factor": "88.9",
                    "total_passengers_flight": "80.0",
                }
            ],
        )
    )

    # Operations: PREZIP index + one ZIP per month
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_ops_index_html(_OPS_MONTHS))
    )
    for m_year, m_month in _OPS_MONTHS:
        ops_csv = "\n".join(
            [
                _OPS_CSV_HEADER,
                f"{m_year},{m_month},LAX,10,8,0,0,0,0,0,0,0",
            ]
        )
        zip_bytes = make_zip(f"On_Time_{m_year}_{m_month}.csv", ops_csv)
        respx.get(ops_url(m_year, m_month)).mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )


@respx.mock
async def test_pipeline_populates_all_tables(tmp_path: Path) -> None:
    _mock_all_sources()
    db_path = tmp_path / "aviation.db"
    await run_pipeline(db_path=db_path)

    handler = SQLiteHandler(db_path)
    airports = handler.fetchall("SELECT * FROM airports")
    assert len(airports) == 2
    codes = {r["airport_code"] for r in airports}
    assert "LAX" in codes
    assert "BOS" in codes

    # BOS is in Massachusetts → New England
    bos = handler.fetchone("SELECT region FROM airports WHERE airport_code='BOS'")
    assert bos is not None
    assert bos["region"] == "New England"

    traffic = handler.fetchall("SELECT * FROM airport_traffic")
    assert len(traffic) == 1

    ops = handler.fetchall("SELECT * FROM airport_operations")
    assert len(ops) == 12  # one per month in the 12-month window

    sync = handler.fetchall("SELECT dataset_name, status FROM sync_state ORDER BY dataset_name")
    statuses = {r["dataset_name"]: r["status"] for r in sync}

    # Three datasets load successfully.
    for name in ("airport_metadata", "airport_traffic", "airport_operations"):
        assert statuses.get(name) == "success", f"{name} not success: {statuses}"

    # Routes always records an error (T-100 source cannot be automated).
    assert statuses.get("routes") == "error", f"routes should be error: {statuses}"


@respx.mock
async def test_pipeline_preserves_data_on_source_failure(tmp_path: Path) -> None:
    """If a source fails on second run, previously loaded rows must be kept."""
    _mock_all_sources()
    db_path = tmp_path / "aviation.db"

    # First run loads data successfully (routes will error, which is expected).
    await run_pipeline(db_path=db_path)

    airports_before = SQLiteHandler(db_path).fetchall("SELECT * FROM airports")
    assert len(airports_before) == 2

    # Second run: metadata endpoint fails, others succeed.
    # The datasets that were just synced are fresh and skip; only metadata is retried.
    respx.get(META_ENDPOINT).mock(return_value=httpx.Response(500))

    await run_pipeline(db_path=db_path)

    # Airports table must still have the 2 rows from the first run.
    airports_after = SQLiteHandler(db_path).fetchall("SELECT * FROM airports")
    assert len(airports_after) == 2

    sync = SQLiteHandler(db_path).fetchone(
        "SELECT status FROM sync_state WHERE dataset_name='airport_metadata'"
    )
    assert sync is not None


@respx.mock
async def test_pipeline_skips_fresh_datasets(tmp_path: Path) -> None:
    """A dataset that was just synced should not trigger another HTTP request."""
    _mock_all_sources()
    db_path = tmp_path / "aviation.db"

    await run_pipeline(db_path=db_path)

    # Track how many times metadata endpoint is called on second run.
    meta_calls: list[httpx.Request] = []

    def meta_side_effect(req: httpx.Request) -> httpx.Response:
        meta_calls.append(req)
        return httpx.Response(200, json={"features": [], "exceededTransferLimit": False})

    respx.get(META_ENDPOINT).mock(side_effect=meta_side_effect)

    await run_pipeline(db_path=db_path)

    # Metadata has a 7-day refresh window; it should be skipped on the second run.
    assert len(meta_calls) == 0
