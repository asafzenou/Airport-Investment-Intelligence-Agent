"""Tests for AviationDAL: upserts and sync-state helpers."""


from data_pipeline.dal.aviation_dal import AviationDAL


def _airport(code: str = "LAX", name: str = "LA Intl") -> dict:
    return {
        "airport_code": code,
        "icao_code": f"K{code}",
        "airport_name": name,
        "city": "Testville",
        "state_code": "CA",
        "state_name": "California",
        "region": None,
        "latitude": 33.9,
        "longitude": -118.4,
        "source_effective_date": "2024-01-01",
    }


def test_upsert_airports_inserts_and_updates(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("SFO", "San Francisco Old Name")])
    dal.upsert_airports([_airport("SFO", "San Francisco Intl")])

    rows = dal._db.fetchall("SELECT * FROM airports WHERE airport_code='SFO'")
    assert len(rows) == 1
    assert rows[0]["airport_name"] == "San Francisco Intl"


def test_upsert_traffic(dal: AviationDAL) -> None:
    rows = [
        {
            "airport_code": "LAX",
            "year": 2024,
            "month": 1,
            "departures": 10_000,
            "passengers": 800_000,
            "seats": 900_000,
            "load_factor": 88.9,
            "passengers_per_flight": 80.0,
        }
    ]
    count = dal.upsert_traffic(rows)
    assert count == 1
    # Upsert again with updated passengers
    rows[0]["passengers"] = 850_000
    dal.upsert_traffic(rows)
    result = dal._db.fetchone(
        "SELECT passengers FROM airport_traffic WHERE airport_code='LAX' AND year=2024 AND month=1"
    )
    assert result is not None
    assert result["passengers"] == 850_000


def test_upsert_routes(dal: AviationDAL) -> None:
    rows = [
        {
            "origin_airport": "ANC",
            "destination_airport": "LAX",
            "year": 2024,
            "month": 3,
            "distance_miles": 3270.0,
            "scheduled_departures": 60,
            "performed_departures": 58,
            "passengers": 9860,
            "seats": 10440,
        }
    ]
    count = dal.upsert_routes(rows)
    assert count == 1


def test_upsert_operations(dal: AviationDAL) -> None:
    rows = [
        {
            "airport_code": "LAX",
            "year": 2024,
            "month": 6,
            "scheduled_flights": 18000,
            "delayed_flights": 3600,
            "cancelled_flights": 180,
            "diverted_flights": 36,
            "average_departure_delay": 14.2,
            "average_arrival_delay": 12.1,
            "carrier_delay_minutes": 54000.0,
            "weather_delay_minutes": 18000.0,
            "nas_delay_minutes": 27000.0,
            "security_delay_minutes": 900.0,
            "late_aircraft_delay_minutes": 36000.0,
        }
    ]
    count = dal.upsert_operations(rows)
    assert count == 1


def test_needs_refresh_when_no_state(dal: AviationDAL) -> None:
    assert dal.needs_refresh("airport_metadata", max_age_hours=168) is True


def test_needs_refresh_false_after_recent_success(dal: AviationDAL) -> None:
    dal.update_sync_state("airport_metadata", status="success", rows_loaded=5000)
    assert dal.needs_refresh("airport_metadata", max_age_hours=168) is False


def test_needs_refresh_true_after_error(dal: AviationDAL) -> None:
    dal.update_sync_state("airport_metadata", status="error", error_message="timeout")
    # Error sync never sets last_successful_sync, so it still needs refresh
    assert dal.needs_refresh("airport_metadata", max_age_hours=168) is True


def test_update_sync_state_preserves_last_sync_on_error(dal: AviationDAL) -> None:
    dal.update_sync_state("airport_traffic", status="success", rows_loaded=100)
    first = dal.get_sync_state("airport_traffic")
    assert first is not None
    last_sync_before = first["last_successful_sync"]

    dal.update_sync_state("airport_traffic", status="error", error_message="network failure")
    after = dal.get_sync_state("airport_traffic")
    assert after is not None
    # last_successful_sync must not be wiped on error
    assert after["last_successful_sync"] == last_sync_before
    assert after["status"] == "error"
    assert after["error_message"] == "network failure"


