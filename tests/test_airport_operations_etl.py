"""Tests for AirportOperationsETL: transform, load, extract, and run."""

import httpx
import pytest
import respx

from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_operations_etl import (
    _PREZIP_INDEX,
    AirportOperationsETL,
    _available_months,
    _prezip_url,
)
from tests.conftest import make_zip

# Real BTS CSV column names (title-case, not all-caps).
_CSV_HEADER = (
    "Year,Month,Origin,DepDelay,ArrDelay,Cancelled,Diverted,"
    "CarrierDelay,WeatherDelay,NASDelay,SecurityDelay,LateAircraftDelay"
)

# 12 test months newest-first; mirrors what a real PREZIP index would expose.
_TEST_MONTHS = [
    (2026, 6), (2026, 5), (2026, 4), (2026, 3), (2026, 2), (2026, 1),
    (2025, 12), (2025, 11), (2025, 10), (2025, 9), (2025, 8), (2025, 7),
]


def _csv_row(
    year: int = 2025,
    month: int = 1,
    origin: str = "LAX",
    dep_delay: float = 10.0,
    arr_delay: float = 8.0,
    cancelled: int = 0,
    diverted: int = 0,
    carrier: float = 0.0,
    weather: float = 0.0,
    nas: float = 0.0,
    security: float = 0.0,
    late: float = 0.0,
) -> str:
    return (
        f"{year},{month},{origin},{dep_delay},{arr_delay},{cancelled},{diverted},"
        f"{carrier},{weather},{nas},{security},{late}"
    )


def _index_html(months: list[tuple[int, int]]) -> str:
    """Build minimal PREZIP index HTML containing the given (year, month) pairs."""
    lines = []
    for y, m in months:
        fn = (
            f"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{y}_{m}.zip"
        )
        lines.append(f'<a href="/PREZIP/{fn}">{fn}</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _available_months()
# ---------------------------------------------------------------------------


@respx.mock
async def test_available_months_parses_prezip_index() -> None:
    html = _index_html([(2026, 6), (2026, 5), (2025, 12)])
    respx.get(_PREZIP_INDEX).mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as client:
        months = await _available_months(client)
    assert months == [(2026, 6), (2026, 5), (2025, 12)]


@respx.mock
async def test_available_months_returns_empty_when_no_match() -> None:
    respx.get(_PREZIP_INDEX).mock(return_value=httpx.Response(200, text="<html>no files</html>"))
    async with httpx.AsyncClient() as client:
        months = await _available_months(client)
    assert months == []


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_transform_aggregates_flights(dal: AviationDAL) -> None:
    etl = AirportOperationsETL(dal, None)
    raw = [
        {
            "Year": "2025", "Month": "1", "Origin": "LAX",
            "DepDelay": "5", "ArrDelay": "3", "Cancelled": "0", "Diverted": "0",
            "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
            "SecurityDelay": "0", "LateAircraftDelay": "0",
        },
        {
            "Year": "2025", "Month": "1", "Origin": "LAX",
            "DepDelay": "20", "ArrDelay": "18", "Cancelled": "0", "Diverted": "0",
            "CarrierDelay": "10", "WeatherDelay": "0", "NASDelay": "5",
            "SecurityDelay": "0", "LateAircraftDelay": "5",
        },
        {
            "Year": "2025", "Month": "1", "Origin": "LAX",
            "DepDelay": "0", "ArrDelay": "0", "Cancelled": "1", "Diverted": "0",
            "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
            "SecurityDelay": "0", "LateAircraftDelay": "0",
        },
    ]
    rows = etl.transform(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["airport_code"] == "LAX"
    assert r["scheduled_flights"] == 3
    assert r["cancelled_flights"] == 1
    assert r["delayed_flights"] == 1  # only the 20-min flight exceeds 15-min threshold
    assert r["average_departure_delay"] == pytest.approx((5 + 20) / 2)
    assert r["nas_delay_minutes"] == pytest.approx(5.0)


def test_transform_skips_missing_origin(dal: AviationDAL) -> None:
    etl = AirportOperationsETL(dal, None)
    raw = [{"Year": "2025", "Month": "1", "Origin": "", "DepDelay": "5",
            "ArrDelay": "3", "Cancelled": "0", "Diverted": "0",
            "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
            "SecurityDelay": "0", "LateAircraftDelay": "0"}]
    assert etl.transform(raw) == []


def test_transform_cancelled_excluded_from_delay_avg(dal: AviationDAL) -> None:
    etl = AirportOperationsETL(dal, None)
    raw = [{
        "Year": "2025", "Month": "2", "Origin": "SFO",
        "DepDelay": "0", "ArrDelay": "0", "Cancelled": "1", "Diverted": "0",
        "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
        "SecurityDelay": "0", "LateAircraftDelay": "0",
    }]
    rows = etl.transform(raw)
    assert len(rows) == 1
    assert rows[0]["average_departure_delay"] is None


def test_transform_two_airports_separate_rows(dal: AviationDAL) -> None:
    etl = AirportOperationsETL(dal, None)
    raw = [
        {"Year": "2025", "Month": "1", "Origin": "LAX", "DepDelay": "5",
         "ArrDelay": "3", "Cancelled": "0", "Diverted": "0",
         "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
         "SecurityDelay": "0", "LateAircraftDelay": "0"},
        {"Year": "2025", "Month": "1", "Origin": "SFO", "DepDelay": "2",
         "ArrDelay": "1", "Cancelled": "0", "Diverted": "0",
         "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
         "SecurityDelay": "0", "LateAircraftDelay": "0"},
    ]
    assert len(etl.transform(raw)) == 2


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_upserts_operations(dal: AviationDAL, tmp_db: SQLiteHandler) -> None:
    etl = AirportOperationsETL(dal, None)
    raw = [{
        "Year": "2025", "Month": "1", "Origin": "LAX",
        "DepDelay": "10", "ArrDelay": "8", "Cancelled": "0", "Diverted": "0",
        "CarrierDelay": "0", "WeatherDelay": "0", "NASDelay": "0",
        "SecurityDelay": "0", "LateAircraftDelay": "0",
    }]
    count = etl.load(etl.transform(raw))
    assert count == 1
    stored = tmp_db.fetchall("SELECT * FROM airport_operations")
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# extract() – mocked PREZIP index + individual month ZIPs
# ---------------------------------------------------------------------------


@respx.mock
async def test_extract_reads_latest_12_months(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    for year, month in _TEST_MONTHS:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=year, month=month)])
        zip_bytes = make_zip(f"On_Time_{year}_{month}.csv", csv_content)
        respx.get(_prezip_url(year, month)).mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

    async with httpx.AsyncClient() as client:
        etl = AirportOperationsETL(dal, client)
        records = await etl.extract()

    assert len(records) == len(_TEST_MONTHS)


