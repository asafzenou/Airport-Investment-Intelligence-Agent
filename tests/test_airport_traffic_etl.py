"""Tests for AirportTrafficETL: transform, load, extract, and run."""

import httpx
import pytest
import respx

from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_traffic_etl import ENDPOINT, PAGE_SIZE, AirportTrafficETL


def _raw(
    code: str = "LAX",
    year: str = "2024",
    month_str: str = "2024-06-01T00:00:00.000",
    departures: str = "10000",
    passengers: str = "800000",
    seats: str = "900000",
    load_factor: str = "88.9",
    ppf: str = "80.0",
) -> dict:
    return {
        "origin_airport_code": code,
        "year": year,
        "reporting_month": month_str,
        "total_departures": departures,
        "total_passengers": passengers,
        "total_seats": seats,
        "total_load_factor": load_factor,
        "total_passengers_flight": ppf,
    }


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_transform_basic(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    rows = etl.transform([_raw()])
    assert len(rows) == 1
    r = rows[0]
    assert r["airport_code"] == "LAX"
    assert r["year"] == 2024
    assert r["month"] == 6
    assert r["passengers"] == 800_000
    assert r["load_factor"] == pytest.approx(88.9)


def test_transform_rejects_negative_passengers(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    assert etl.transform([_raw(passengers="-1")]) == []


def test_transform_rejects_negative_seats(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    assert etl.transform([_raw(seats="-100")]) == []


def test_transform_rejects_load_factor_over_100(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    assert etl.transform([_raw(load_factor="101")]) == []


def test_transform_allows_zero_load_factor(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    assert len(etl.transform([_raw(load_factor="0")])) == 1


def test_transform_skips_missing_airport_code(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    assert etl.transform([_raw(code="")]) == []


def test_transform_handles_none_numeric_fields(dal: AviationDAL) -> None:
    etl = AirportTrafficETL(dal, None)
    item = _raw()
    item["total_departures"] = None
    rows = etl.transform([item])
    assert len(rows) == 1
    assert rows[0]["departures"] is None


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_upserts_traffic(dal: AviationDAL, tmp_db: SQLiteHandler) -> None:
    etl = AirportTrafficETL(dal, None)
    rows = etl.transform([_raw()])
    count = etl.load(rows)
    assert count == 1
    stored = tmp_db.fetchall("SELECT * FROM airport_traffic")
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# extract() - mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
async def test_extract_single_page(dal: AviationDAL) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[_raw()]))
    async with httpx.AsyncClient() as client:
        etl = AirportTrafficETL(dal, client)
        records = await etl.extract()
    assert len(records) == 1


@respx.mock
async def test_extract_paginates(dal: AviationDAL) -> None:
    first_batch = [_raw(code=f"A{i:04d}") for i in range(PAGE_SIZE)]
    second_batch = [_raw(code="LAST")]
    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=first_batch if call_count == 1 else second_batch)

    respx.get(ENDPOINT).mock(side_effect=side_effect)
    async with httpx.AsyncClient() as client:
        etl = AirportTrafficETL(dal, client)
        records = await etl.extract()
    assert len(records) == PAGE_SIZE + 1
    assert call_count == 2


@respx.mock
async def test_extract_raises_on_http_error(dal: AviationDAL) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            etl = AirportTrafficETL(dal, client)
            await etl.extract()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@respx.mock
async def test_run_populates_db_and_sets_sync_state(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[_raw()]))
    async with httpx.AsyncClient() as client:
        etl = AirportTrafficETL(dal, client)
        await etl.run()

    rows = tmp_db.fetchall("SELECT * FROM airport_traffic")
    assert len(rows) == 1
    state = dal.get_sync_state("airport_traffic")
    assert state is not None
    assert state["status"] == "success"


@respx.mock
async def test_run_records_error_on_http_failure(dal: AviationDAL) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        etl = AirportTrafficETL(dal, client)
        await etl.run()

    state = dal.get_sync_state("airport_traffic")
    assert state is not None
    assert state["status"] == "error"
