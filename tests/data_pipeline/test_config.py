"""Tests for data_pipeline/config.py constants."""

import pytest

from data_pipeline.config import EXPANSION_SCORE_WEIGHTS, NEW_ENGLAND_STATES


def test_new_england_states_exact_membership() -> None:
    assert NEW_ENGLAND_STATES == {"CT", "ME", "MA", "NH", "RI", "VT"}


def test_new_england_states_is_frozenset() -> None:
    assert isinstance(NEW_ENGLAND_STATES, frozenset)


def test_expansion_weights_sum_to_one() -> None:
    assert sum(EXPANSION_SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_expansion_weights_all_between_zero_and_one() -> None:
    for name, weight in EXPANSION_SCORE_WEIGHTS.items():
        assert 0.0 <= weight <= 1.0, f"Weight '{name}' = {weight} is out of [0, 1]"


def test_region_new_england_states_all_classify(dal) -> None:
    from data_pipeline.etls.airport_metadata_etl import AirportMetadataETL

    etl = AirportMetadataETL(dal, None)
    for state in NEW_ENGLAND_STATES:
        rows = etl.transform(
            [{"attributes": {
                "ARPT_ID": "TST",
                "ICAO_ID": "KTST",
                "ARPT_NAME": "Test Airport",
                "CITY": "Testville",
                "STATE_CODE": state,
                "STATE_NAME": "Test State",
                "LAT_DECIMAL": 42.0,
                "LONG_DECIMAL": -72.0,
                "EFF_DATE": "2024-01-01",
            }}]
        )
        assert rows[0]["region"] == "New England", f"State {state!r} not classified as New England"


def test_region_non_new_england_state_is_null(dal) -> None:
    from data_pipeline.etls.airport_metadata_etl import AirportMetadataETL

    etl = AirportMetadataETL(dal, None)
    rows = etl.transform(
        [{"attributes": {
            "ARPT_ID": "LAX",
            "ICAO_ID": "KLAX",
            "ARPT_NAME": "Los Angeles Intl",
            "CITY": "Los Angeles",
            "STATE_CODE": "CA",
            "STATE_NAME": "California",
            "LAT_DECIMAL": 33.94,
            "LONG_DECIMAL": -118.40,
            "EFF_DATE": "2024-01-01",
        }}]
    )
    assert rows[0]["region"] is None