@respx.mock
async def test_extract_sets_latest_period(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    for year, month in _TEST_MONTHS:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=year, month=month)])
        zip_bytes = make_zip(f"On_Time_{year}_{month}.csv", csv_content)
        respx.get(_prezip_url(year, month)).mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

    async with httpx.AsyncClient() as client:
        etl = AirportOperationsETL(dal, client)
        await etl.extract()

    assert etl._latest_period == "2026-06"


@respx.mock
async def test_extract_raises_when_index_empty(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(return_value=httpx.Response(200, text="<html>empty</html>"))
    with pytest.raises(ValueError, match="No marketing-carrier"):
        async with httpx.AsyncClient() as client:
            etl = AirportOperationsETL(dal, client)
            await etl.extract()


@respx.mock
async def test_extract_raises_on_http_error(dal: AviationDAL) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    # First month returns 503; rest return valid ZIPs
    year, month = _TEST_MONTHS[0]
    respx.get(_prezip_url(year, month)).mock(return_value=httpx.Response(503))
    for y, m in _TEST_MONTHS[1:]:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=y, month=m)])
        zip_bytes = make_zip(f"On_Time_{y}_{m}.csv", csv_content)
        respx.get(_prezip_url(y, m)).mock(return_value=httpx.Response(200, content=zip_bytes))

    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            etl = AirportOperationsETL(dal, client)
            await etl.extract()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@respx.mock
async def test_run_populates_db_and_records_latest_period(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    respx.get(_PREZIP_INDEX).mock(
        return_value=httpx.Response(200, text=_index_html(_TEST_MONTHS))
    )
    for year, month in _TEST_MONTHS:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=year, month=month)])
        zip_bytes = make_zip(f"On_Time_{year}_{month}.csv", csv_content)
        respx.get(_prezip_url(year, month)).mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )

    async with httpx.AsyncClient() as client:
        etl = AirportOperationsETL(dal, client)
        await etl.run()

    stored = tmp_db.fetchall("SELECT * FROM airport_operations")
    assert len(stored) == len(_TEST_MONTHS)
    state = dal.get_sync_state("airport_operations")
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
    for y, m in _TEST_MONTHS[1:]:
        csv_content = "\n".join([_CSV_HEADER, _csv_row(year=y, month=m)])
        zip_bytes = make_zip(f"On_Time_{y}_{m}.csv", csv_content)
        respx.get(_prezip_url(y, m)).mock(return_value=httpx.Response(200, content=zip_bytes))

    async with httpx.AsyncClient() as client:
        etl = AirportOperationsETL(dal, client)
        await etl.run()

    state = dal.get_sync_state("airport_operations")
    assert state is not None
    assert state["status"] == "error"
