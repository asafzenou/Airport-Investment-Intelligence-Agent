"""Tests for AirportMetadataETL: transform, load, extract, and run."""

import httpx
import pytest
import respx

from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_metadata_etl import (
    ENDPOINT,
    PAGE_SIZE,
    AirportMetadataETL,
)


def _feature(arpt_id: str, state: str = "CA", **kwargs) -> dict:
    attrs = {
        "ARPT_ID": arpt_id,
        "ICAO_ID": f"K{arpt_id}",
        "ARPT_NAME": f"{arpt_id} Airport",
        "CITY": "Testville",
        "STATE_CODE": state,
        "STATE_NAME": "Test State",
        "LAT_DECIMAL": 34.0,
        "LONG_DECIMAL": -118.0,
        "EFF_DATE": "2024-01-01",
    }
    attrs.update(kwargs)
    return {"attributes": attrs}


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_transform_basic_fields(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    rows = etl.transform([_feature("LAX")])
    assert len(rows) == 1
    r = rows[0]
    assert r["airport_code"] == "LAX"
    assert r["icao_code"] == "KLAX"
    assert r["region"] is None  # CA is not New England


def test_transform_new_england_region(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    for state in ("CT", "ME", "MA", "NH", "RI", "VT"):
        rows = etl.transform([_feature("TMP", state=state)])
        assert rows[0]["region"] == "New England", f"Expected New England for {state}"


def test_transform_skips_missing_code(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    rows = etl.transform([{"attributes": {"ARPT_ID": "", "ARPT_NAME": "No Code"}}])
    assert rows == []


def test_transform_strips_whitespace(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    rows = etl.transform([_feature("  BOS  ", state="MA")])
    assert rows[0]["airport_code"] == "BOS"


def test_transform_empty_icao_becomes_none(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    feature = _feature("SFO")
    feature["attributes"]["ICAO_ID"] = ""
    rows = etl.transform([feature])
    assert rows[0]["icao_code"] is None


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_upserts_airports(dal: AviationDAL) -> None:
    etl = AirportMetadataETL(dal, None)
    rows = etl.transform([_feature("LAX"), _feature("BOS", state="MA")])
    count = etl.load(rows)
    assert count == 2
    stored = dal._db.fetchall("SELECT airport_code FROM airports ORDER BY airport_code")
    assert [r["airport_code"] for r in stored] == ["BOS", "LAX"]


# ---------------------------------------------------------------------------
# extract() – mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
async def test_extract_single_page(dal: AviationDAL) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"features": [_feature("LAX")], "exceededTransferLimit": False},
        )
    )
    async with httpx.AsyncClient() as client:
        etl = AirportMetadataETL(dal, client)
        records = await etl.extract()
    assert len(records) == 1
    assert records[0]["attributes"]["ARPT_ID"] == "LAX"


@respx.mock
async def test_extract_paginates(dal: AviationDAL) -> None:
    first_page = [_feature(f"A{i:04d}") for i in range(PAGE_SIZE)]
    second_page = [_feature("LAST")]
    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200, json={"features": first_page, "exceededTransferLimit": True}
            )
        return httpx.Response(
            200, json={"features": second_page, "exceededTransferLimit": False}
        )

    respx.get(ENDPOINT).mock(side_effect=side_effect)
    async with httpx.AsyncClient() as client:
        etl = AirportMetadataETL(dal, client)
        records = await etl.extract()
    assert len(records) == PAGE_SIZE + 1
    assert call_count == 2


@respx.mock
async def test_extract_raises_on_http_error(dal: AviationDAL) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            etl = AirportMetadataETL(dal, client)
            await etl.extract()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@respx.mock
async def test_run_populates_db_and_sets_sync_state(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"features": [_feature("LAX"), _feature("BOS", state="MA")],
                  "exceededTransferLimit": False},
        )
    )
    async with httpx.AsyncClient() as client:
        etl = AirportMetadataETL(dal, client)
        await etl.run()

    rows = tmp_db.fetchall("SELECT airport_code FROM airports ORDER BY airport_code")
    assert [r["airport_code"] for r in rows] == ["BOS", "LAX"]
    state = dal.get_sync_state("airport_metadata")
    assert state is not None
    assert state["status"] == "success"
    assert state["rows_loaded"] == 2


@respx.mock
async def test_run_records_error_and_preserves_data(
    dal: AviationDAL, tmp_db: SQLiteHandler
) -> None:
    # Seed a row
    dal.upsert_airports(
        [{"airport_code": "SFO", "airport_name": "SF Intl", "icao_code": None,
          "city": None, "state_code": None, "state_name": None, "region": None,
          "latitude": None, "longitude": None, "source_effective_date": None}]
    )
    respx.get(ENDPOINT).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        etl = AirportMetadataETL(dal, client)
        await etl.run()

    # Existing row must still be there
    rows = tmp_db.fetchall("SELECT airport_code FROM airports")
    assert len(rows) == 1
    state = dal.get_sync_state("airport_metadata")
    assert state is not None
    assert state["status"] == "error"


@respx.mock
async def test_run_skips_when_fresh(dal: AviationDAL) -> None:
    # Mark as recently synced
    dal.update_sync_state("airport_metadata", status="success", rows_loaded=10)

    calls: list[httpx.Request] = []

    def side_effect(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"features": [], "exceededTransferLimit": False})

    respx.get(ENDPOINT).mock(side_effect=side_effect)
    async with httpx.AsyncClient() as client:
        etl = AirportMetadataETL(dal, client)
        await etl.run()

    assert len(calls) == 0
