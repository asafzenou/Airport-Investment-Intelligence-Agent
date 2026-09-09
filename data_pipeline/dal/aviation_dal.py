"""Aviation-domain database operations.

Knows the schema and exposes domain-level upserts and sync-state helpers.
Does not make HTTP requests.
"""

import datetime
from typing import Any

from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler


class AviationDAL:
    def __init__(self, handler: SQLiteHandler) -> None:
        self._db = handler

    # ------------------------------------------------------------------
    # Upserts
    # ------------------------------------------------------------------

    def upsert_airports(self, rows: list[dict[str, Any]]) -> int:
        sql = """
        INSERT INTO airports (
            airport_code, icao_code, airport_name, city,
            state_code, state_name, region, latitude, longitude, source_effective_date
        ) VALUES (
            :airport_code, :icao_code, :airport_name, :city,
            :state_code, :state_name, :region, :latitude, :longitude, :source_effective_date
        )
        ON CONFLICT(airport_code) DO UPDATE SET
            icao_code             = excluded.icao_code,
            airport_name          = excluded.airport_name,
            city                  = excluded.city,
            state_code            = excluded.state_code,
            state_name            = excluded.state_name,
            region                = excluded.region,
            latitude              = excluded.latitude,
            longitude             = excluded.longitude,
            source_effective_date = excluded.source_effective_date
        """
        return self._db.executemany(sql, rows)

    def upsert_traffic(self, rows: list[dict[str, Any]]) -> int:
        sql = """
        INSERT INTO airport_traffic (
            airport_code, year, month, departures, passengers,
            seats, load_factor, passengers_per_flight
        ) VALUES (
            :airport_code, :year, :month, :departures, :passengers,
            :seats, :load_factor, :passengers_per_flight
        )
        ON CONFLICT(airport_code, year, month) DO UPDATE SET
            departures            = excluded.departures,
            passengers            = excluded.passengers,
            seats                 = excluded.seats,
            load_factor           = excluded.load_factor,
            passengers_per_flight = excluded.passengers_per_flight
        """
        return self._db.executemany(sql, rows)

    def upsert_routes(self, rows: list[dict[str, Any]]) -> int:
        sql = """
        INSERT INTO routes (
            origin_airport, destination_airport, year, month,
            distance_miles, scheduled_departures, performed_departures, passengers, seats
        ) VALUES (
            :origin_airport, :destination_airport, :year, :month,
            :distance_miles, :scheduled_departures, :performed_departures, :passengers, :seats
        )
        ON CONFLICT(origin_airport, destination_airport, year, month) DO UPDATE SET
            distance_miles       = excluded.distance_miles,
            scheduled_departures = excluded.scheduled_departures,
            performed_departures = excluded.performed_departures,
            passengers           = excluded.passengers,
            seats                = excluded.seats
        """
        return self._db.executemany(sql, rows)

    def upsert_operations(self, rows: list[dict[str, Any]]) -> int:
        sql = """
        INSERT INTO airport_operations (
            airport_code, year, month, scheduled_flights, delayed_flights,
            cancelled_flights, diverted_flights,
            average_departure_delay, average_arrival_delay,
            carrier_delay_minutes, weather_delay_minutes, nas_delay_minutes,
            security_delay_minutes, late_aircraft_delay_minutes
        ) VALUES (
            :airport_code, :year, :month, :scheduled_flights, :delayed_flights,
            :cancelled_flights, :diverted_flights,
            :average_departure_delay, :average_arrival_delay,
            :carrier_delay_minutes, :weather_delay_minutes, :nas_delay_minutes,
            :security_delay_minutes, :late_aircraft_delay_minutes
        )
        ON CONFLICT(airport_code, year, month) DO UPDATE SET
            scheduled_flights           = excluded.scheduled_flights,
            delayed_flights             = excluded.delayed_flights,
            cancelled_flights           = excluded.cancelled_flights,
            diverted_flights            = excluded.diverted_flights,
            average_departure_delay     = excluded.average_departure_delay,
            average_arrival_delay       = excluded.average_arrival_delay,
            carrier_delay_minutes       = excluded.carrier_delay_minutes,
            weather_delay_minutes       = excluded.weather_delay_minutes,
            nas_delay_minutes           = excluded.nas_delay_minutes,
            security_delay_minutes      = excluded.security_delay_minutes,
            late_aircraft_delay_minutes = excluded.late_aircraft_delay_minutes
        """
        return self._db.executemany(sql, rows)

    # ------------------------------------------------------------------
    # Sync state
    # ------------------------------------------------------------------

    def get_sync_state(self, dataset_name: str) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT * FROM sync_state WHERE dataset_name = ?",
            (dataset_name,),
        )
        return dict(row) if row else None

    def update_sync_state(
        self,
        dataset_name: str,
        *,
        status: str,
        rows_loaded: int | None = None,
        latest_source_period: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now_iso = datetime.datetime.utcnow().isoformat()
        last_sync = now_iso if status == "success" else None
        self._db.execute(
            """
            INSERT INTO sync_state (
                dataset_name, last_successful_sync, latest_source_period,
                rows_loaded, status, error_message
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_name) DO UPDATE SET
                last_successful_sync = COALESCE(
                    excluded.last_successful_sync, last_successful_sync
                ),
                latest_source_period = COALESCE(
                    excluded.latest_source_period, latest_source_period
                ),
                rows_loaded = COALESCE(excluded.rows_loaded, rows_loaded),
                status               = excluded.status,
                error_message        = excluded.error_message
            """,
            (dataset_name, last_sync, latest_source_period, rows_loaded, status, error_message),
        )

    # ------------------------------------------------------------------
    # Analytics reads
    # ------------------------------------------------------------------

    def get_airports_in_states(self, state_codes: frozenset[str]) -> list[dict[str, Any]]:
        """Return airports whose state_code is in the given set."""
        placeholders = ",".join("?" * len(state_codes))
        rows = self._db.fetchall(
            f"SELECT airport_code, airport_name, state_code FROM airports "
            f"WHERE state_code IN ({placeholders})",
            tuple(state_codes),
        )
        return [dict(r) for r in rows]

    def get_traffic_for_airports(self, airport_codes: list[str]) -> list[dict[str, Any]]:
        """Return all traffic rows for the given airports ordered by airport, year, month."""
        placeholders = ",".join("?" * len(airport_codes))
        rows = self._db.fetchall(
            f"SELECT airport_code, year, month, passengers, seats, departures "
            f"FROM airport_traffic WHERE airport_code IN ({placeholders}) "
            f"ORDER BY airport_code, year, month",
            tuple(airport_codes),
        )
        return [dict(r) for r in rows]

    def get_operations_for_airports(self, airport_codes: list[str]) -> list[dict[str, Any]]:
        """Return all operations rows for the given airports ordered by airport, year, month."""
        placeholders = ",".join("?" * len(airport_codes))
        rows = self._db.fetchall(
            f"SELECT airport_code, year, month, scheduled_flights, delayed_flights, "
            f"cancelled_flights, carrier_delay_minutes, weather_delay_minutes, "
            f"nas_delay_minutes, security_delay_minutes, late_aircraft_delay_minutes "
            f"FROM airport_operations WHERE airport_code IN ({placeholders}) "
            f"ORDER BY airport_code, year, month",
            tuple(airport_codes),
        )
        return [dict(r) for r in rows]

    def get_routes_from_airport(self, airport_code: str) -> list[dict[str, Any]]:
        """Return all route rows originating from the given airport."""
        rows = self._db.fetchall(
            "SELECT origin_airport, destination_airport, year, month, "
            "distance_miles, performed_departures "
            "FROM routes WHERE origin_airport = ? ORDER BY year, month",
            (airport_code,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Availability queries (used by the UI sidebar)
    # ------------------------------------------------------------------

    _ALLOWED_TABLES: frozenset[str] = frozenset(
        {"airports", "airport_traffic", "routes", "airport_operations"}
    )

    def get_row_count(self, table: str) -> int:
        """Return the total number of rows in *table*. Returns 0 for empty tables."""
        if table not in self._ALLOWED_TABLES:
            raise ValueError(f"Unknown table: {table!r}")
        row = self._db.fetchone(f"SELECT COUNT(*) AS cnt FROM {table}")
        return int(row["cnt"]) if row else 0

    def get_timeseries_stats(self, table: str) -> dict[str, Any]:
        """Return availability stats for a table that has integer year+month columns.

        Keys returned:
          count           – total row count (0 when table is empty)
          earliest_year   – integer year of the oldest period, or None
          earliest_month  – integer month of the oldest period, or None
          latest_year     – integer year of the most-recent period, or None
          latest_month    – integer month of the most-recent period, or None
          distinct_months – number of distinct (year, month) combinations
        """
        if table not in {"airport_traffic", "routes", "airport_operations"}:
            raise ValueError(f"Table {table!r} does not have year/month columns")
        row = self._db.fetchone(
            f"SELECT COUNT(*) AS cnt, "
            f"MIN(year * 100 + month) AS earliest_key, "
            f"MAX(year * 100 + month) AS latest_key, "
            f"COUNT(DISTINCT year * 100 + month) AS distinct_months "
            f"FROM {table}"
        )
        if not row or not row["cnt"]:
            return {
                "count": 0,
                "earliest_year": None, "earliest_month": None,
                "latest_year": None, "latest_month": None,
                "distinct_months": 0,
            }
        earliest_key = row["earliest_key"]
        latest_key = row["latest_key"]
        return {
            "count": int(row["cnt"]),
            "earliest_year": int(earliest_key) // 100 if earliest_key else None,
            "earliest_month": int(earliest_key) % 100 if earliest_key else None,
            "latest_year": int(latest_key) // 100 if latest_key else None,
            "latest_month": int(latest_key) % 100 if latest_key else None,
            "distinct_months": int(row["distinct_months"]),
        }

    def transaction(self):
        """Expose an atomic multi-statement SQLite transaction."""
        return self._db.transaction()

    def get_expansion_scores(self, limit: int) -> dict[str, Any]:
        """Return top-N ranked airports and all excluded airports as separate lists.

        ranked_airports       — up to `limit` rankable airports ordered by rank_position.
        rankable_airport_count — total rankable airports before the limit is applied.
        excluded_airports     — airports that had enough passengers but lacked a required
                                metric and therefore received no rank.
        """
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer between 1 and 10.")
        ranked = self._db.fetchall(
            "SELECT * FROM analytics_expansion_scores WHERE is_rankable = 1 "
            "ORDER BY rank_position ASC, airport_code ASC LIMIT ?",
            (limit,),
        )
        count_row = self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM analytics_expansion_scores WHERE is_rankable = 1",
        )
        excluded = self._db.fetchall(
            "SELECT * FROM analytics_expansion_scores WHERE is_rankable = 0 "
            "ORDER BY airport_code ASC",
        )
        return {
            "ranked_airports": [dict(r) for r in ranked],
            "rankable_airport_count": count_row["cnt"] if count_row else 0,
            "excluded_airports": [dict(r) for r in excluded],
        }

    def get_congestion_comparison(self) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            "SELECT * FROM analytics_congestion WHERE comparison_key = ? ORDER BY airport_code",
            ("LAX_SNA",),
        )
        return [dict(row) for row in rows]

    def get_long_haul_analysis(self) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT * FROM analytics_long_haul WHERE airport_code = ?", ("ANC",),
        )
        return dict(row) if row else None

    def get_unmet_demand_analysis(self) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT * FROM analytics_unmet_demand WHERE airport_code = ?", ("SFO",),
        )
        return dict(row) if row else None

    def needs_refresh(self, dataset_name: str, max_age_hours: float) -> bool:
        """Return True when no successful sync exists or the last sync exceeds max_age_hours."""
        state = self.get_sync_state(dataset_name)
        if not state or not state.get("last_successful_sync"):
            return True
        if state.get("status") != "success":
            return True
        last = datetime.datetime.fromisoformat(state["last_successful_sync"])
        age_seconds = (datetime.datetime.utcnow() - last).total_seconds()
        return age_seconds > max_age_hours * 3600
