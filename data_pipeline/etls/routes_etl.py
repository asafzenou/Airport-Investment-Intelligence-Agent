"""Origin-destination routes ETL.

Source: BTS TranStats T-100 Segment (All Carriers).

KNOWN LIMITATION: BTS does not expose a reliable, stable programmatic
endpoint for T-100 Segment data.

The PREZIP directory contains T-100 files only under opaque numeric IDs
(e.g. T_T100_SEGMENT_893734.zip) and those files are missing the YEAR,
CLASS, PASSENGERS, and DISTANCE columns that the schema requires.  There
is no Socrata dataset for T-100 OD pairs on data.bts.gov.

The official source is the BTS download form at:
    https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMG

That form requires a multi-step ASP.NET ViewState postback sequence that
is not reliably automatable.  Rather than ship brittle web-scraping code,
this ETL records a clear error in sync_state on every run.

To load routes data manually:
1. Visit the URL above.
2. Select fields: YEAR, MONTH, ORIGIN, DEST, DISTANCE,
   DEPARTURES_SCHEDULED, DEPARTURES_PERFORMED, PASSENGERS, SEATS, CLASS.
3. Download the ZIP for the desired year.
4. Unzip and place the CSV at a known path.
5. Load it via the CLI (not yet implemented).

Aggregation logic (for when data becomes available):
- Only rows with CLASS = 'F' (scheduled passenger service) are retained.
- Rows are summed across carriers and aircraft types into one row per
  (ORIGIN, DEST, YEAR, MONTH).

Long-haul definition for Anchorage queries:
    distance_miles >= LONG_HAUL_MILES (see data_pipeline/config.py).
"""

import logging
from typing import Any

import httpx

from data_pipeline.config import REFRESH_HOURS
from data_pipeline.dal.aviation_dal import AviationDAL

log = logging.getLogger(__name__)

_SCHEDULED_PASSENGER_CLASS = "F"

_SOURCE_UNAVAILABLE = (
    "T-100 Segment data cannot be downloaded automatically.  "
    "The BTS PREZIP directory does not contain year-specific T-100 segment files "
    "with the required columns (YEAR, CLASS, PASSENGERS, DISTANCE).  "
    "The official source at transtats.bts.gov/DL_SelectFields.aspx requires a "
    "multi-step ASP.NET form submission that cannot be reliably automated.  "
    "Load routes data manually — see the module docstring for instructions."
)


class RoutesETL:
    DATASET_NAME = "routes"

    def __init__(self, dal: AviationDAL, client: httpx.AsyncClient) -> None:
        self._dal = dal
        self._client = client

    async def extract(self) -> list[dict[str, Any]]:
        raise RuntimeError(_SOURCE_UNAVAILABLE)

    def transform(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str, int, int], dict[str, Any]] = {}

        for item in raw:
            if (item.get("CLASS") or "").strip().upper() != _SCHEDULED_PASSENGER_CLASS:
                continue
            origin = (item.get("ORIGIN") or "").strip()
            dest = (item.get("DEST") or "").strip()
            if not origin or not dest:
                continue
            try:
                year = int(item["YEAR"])
                month = int(item["MONTH"])
                distance = float(item.get("DISTANCE") or 0)
                sched = int(float(item.get("DEPARTURES_SCHEDULED") or 0))
                perf = int(float(item.get("DEPARTURES_PERFORMED") or 0))
                pax = int(float(item.get("PASSENGERS") or 0))
                seats = int(float(item.get("SEATS") or 0))
            except (ValueError, KeyError, TypeError):
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
                    "passengers": 0,
                    "seats": 0,
                }
            entry = agg[key]
            entry["scheduled_departures"] += sched
            entry["performed_departures"] += perf
            entry["passengers"] += pax
            entry["seats"] += seats
            if entry["distance_miles"] == 0 and distance > 0:
                entry["distance_miles"] = distance

        return list(agg.values())

    def load(self, rows: list[dict[str, Any]]) -> int:
        return self._dal.upsert_routes(rows)

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
