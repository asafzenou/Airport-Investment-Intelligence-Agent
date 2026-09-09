"""Tests for RoutesETL: transform, load, extract (On-Time PREZIP), and run."""

import httpx
import pytest
import respx

from data_pipeline.config import LONG_HAUL_MILES, OPERATIONS_MONTHS_WINDOW
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_operations_etl import _PREZIP_INDEX, _prezip_url
from data_pipeline.etls.routes_etl import RoutesETL
from tests.conftest import make_zip

_CSV_HEADER = "Year,Month,Origin,Dest,Distance,Cancelled"

_TEST_MONTHS = [
    (2026, 6), (2026, 5), (2026, 4), (2026, 3), (2026, 2), (2026, 1),
    (2025, 12), (2025, 11), (2025, 10), (2025, 9), (2025, 8), (2025, 7),
]


def _csv_row(
    year: int = 2025,
    month: int = 1,
    origin: str = "ANC",
    dest: str = "LAX",
    distance: float = 3270.0,
    cancelled: int = 0,
) -> str:
    return f"{year},{month},{origin},{dest},{distance},{cancelled}"


def _index_html(months: list[tuple[int, int]]) -> str:
    lines = []
    for y, m in months:
        fn = (
            f"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{y}_{m}.zip"
        )
        lines.append(f'<a href="/PREZIP/{fn}">{fn}</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_transform_aggregates_by_route(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "LAX",
         "Distance": "3270", "Cancelled": "0"},
        {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "LAX",
         "Distance": "3270", "Cancelled": "0"},
    ]
    rows = etl.transform(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["origin_airport"] == "ANC"
    assert r["destination_airport"] == "LAX"
    assert r["scheduled_departures"] == 2
    assert r["performed_departures"] == 2
    assert r["distance_miles"] == pytest.approx(3270.0)


def test_transform_cancelled_increments_scheduled_only(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "SEA",
         "Distance": "1400", "Cancelled": "0"},
        {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "SEA",
         "Distance": "1400", "Cancelled": "1"},
    ]
    rows = etl.transform(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["scheduled_departures"] == 2
    assert r["performed_departures"] == 1


def test_transform_passengers_and_seats_are_null(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "LAX",
         "Distance": "3270", "Cancelled": "0"},
    ]
    rows = etl.transform(raw)
    assert len(rows) == 1
    assert rows[0]["passengers"] is None
    assert rows[0]["seats"] is None


def test_transform_long_haul_threshold() -> None:
    assert LONG_HAUL_MILES == 2_500.0


def test_transform_skips_missing_origin_or_dest(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "", "Dest": "LAX",
         "Distance": "500", "Cancelled": "0"},
    ]
    assert etl.transform(raw) == []


def test_transform_different_months_not_merged(dal: AviationDAL) -> None:
    etl = RoutesETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "SFO", "Dest": "LAX",
         "Distance": "337", "Cancelled": "0"},
        {"Year": "2025", "Month": "2", "Origin": "SFO", "Dest": "LAX",
         "Distance": "337", "Cancelled": "0"},
    ]
    assert len(etl.transform(raw)) == 2


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_upserts_routes(dal: AviationDAL, tmp_db: SQLiteHandler) -> None:
    etl = RoutesETL(dal, None)
    rows = etl.transform(
        [
            {"Year": "2025", "Month": "1", "Origin": "ANC", "Dest": "LAX",
             "Distance": "3270", "Cancelled": "0"},
        ]
    )
    count = etl.load(rows)
    assert count == 1
    stored = tmp_db.fetchall("SELECT * FROM routes")
    assert len(stored) == 1
    assert stored[0]["passengers"] is None
    assert stored[0]["seats"] is None


# ---------------------------------------------------------------------------
# extract() — mocked PREZIP index + ZIP/CSV
# ---------------------------------------------------------------------------


@respx.mock
async def test_extract_reads_single_month(dal: AviationDAL) -> None:
    year, month = _TEST_MONTHS[0]
    csv_content = "\n".join([_CSV_HEADER, _csv_row(year=year, month=month)])
    zip_bytes = make_zip(f"On_Time_{year}_{month}.csv", csv_content)
    respx.get(_prezip_url(year, month)).mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        records = await etl.extract(year, month, 1, 1)

    assert len(records) == 1


@respx.mock
async def test_run_records_error_when_index_empty(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(return_value=httpx.Response(200, text="<html>empty</html>"))
    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        await etl.run()

    state = dal.get_sync_state("routes")
    assert state is not None
    assert state["status"] == "error"


@respx.mock
async def test_extract_raises_on_http_error(dal: AviationDAL) -> None:
    year, month = _TEST_MONTHS[0]
    respx.get(_prezip_url(year, month)).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            etl = RoutesETL(dal, client)
            await etl.extract(year, month, 1, 1)


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@respx.mock
async def test_run_populates_db_and_records_success(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    for year, month in _TEST_MONTHS[:OPERATIONS_MONTHS_WINDOW]:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=year, month=month)])
        zip_bytes = make_zip(f"On_Time_{year}_{month}.csv", csv_content)
        respx.get(_prezip_url(year, month)).mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        await etl.run()

    stored = tmp_db.fetchall("SELECT * FROM routes")
    assert len(stored) == OPERATIONS_MONTHS_WINDOW
    state = dal.get_sync_state("routes")
    assert state is not None
    assert state["status"] == "success"
    assert state["latest_source_period"] == "2026-06"


@respx.mock
async def test_run_records_error_on_http_failure(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    year, month = _TEST_MONTHS[0]
    respx.get(_prezip_url(year, month)).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        etl = RoutesETL(dal, client)
        await etl.run()

    state = dal.get_sync_state("routes")
    assert state is not None
    assert state["status"] == "error"
