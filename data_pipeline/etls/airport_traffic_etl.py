"""Airport traffic and capacity ETL.

Source: USDOT BTS AFF-T100 Segment Summary via Socrata SODA REST API.
No API key required for this workload; Socrata public endpoint.

Window: latest TRAFFIC_MONTHS_WINDOW available months, queried with SoQL filters.
Pagination: Socrata uses $limit/$offset; extractor pages until a response
is smaller than PAGE_SIZE.
"""

import logging
from datetime import date
from typing import Any

import httpx

from data_pipeline.config import REFRESH_HOURS, TRAFFIC_MONTHS_WINDOW
from data_pipeline.dal.aviation_dal import AviationDAL

log = logging.getLogger(__name__)

ENDPOINT = "https://data.bts.gov/resource/r495-tyji.json"
PAGE_SIZE = 50_000


def _window_start() -> str:
    today = date.today()
    # Subtract ~TRAFFIC_MONTHS_WINDOW months without depending on dateutil
    year = today.year - (TRAFFIC_MONTHS_WINDOW // 12)
    month = today.month - (TRAFFIC_MONTHS_WINDOW % 12)
    if month <= 0:
        year -= 1
        month += 12
    return date(year, month, 1).isoformat()


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _valid(row: dict[str, Any]) -> bool:
    pax = row.get("passengers")
    seats = row.get("seats")
    lf = row.get("load_factor")
    if pax is not None and pax < 0:
        return False
    if seats is not None and seats < 0:
        return False
    if lf is not None and not (0 <= lf <= 100):
        return False
    return True


class AirportTrafficETL:
    DATASET_NAME = "airport_traffic"

    def __init__(self, dal: AviationDAL, client: httpx.AsyncClient) -> None:
        self._dal = dal
        self._client = client

    async def extract(self) -> list[dict[str, Any]]:
        select_cols = (
            "origin_airport_code,year,reporting_month,"
            "total_departures,total_passengers,total_seats,"
            "total_load_factor,total_passengers_flight"
        )
        params: dict[str, Any] = {
            "$select": select_cols,
            "$where": f"reporting_month >= '{_window_start()}'",
            "$order": "reporting_month ASC",
            "$limit": PAGE_SIZE,
            "$offset": 0,
        }
        records: list[dict[str, Any]] = []
        while True:
            resp = await self._client.get(ENDPOINT, params=params)
            resp.raise_for_status()
            batch: list[dict[str, Any]] = resp.json()
            records.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            params["$offset"] += PAGE_SIZE
        return records

    def transform(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in raw:
            airport_code = (item.get("origin_airport_code") or "").strip()
            if not airport_code:
                continue

            # reporting_month arrives as an ISO-8601 timestamp; extract year/month from it
            month_raw = item.get("reporting_month") or ""
            try:
                year = int(item.get("year") or month_raw[:4])
                month = int(month_raw[5:7])
            except (ValueError, TypeError, IndexError):
                continue

            row: dict[str, Any] = {
                "airport_code": airport_code,
                "year": year,
                "month": month,
                "departures": _to_int(item.get("total_departures")),
                "passengers": _to_int(item.get("total_passengers")),
                "seats": _to_int(item.get("total_seats")),
                "load_factor": _to_float(item.get("total_load_factor")),
                "passengers_per_flight": _to_float(item.get("total_passengers_flight")),
            }
            if _valid(row):
                rows.append(row)
        return rows

    def load(self, rows: list[dict[str, Any]]) -> int:
        return self._dal.upsert_traffic(rows)

    async def run(self) -> None:
        if not self._dal.needs_refresh(self.DATASET_NAME, REFRESH_HOURS[self.DATASET_NAME]):
            log.info("%s is fresh, skipping.", self.DATASET_NAME)
            return
        try:
            raw = await self.extract()
            rows = self.transform(raw)
            count = self.load(rows)
            self._dal.update_sync_state(self.DATASET_NAME, status="success", rows_loaded=count)
            log.info("Loaded %d rows for %s", count, self.DATASET_NAME)
        except Exception as exc:  # noqa: BLE001
            log.error("ETL failed for %s: %s", self.DATASET_NAME, exc)
            self._dal.update_sync_state(
                self.DATASET_NAME, status="error", error_message=str(exc)
            )
