"""Low-level SQLite helper: connections, schema initialisation, and transactions.

No aviation or BTS concepts belong here.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS airports (
    airport_code          TEXT PRIMARY KEY,
    icao_code             TEXT,
    airport_name          TEXT NOT NULL,
    city                  TEXT,
    state_code            TEXT,
    state_name            TEXT,
    region                TEXT,
    latitude              REAL,
    longitude             REAL,
    source_effective_date TEXT
);

CREATE TABLE IF NOT EXISTS airport_traffic (
    airport_code        TEXT    NOT NULL,
    year                INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    departures          INTEGER,
    passengers          INTEGER,
    seats               INTEGER,
    load_factor         REAL,
    passengers_per_flight REAL,
    PRIMARY KEY (airport_code, year, month)
);

CREATE TABLE IF NOT EXISTS routes (
    origin_airport       TEXT    NOT NULL,
    destination_airport  TEXT    NOT NULL,
    year                 INTEGER NOT NULL,
    month                INTEGER NOT NULL,
    distance_miles       REAL,
    scheduled_departures INTEGER,
    performed_departures INTEGER,
    passengers           INTEGER,
    seats                INTEGER,
    PRIMARY KEY (origin_airport, destination_airport, year, month)
);

CREATE TABLE IF NOT EXISTS airport_operations (
    airport_code              TEXT    NOT NULL,
    year                      INTEGER NOT NULL,
    month                     INTEGER NOT NULL,
    scheduled_flights         INTEGER,
    delayed_flights           INTEGER,
    cancelled_flights         INTEGER,
    diverted_flights          INTEGER,
    average_departure_delay   REAL,
    average_arrival_delay     REAL,
    carrier_delay_minutes     REAL,
    weather_delay_minutes     REAL,
    nas_delay_minutes         REAL,
    security_delay_minutes    REAL,
    late_aircraft_delay_minutes REAL,
    PRIMARY KEY (airport_code, year, month)
);

CREATE TABLE IF NOT EXISTS sync_state (
    dataset_name          TEXT PRIMARY KEY,
    last_successful_sync  TEXT,
    latest_source_period  TEXT,
    rows_loaded           INTEGER,
    status                TEXT,
    error_message         TEXT
);

CREATE TABLE IF NOT EXISTS analytics_expansion_scores (
    airport_code             TEXT    NOT NULL,
    airport_name             TEXT,
    state_code               TEXT,
    current_passengers       INTEGER,
    previous_passengers      INTEGER,
    passenger_growth_percent REAL,
    load_factor_percent      REAL,
    delay_rate_percent       REAL,
    expansion_score          REAL,
    rank_position            INTEGER,
    is_rankable              INTEGER,
    calculated_at            TEXT    NOT NULL,
    period_start             TEXT    NOT NULL,
    period_end               TEXT    NOT NULL,
    data_scope               TEXT,
    limitations              TEXT
);

CREATE TABLE IF NOT EXISTS analytics_congestion (
    comparison_key            TEXT    NOT NULL,
    airport_code              TEXT    NOT NULL,
    scheduled_departures      INTEGER,
    delayed_departures        INTEGER,
    cancelled_departures      INTEGER,
    delay_rate_percent        REAL,
    cancellation_rate_percent REAL,
    average_delay_minutes     REAL,
    congestion_index          REAL,
    comparison_result         TEXT,
    calculated_at             TEXT    NOT NULL,
    period_start              TEXT    NOT NULL,
    period_end                TEXT    NOT NULL,
    data_scope                TEXT,
    limitations               TEXT
);

CREATE TABLE IF NOT EXISTS analytics_long_haul (
    airport_code              TEXT    NOT NULL,
    long_haul_threshold_miles REAL,
    long_haul_flights         INTEGER,
    total_departing_flights   INTEGER,
    long_haul_percentage      REAL,
    calculated_at             TEXT    NOT NULL,
    period_start              TEXT    NOT NULL,
    period_end                TEXT    NOT NULL,
    data_scope                TEXT,
    limitations               TEXT
);

CREATE TABLE IF NOT EXISTS analytics_unmet_demand (
    airport_code                      TEXT    NOT NULL,
    current_passengers                INTEGER,
    current_seats                     INTEGER,
    load_factor_percent               REAL,
    passenger_growth_percent          REAL,
    delay_rate_percent                REAL,
    cancellation_rate_percent         REAL,
    target_load_factor_percent        REAL,
    estimated_additional_seats_needed INTEGER,
    unmet_passenger_capacity_proxy    INTEGER,
    unmet_demand_score                REAL,
    reason_flags                      TEXT,
    calculated_at                     TEXT    NOT NULL,
    period_start                      TEXT    NOT NULL,
    period_end                        TEXT    NOT NULL,
    data_scope                        TEXT,
    limitations                       TEXT
);
"""


class SQLiteHandler:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)

    def executemany(self, sql: str, params: Iterable[Any]) -> int:
        rows = list(params)
        with self._connect() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield an open connection for multi-statement atomic operations."""
        with self._connect() as conn:
            yield conn