def test_needs_refresh_true_when_status_error_despite_recent_success(dal: AviationDAL) -> None:
    dal.update_sync_state("airport_operations", status="success", rows_loaded=100)
    dal.update_sync_state("airport_operations", status="error", error_message="timeout")
    # last_successful_sync is still set, but status=error means data is stale
    assert dal.needs_refresh("airport_operations", max_age_hours=168) is True


def test_update_sync_state_rows_loaded(dal: AviationDAL) -> None:
    dal.update_sync_state("routes", status="success", rows_loaded=42)
    state = dal.get_sync_state("routes")
    assert state is not None
    assert state["rows_loaded"] == 42


# ------------------------------------------------------------------
# get_row_count
# ------------------------------------------------------------------


def test_get_row_count_empty(dal: AviationDAL) -> None:
    assert dal.get_row_count("airports") == 0


def test_get_row_count_after_insert(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("JFK"), _airport("LAX")])
    assert dal.get_row_count("airports") == 2


def test_get_row_count_unknown_table_raises(dal: AviationDAL) -> None:
    import pytest
    with pytest.raises(ValueError, match="Unknown table"):
        dal.get_row_count("nonexistent_table")


# ------------------------------------------------------------------
# get_timeseries_stats
# ------------------------------------------------------------------


def _traffic_row(airport: str, year: int, month: int) -> dict:
    return {
        "airport_code": airport,
        "year": year,
        "month": month,
        "departures": 100,
        "passengers": 8000,
        "seats": 9000,
        "load_factor": 88.9,
        "passengers_per_flight": 80.0,
    }


def test_get_timeseries_stats_empty(dal: AviationDAL) -> None:
    result = dal.get_timeseries_stats("airport_traffic")
    assert result == {
        "count": 0,
        "earliest_year": None, "earliest_month": None,
        "latest_year": None, "latest_month": None,
        "distinct_months": 0,
    }


def test_get_timeseries_stats_single_row(dal: AviationDAL) -> None:
    dal.upsert_traffic([_traffic_row("LAX", 2024, 6)])
    result = dal.get_timeseries_stats("airport_traffic")
    assert result["count"] == 1
    assert result["earliest_year"] == 2024
    assert result["earliest_month"] == 6
    assert result["latest_year"] == 2024
    assert result["latest_month"] == 6
    assert result["distinct_months"] == 1


def test_get_timeseries_stats_picks_earliest_and_latest_period(dal: AviationDAL) -> None:
    dal.upsert_traffic([
        _traffic_row("LAX", 2023, 9),
        _traffic_row("LAX", 2025, 3),
        _traffic_row("SFO", 2025, 1),
    ])
    result = dal.get_timeseries_stats("airport_traffic")
    assert result["earliest_year"] == 2023
    assert result["earliest_month"] == 9
    assert result["latest_year"] == 2025
    assert result["latest_month"] == 3
    assert result["distinct_months"] == 3


def test_get_timeseries_stats_invalid_table_raises(dal: AviationDAL) -> None:
    import pytest
    with pytest.raises(ValueError, match="year/month"):
        dal.get_timeseries_stats("airports")


def test_get_timeseries_stats_routes(dal: AviationDAL) -> None:
    dal.upsert_routes([
        {
            "origin_airport": "ANC",
            "destination_airport": "SEA",
            "year": 2024,
            "month": 12,
            "distance_miles": 1450.0,
            "scheduled_departures": 30,
            "performed_departures": 29,
            "passengers": 4800,
            "seats": 5200,
        }
    ])
    result = dal.get_timeseries_stats("routes")
    assert result["latest_year"] == 2024
    assert result["latest_month"] == 12
