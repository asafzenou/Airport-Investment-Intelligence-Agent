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
