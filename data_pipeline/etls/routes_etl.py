"""Origin-destination routes ETL.

Source: BTS TranStats Marketing Carrier On-Time Performance (from Jan 2018).
Access: BTS PREZIP monthly bulk download (ZIP containing a CSV) — the same
source used by AirportOperationsETL.

SCOPE LIMITATION:
    Routes represent domestic scheduled flights reported in the BTS Marketing
    Carrier On-Time Performance dataset.  International, charter, cargo-only,
    and general-aviation routes are excluded.  Therefore, long-haul percentages
    are percentages within this reported domestic-flight scope.

The PREZIP directory is queried at runtime to discover which months are
actually published.  The OPERATIONS_MONTHS_WINDOW most recently published
months are loaded.

Aggregation:
    Each CSV row represents one flight leg.  Rows are grouped by
    (Origin, Dest, Year, Month).  scheduled_departures is incremented for
    every row; performed_departures is incremented when Cancelled != 1.
    The Distance field supplies distance_miles.

    The On-Time dataset does not contain passenger or seat totals.
    passengers and seats are stored as NULL.

Long-haul definition for Anchorage queries:
    distance_miles >= LONG_HAUL_MILES (see data_pipeline/config.py).
"""

import csv
import io
import logging
import time
import zipfile
from typing import Any

import httpx

from data_pipeline.config import OPERATIONS_MONTHS_WINDOW, REFRESH_HOURS
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.etls._http_retry import get_with_retry
from data_pipeline.etls.airport_operations_etl import _available_months, _prezip_url

log = logging.getLogger(__name__)


class RoutesETL:
    DATASET_NAME = "routes"

    def __init__(self, dal: AviationDAL, client: httpx.AsyncClient) -> None:
        self._dal = dal
        self._client = client
        self._latest_period: str | None = None

    async def extract(self, year: int, month: int, i: int, total: int) -> list[dict[str, Any]]:
        url = _prezip_url(year, month)
        log.info("routes: month %d/%d — %d-%02d  %s", i, total, year, month, url)
        resp = await get_with_retry(self._client, url, follow_redirects=True)
        resp.raise_for_status()
        size_mb = len(resp.content) / 1_048_576
        if resp.content[:2] != b"PK":
            raise ValueError(f"Response from {url} is not a ZIP file")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = next(
                (n for n in zf.namelist() if n.upper().endswith(".CSV")),
                None,
            )
            if csv_name is None:
                raise ValueError(
                    f"No CSV found inside {url}; archive contained: {zf.namelist()}"
                )
            with zf.open(csv_name) as raw_file:
                reader = csv.DictReader(io.TextIOWrapper(raw_file, encoding="utf-8-sig"))
                records = [dict(r) for r in reader]
        log.info(
            "routes:   %.2f MB downloaded, %d raw CSV rows",
            size_mb, len(records),
        )
        return records

    def transform(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str, int, int], dict[str, Any]] = {}

        for item in raw:
            origin = (item.get("Origin") or "").strip()
            dest = (item.get("Dest") or "").strip()
            if not origin or not dest:
                continue
            try:
                year = int(item.get("Year") or 0)
                month = int(item.get("Month") or 0)
                distance = float(item.get("Distance") or 0)
                cancelled = float(item.get("Cancelled") or 0)
            except (ValueError, TypeError):
                continue
            if not year or not month:
                continue

            key = (origin, dest, year, month)
            if key not in agg:
                agg[key] = {
                    "origin_airport": origin,
                    "destination_airport": dest,
                    "year": year,
                    "month": month,
                    "distance_miles": distance,
                    "scheduled_departures": 0,
                    "performed_departures": 0,
                    "passengers": None,
                    "seats": None,
                }
            entry = agg[key]
            entry["scheduled_departures"] += 1
            if cancelled != 1:
                entry["performed_departures"] += 1
            if entry["distance_miles"] == 0 and distance > 0:
                entry["distance_miles"] = distance

        return list(agg.values())

    def load(self, rows: list[dict[str, Any]]) -> int:
        return self._dal.upsert_routes(rows)

    async def run(self) -> None:
        if not self._dal.needs_refresh(self.DATASET_NAME, REFRESH_HOURS[self.DATASET_NAME]):
            log.info("%s is fresh, skipping.", self.DATASET_NAME)
            return
        log.info("Starting %s ETL.", self.DATASET_NAME)
        t0 = time.monotonic()
        total_extracted = 0
        total_aggregated = 0
        total_stored = 0
        try:
            months = await _available_months(self._client)
            if not months:
                raise ValueError("No marketing-carrier on-time files found in PREZIP index")
            selected = months[:OPERATIONS_MONTHS_WINDOW]
            self._latest_period = f"{selected[0][0]}-{selected[0][1]:02d}"
            for i, (year, month) in enumerate(selected, 1):
                raw = await self.extract(year, month, i, len(selected))
                rows = self.transform(raw)
                total_stored += self.load(rows)
                total_extracted += len(raw)
                total_aggregated += len(rows)
            elapsed = time.monotonic() - t0
            self._dal.update_sync_state(
                self.DATASET_NAME,
                status="success",
                rows_loaded=total_stored,
                latest_source_period=self._latest_period,
            )
            log.info(
                "%s ETL completed: extracted=%d aggregated=%d stored=%d latest=%s (%.1fs).",
                self.DATASET_NAME, total_extracted, total_aggregated, total_stored,
                self._latest_period, elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            log.exception("ETL failed for %s after %.1fs: %s", self.DATASET_NAME, elapsed, exc)
            self._dal.update_sync_state(
                self.DATASET_NAME, status="error", error_message=str(exc)
            )
