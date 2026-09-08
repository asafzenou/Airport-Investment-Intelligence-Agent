"""Focused tests for the analytics service using a temporary SQLite database."""

import pytest

from data_pipeline.analytics.service import AnalyticsService
from data_pipeline.config import (
    ANALYSIS_WINDOW_MONTHS,
    CONGESTION_CANCEL_RATE_CAP,
    CONGESTION_DELAY_MIN_CAP,
    CONGESTION_DELAY_RATE_CAP,
    CONGESTION_WEIGHTS,
    EXPANSION_WEIGHTS,
    SFO_LOAD_FACTOR_CEIL,
    SFO_LOAD_FACTOR_FLOOR,
    SFO_SCORE_WEIGHTS,
)
from data_pipeline.dal.aviation_dal import AviationDAL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _airport(code: str, state: str, name: str = "") -> dict:
    return {
        "airport_code": code,
        "icao_code": f"K{code}",
        "airport_name": name or f"{code} Airport",
        "city": "Testcity",
        "state_code": state,
        "state_name": state,
        "region": None,
        "latitude": 42.0,
        "longitude": -71.0,
        "source_effective_date": "2024-01-01",
    }


def _traffic(code: str, year: int, month: int, passengers: int, seats: int) -> dict:
    return {
        "airport_code": code,
        "year": year,
        "month": month,
        "departures": passengers // 100,
        "passengers": passengers,
        "seats": seats,
        "load_factor": round(passengers / seats * 100, 2),
        "passengers_per_flight": round(passengers / (passengers // 100), 2),
    }


def _ops(
    code: str,
    year: int,
    month: int,
    scheduled: int,
    delayed: int,
    cancelled: int = 0,
    delay_min: float = 0.0,
) -> dict:
    return {
        "airport_code": code,
        "year": year,
        "month": month,
        "scheduled_flights": scheduled,
        "delayed_flights": delayed,
        "cancelled_flights": cancelled,
        "diverted_flights": 0,
        "average_departure_delay": delay_min / max(delayed, 1),
        "average_arrival_delay": 0.0,
        "carrier_delay_minutes": delay_min * 0.5,
        "weather_delay_minutes": delay_min * 0.2,
        "nas_delay_minutes": delay_min * 0.2,
        "security_delay_minutes": 0.0,
        "late_aircraft_delay_minutes": delay_min * 0.1,
    }


def _route(origin: str, dest: str, year: int, month: int, distance: float, departures: int) -> dict:
    return {
        "origin_airport": origin,
        "destination_airport": dest,
        "year": year,
        "month": month,
        "distance_miles": distance,
        "scheduled_departures": departures,
        "performed_departures": departures,
        "passengers": departures * 150,
        "seats": departures * 160,
    }


def _insert_traffic_months(
    dal: AviationDAL, code: str, months: list[tuple[int, int, int, int]]
) -> None:
    """months: list of (year, month, passengers, seats)."""
    rows = [_traffic(code, y, m, pax, seats) for y, m, pax, seats in months]
    dal.upsert_traffic(rows)


def _insert_ops_months(
    dal: AviationDAL,
    code: str,
    months: list[tuple[int, int, int, int, int, float]],
) -> None:
    """months: list of (year, month, scheduled, delayed, cancelled, total_delay_min)."""
    rows = [
        _ops(code, y, m, sched, delayed, canc, dmin)
        for y, m, sched, delayed, canc, dmin in months
    ]
    dal.upsert_operations(rows)


# ---------------------------------------------------------------------------
# Expansion scores – basic
# ---------------------------------------------------------------------------


def test_expansion_scores_empty_when_no_ne_airports(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("LAX", "CA")])
    svc = AnalyticsService(dal)
    assert svc.calculate_expansion_scores() == []


def test_expansion_scores_excluded_below_min_passengers(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("PVD", "RI")])
    dal.upsert_traffic([_traffic("PVD", 2024, 1, 50_000, 60_000)])
    svc = AnalyticsService(dal)
    assert svc.calculate_expansion_scores() == []


def test_expansion_scores_growth_null_without_previous_window(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2024, m, 500_000, 550_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    svc = AnalyticsService(dal)
    rows = svc.calculate_expansion_scores()
    assert len(rows) == 1
    assert rows[0]["passenger_growth_percent"] is None
    assert rows[0]["is_rankable"] == 0
    assert rows[0]["expansion_score"] is None


def test_expansion_scores_ranking_two_airports(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("BOS", "MA"), _airport("PVD", "RI")])

    bos_prev, bos_curr = 450_000, 500_000
    pvd_prev, pvd_curr = 120_000, 150_000

    bos_months = [(2023, m, bos_prev, 550_000) for m in range(1, 13)]
    bos_months += [(2024, m, bos_curr, 550_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", bos_months)

    pvd_months = [(2023, m, pvd_prev, 180_000) for m in range(1, 13)]
    pvd_months += [(2024, m, pvd_curr, 180_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "PVD", pvd_months)

    bos_ops = [(2024, m, 10_000, 2_000, 200, 120_000.0) for m in range(1, 13)]
    pvd_ops = [(2024, m, 2_000, 200, 20, 10_000.0) for m in range(1, 13)]
    _insert_ops_months(dal, "BOS", bos_ops)
    _insert_ops_months(dal, "PVD", pvd_ops)

    svc = AnalyticsService(dal)
    rows = svc.calculate_expansion_scores()

    assert len(rows) == 2
    codes = [r["airport_code"] for r in rows]
    assert "BOS" in codes and "PVD" in codes

    for r in rows:
        assert r["expansion_score"] is not None
        assert r["is_rankable"] == 1
        assert r["passenger_growth_percent"] is not None
        assert r["rank_position"] in (1, 2)

    assert rows[0]["rank_position"] == 1
    assert rows[1]["rank_position"] == 2
    assert rows[0]["expansion_score"] >= rows[1]["expansion_score"]


def test_expansion_scores_written_to_db(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2023, m, 400_000, 450_000) for m in range(1, 13)]
    months += [(2024, m, 450_000, 500_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    svc = AnalyticsService(dal)
    svc.run_all()

    rows = dal._db.fetchall("SELECT * FROM analytics_expansion_scores")
    assert len(rows) >= 1
    assert rows[0]["airport_code"] == "BOS"


# ---------------------------------------------------------------------------
# Expansion scores – incomplete traffic windows
# ---------------------------------------------------------------------------


def test_expansion_incomplete_current_window_growth_null(dal: AviationDAL) -> None:
    """Partial current window: growth and score must be NULL, is_rankable false."""
    dal.upsert_airports([_airport("BOS", "MA")])
    # Only 6 months in 2024 (Jan–Jun). t_latest=2024-06, so curr_keys spans
    # 2023-07 to 2024-06. Only 6 of those 12 months have data → incomplete.
    curr = [(2024, m, 500_000, 550_000) for m in range(1, 7)]
    _insert_traffic_months(dal, "BOS", curr)

    svc = AnalyticsService(dal)
    rows = svc.calculate_expansion_scores()

    assert len(rows) == 1
    r = rows[0]
    assert r["passenger_growth_percent"] is None
    assert r["is_rankable"] == 0
    assert r["expansion_score"] is None
    assert "incomplete" in r["limitations"].lower()


def test_expansion_incomplete_previous_window_growth_null(dal: AviationDAL) -> None:
    """Incomplete previous window: growth and score must be NULL, is_rankable false."""
    dal.upsert_airports([_airport("BOS", "MA")])
    # Full current window, but only 6 months of the previous window.
    prev = [(2023, m, 500_000, 550_000) for m in range(7, 13)]  # 6 months only
    curr = [(2024, m, 500_000, 550_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", prev + curr)

    svc = AnalyticsService(dal)
    rows = svc.calculate_expansion_scores()

    assert len(rows) == 1
    r = rows[0]
    assert r["passenger_growth_percent"] is None
    assert r["is_rankable"] == 0
    assert r["expansion_score"] is None


def test_expansion_unrankable_rows_have_null_rank(dal: AviationDAL) -> None:
    """Non-rankable airports must receive rank_position = None."""
    dal.upsert_airports([_airport("BOS", "MA"), _airport("PVD", "RI")])

    # BOS: full 24 months + ops → rankable
    bos = [(2023, m, 500_000, 550_000) for m in range(1, 13)]
    bos += [(2024, m, 500_000, 550_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", bos)
    _insert_ops_months(dal, "BOS", [(2024, m, 10_000, 2_000, 200, 120_000.0) for m in range(1, 13)])

    # PVD: only current 12 months → not rankable (no previous window)
    pvd = [(2024, m, 200_000, 250_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "PVD", pvd)

    svc = AnalyticsService(dal)
    rows = svc.calculate_expansion_scores()

    by_code = {r["airport_code"]: r for r in rows}
    assert by_code["BOS"]["rank_position"] is not None
    assert by_code["PVD"]["rank_position"] is None
    assert by_code["PVD"]["is_rankable"] == 0


def test_expansion_formula_uses_configured_weights(dal: AviationDAL) -> None:
    """Verify the expansion score formula uses EXPANSION_WEIGHTS constants."""
    assert abs(sum(EXPANSION_WEIGHTS.values()) - 1.0) < 1e-9
    assert EXPANSION_WEIGHTS["passenger_growth"] == pytest.approx(0.40)
    assert EXPANSION_WEIGHTS["load_factor"] == pytest.approx(0.30)
    assert EXPANSION_WEIGHTS["delay_rate"] == pytest.approx(0.20)
    assert EXPANSION_WEIGHTS["current_passengers"] == pytest.approx(0.10)
    assert ANALYSIS_WINDOW_MONTHS == 12


# ---------------------------------------------------------------------------
# Congestion
# ---------------------------------------------------------------------------


def test_congestion_empty_when_airport_missing(dal: AviationDAL) -> None:
    _insert_ops_months(dal, "LAX", [(2024, 6, 18_000, 3_600, 180, 200_000.0)])
    svc = AnalyticsService(dal)
    assert svc.calculate_congestion() == []


def test_congestion_two_rows_returned(dal: AviationDAL) -> None:
    _insert_ops_months(dal, "LAX", [(2024, 6, 18_000, 3_600, 180, 200_000.0)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 5_000, 500, 50, 30_000.0)])
    svc = AnalyticsService(dal)
    rows = svc.calculate_congestion()
    assert len(rows) == 2
    assert {r["airport_code"] for r in rows} == {"LAX", "SNA"}


def test_congestion_comparison_result_labels(dal: AviationDAL) -> None:
    _insert_ops_months(dal, "LAX", [(2024, 6, 10_000, 3_000, 100, 180_000.0)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 5_000, 250, 10, 5_000.0)])
    svc = AnalyticsService(dal)
    rows = svc.calculate_congestion()
    by_code = {r["airport_code"]: r for r in rows}
    assert by_code["LAX"]["comparison_result"] == "more_congested"
    assert by_code["SNA"]["comparison_result"] == "less_congested"


def test_congestion_equal_scores_yield_equally_congested(dal: AviationDAL) -> None:
    """Identical congestion indices must not favour LAX; both get equally_congested."""
    # Same numbers for both airports → same index.
    for code in ("LAX", "SNA"):
        _insert_ops_months(dal, code, [(2024, 6, 10_000, 2_000, 100, 90_000.0)])

    svc = AnalyticsService(dal)
    rows = svc.calculate_congestion()
    by_code = {r["airport_code"]: r for r in rows}
    assert by_code["LAX"]["comparison_result"] == "equally_congested"
    assert by_code["SNA"]["comparison_result"] == "equally_congested"
    assert by_code["LAX"]["congestion_index"] == pytest.approx(by_code["SNA"]["congestion_index"])


def test_congestion_index_value(dal: AviationDAL) -> None:
    delayed = 2_500
    total_delay = delayed * 45.0
    _insert_ops_months(dal, "LAX", [(2024, 6, 10_000, delayed, 200, total_delay)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 1_000, 10, 1, 100.0)])
    svc = AnalyticsService(dal)
    rows = svc.calculate_congestion()
    lax = next(r for r in rows if r["airport_code"] == "LAX")
    # dp=min(25/40,1)*100=62.5, cp=min(2/10,1)*100=20, sp=min(45/60,1)*100=75
    expected = round(0.50 * 62.5 + 0.20 * 20 + 0.30 * 75, 2)
    assert lax["congestion_index"] == pytest.approx(expected, abs=0.01)


def test_congestion_index_at_cap_values(dal: AviationDAL) -> None:
    """At the configured cap values each pressure component should reach 100 → index = 100."""
    delayed = int(10_000 * CONGESTION_DELAY_RATE_CAP / 100)        # 40% delay rate
    cancelled = int(10_000 * CONGESTION_CANCEL_RATE_CAP / 100)     # 10% cancel rate
    total_delay = delayed * CONGESTION_DELAY_MIN_CAP               # avg exactly at cap
    _insert_ops_months(dal, "LAX", [(2024, 6, 10_000, delayed, cancelled, total_delay)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 100, 1, 0, 10.0)])

    svc = AnalyticsService(dal)
    rows = svc.calculate_congestion()
    lax = next(r for r in rows if r["airport_code"] == "LAX")
    expected = round(
        CONGESTION_WEIGHTS["delay"] * 100
        + CONGESTION_WEIGHTS["cancellation"] * 100
        + CONGESTION_WEIGHTS["severity"] * 100,
        2,
    )
    assert lax["congestion_index"] == pytest.approx(expected, abs=0.01)


def test_congestion_formula_uses_configured_constants() -> None:
    """Sanity-check that the caps and weights match the documented formulas."""
    assert CONGESTION_DELAY_RATE_CAP == pytest.approx(40.0)
    assert CONGESTION_CANCEL_RATE_CAP == pytest.approx(10.0)
    assert CONGESTION_DELAY_MIN_CAP == pytest.approx(60.0)
    assert abs(sum(CONGESTION_WEIGHTS.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Long-haul
# ---------------------------------------------------------------------------


def test_long_haul_empty_when_no_anc_routes(dal: AviationDAL) -> None:
    svc = AnalyticsService(dal)
    assert svc.calculate_long_haul() == []


def test_long_haul_percentage(dal: AviationDAL) -> None:
    routes = [
        _route("ANC", "LAX", 2024, 3, 3_270.0, 60),
        _route("ANC", "SEA", 2024, 3, 2_600.0, 40),
        _route("ANC", "FAI", 2024, 3, 350.0, 100),
    ]
    dal.upsert_routes(routes)
    svc = AnalyticsService(dal)
    rows = svc.calculate_long_haul()
    assert len(rows) == 1
    r = rows[0]
    assert r["airport_code"] == "ANC"
    assert r["long_haul_flights"] == 100
    assert r["total_departing_flights"] == 200
    assert r["long_haul_percentage"] == pytest.approx(50.0)


def test_long_haul_null_when_zero_departures(dal: AviationDAL) -> None:
    dal.upsert_routes([_route("ANC", "LAX", 2024, 3, 3_270.0, 0)])
    svc = AnalyticsService(dal)
    rows = svc.calculate_long_haul()
    assert rows[0]["long_haul_percentage"] is None


# ---------------------------------------------------------------------------
# Unmet demand
# ---------------------------------------------------------------------------


def test_unmet_demand_empty_when_no_sfo_traffic(dal: AviationDAL) -> None:
    svc = AnalyticsService(dal)
    assert svc.calculate_unmet_demand() == []


def test_unmet_demand_insufficient_history_flag(dal: AviationDAL) -> None:
    months = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", months)
    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    assert len(rows) == 1
    r = rows[0]
    assert "INSUFFICIENT_HISTORY" in r["reason_flags"]
    assert r["passenger_growth_percent"] is None
    assert r["unmet_demand_score"] is None


def test_unmet_demand_flags_and_score(dal: AviationDAL) -> None:
    prev_months = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr_months = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev_months + curr_months)

    ops = [(2024, m, 10_000, 2_500, 300, 150_000.0) for m in range(1, 13)]
    _insert_ops_months(dal, "SFO", ops)

    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    assert len(rows) == 1
    r = rows[0]

    assert r["passenger_growth_percent"] is not None
    assert r["passenger_growth_percent"] > 0
    assert "POSITIVE_PASSENGER_GROWTH" in r["reason_flags"]
    assert "INSUFFICIENT_HISTORY" not in r["reason_flags"]
    assert r["load_factor_percent"] is not None
    assert r["unmet_demand_score"] is not None
    assert 0 <= r["unmet_demand_score"] <= 100
    assert r["target_load_factor_percent"] == pytest.approx(85.0)


def test_unmet_demand_high_load_factor_flag(dal: AviationDAL) -> None:
    prev_months = [(2023, m, 3_000_000, 3_400_000) for m in range(1, 13)]
    curr_months = [(2024, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev_months + curr_months)

    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    r = rows[0]
    assert "HIGH_LOAD_FACTOR" in r["reason_flags"]
    assert r["estimated_additional_seats_needed"] is not None


def test_unmet_demand_score_null_when_delay_rate_missing(dal: AviationDAL) -> None:
    """Missing operational data must produce NULL score, not a score with zero delay."""
    prev = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev + curr)
    # No ops data inserted → delay_rate and cancel_rate are None.

    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    r = rows[0]

    assert r["delay_rate_percent"] is None
    assert r["cancellation_rate_percent"] is None
    assert r["unmet_demand_score"] is None, (
        "score must be NULL when delay/cancellation data is absent, not zero-substituted"
    )
    # Raw traffic metrics and flags are still returned.
    assert r["passenger_growth_percent"] is not None
    assert r["load_factor_percent"] is not None


def test_unmet_demand_score_null_when_cancellation_rate_missing(dal: AviationDAL) -> None:
    """score must be NULL when any one required component is unavailable."""
    prev = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev + curr)
    # Ops with zero scheduled flights → rates are None.
    ops = [(2024, m, 0, 0, 0, 0.0) for m in range(1, 13)]
    _insert_ops_months(dal, "SFO", ops)

    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    r = rows[0]
    assert r["unmet_demand_score"] is None


def test_unmet_demand_different_traffic_and_ops_periods(dal: AviationDAL) -> None:
    """Ops covering only part of the traffic window are noted in limitations."""
    prev = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev + curr)
    # Only 1 month of ops (June 2024) within the 12-month traffic window.
    _insert_ops_months(dal, "SFO", [(2024, 6, 10_000, 2_000, 100, 60_000.0)])

    svc = AnalyticsService(dal)
    rows = svc.calculate_unmet_demand()
    r = rows[0]

    # Traffic window is a full 12 months (Jan–Dec 2024).
    assert r["period_start"] == "2024-01"
    assert r["period_end"] == "2024-12"
    # Ops period (1 month) is disclosed in limitations.
    assert "2024-06" in r["limitations"]
    assert "1 month" in r["limitations"]


def test_unmet_demand_sfo_score_formula_uses_configured_weights() -> None:
    """SFO score weights must match the documented formula."""
    assert abs(sum(SFO_SCORE_WEIGHTS.values()) - 1.0) < 1e-9
    assert SFO_SCORE_WEIGHTS["load"] == pytest.approx(0.45)
    assert SFO_SCORE_WEIGHTS["growth"] == pytest.approx(0.25)
    assert SFO_SCORE_WEIGHTS["delay"] == pytest.approx(0.20)
    assert SFO_SCORE_WEIGHTS["cancellation"] == pytest.approx(0.10)
    assert SFO_LOAD_FACTOR_FLOOR == pytest.approx(70.0)
    assert SFO_LOAD_FACTOR_CEIL == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# run_all transaction behaviour
# ---------------------------------------------------------------------------


def test_run_all_returns_correct_counts(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2023, m, 400_000, 450_000) for m in range(1, 13)]
    months += [(2024, m, 450_000, 500_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    _insert_ops_months(dal, "LAX", [(2024, 6, 18_000, 3_600, 180, 200_000.0)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 5_000, 500, 50, 30_000.0)])

    dal.upsert_routes([_route("ANC", "LAX", 2024, 3, 3_270.0, 60)])

    prev = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev + curr)

    svc = AnalyticsService(dal)
    counts = svc.run_all()

    assert counts["analytics_expansion_scores"] >= 1
    assert counts["analytics_congestion"] == 2
    assert counts["analytics_long_haul"] == 1
    assert counts["analytics_unmet_demand"] == 1


def test_run_all_replaces_previous_results(dal: AviationDAL) -> None:
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2024, m, 450_000, 500_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    svc = AnalyticsService(dal)
    svc.run_all()
    svc.run_all()

    rows = dal._db.fetchall("SELECT * FROM analytics_expansion_scores")
    codes = [r["airport_code"] for r in rows]
    assert codes.count("BOS") == 1


def test_run_all_atomic_replacement(dal: AviationDAL) -> None:
    """Second run replaces all four tables atomically in a single transaction."""
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2023, m, 400_000, 450_000) for m in range(1, 13)]
    months += [(2024, m, 450_000, 500_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    _insert_ops_months(dal, "LAX", [(2024, 6, 18_000, 3_600, 180, 200_000.0)])
    _insert_ops_months(dal, "SNA", [(2024, 6, 5_000, 500, 50, 30_000.0)])
    dal.upsert_routes([_route("ANC", "LAX", 2024, 3, 3_270.0, 60)])
    prev = [(2023, m, 3_500_000, 4_000_000) for m in range(1, 13)]
    curr = [(2024, m, 4_000_000, 4_400_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "SFO", prev + curr)

    svc = AnalyticsService(dal)
    counts1 = svc.run_all()
    counts2 = svc.run_all()

    # Row counts must be identical after two complete runs.
    assert counts1 == counts2
    # No duplicate rows in any table.
    for table in (
        "analytics_expansion_scores",
        "analytics_congestion",
        "analytics_long_haul",
        "analytics_unmet_demand",
    ):
        rows = dal._db.fetchall(f"SELECT * FROM {table}")
        assert len(rows) == counts1[table]


def test_run_all_rolls_back_on_write_failure(dal: AviationDAL, tmp_path) -> None:
    """Previous analytics must survive a failed write."""
    dal.upsert_airports([_airport("BOS", "MA")])
    months = [(2024, m, 450_000, 500_000) for m in range(1, 13)]
    _insert_traffic_months(dal, "BOS", months)

    svc = AnalyticsService(dal)
    svc.run_all()

    initial = dal._db.fetchall("SELECT * FROM analytics_expansion_scores")
    assert len(initial) >= 1

    class BrokenService(AnalyticsService):
        def calculate_expansion_scores(self):
            rows = super().calculate_expansion_scores()
            for r in rows:
                r["calculated_at"] = None  # violates NOT NULL
            return rows

    broken = BrokenService(dal)
    with pytest.raises(Exception):
        broken.run_all()

    after = dal._db.fetchall("SELECT * FROM analytics_expansion_scores")
    assert len(after) == len(initial)
