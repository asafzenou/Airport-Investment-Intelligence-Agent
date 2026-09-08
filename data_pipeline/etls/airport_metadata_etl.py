"""Airport metadata ETL.

Source: USDOT/BTS National Transportation Atlas Database via ArcGIS REST.
Endpoint is anonymous, read-only, and returns JSON.

Pagination: the service may set exceededTransferLimit=true when more pages
exist; the extractor keeps fetching until a page is smaller than PAGE_SIZE.
"""

import logging
import time
from typing import Any

import httpx

from data_pipeline.config import NEW_ENGLAND_STATES, REFRESH_HOURS
from data_pipeline.dal.aviation_dal import AviationDAL

log = logging.getLogger(__name__)

ENDPOINT = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services"
    "/NTAD_Aviation_Facilities/FeatureServer/0/query"
)
FIELDS = (
    "ARPT_ID,ICAO_ID,ARPT_NAME,CITY,STATE_CODE,STATE_NAME,"
    "LAT_DECIMAL,LONG_DECIMAL,EFF_DATE"
)
PAGE_SIZE = 1_000


def _region(state_code: str | None) -> str | None:
    if state_code and state_code.upper() in NEW_ENGLAND_STATES:
        return "New England"
    return None


class AirportMetadataETL:
    DATASET_NAME = "airport_metadata"

    def __init__(self, dal: AviationDAL, client: httpx.AsyncClient) -> None:
        self._dal = dal
        self._client = client

    async def extract(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "where": "COUNTRY_CODE='US'",
            "outFields": FIELDS,
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": 0,
        }
        records: list[dict[str, Any]] = []
        while True:
            resp = await self._client.get(ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()
            features: list[dict[str, Any]] = data.get("features", [])
            records.extend(features)
            # Stop when this page is smaller than requested or service says no more
            if len(features) < PAGE_SIZE or not data.get("exceededTransferLimit", False):
                break
            params["resultOffset"] += PAGE_SIZE
        return records

    def transform(self, raw_features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for feature in raw_features:
            attrs = feature.get("attributes", {})
            airport_code = (attrs.get("ARPT_ID") or "").strip()
            if not airport_code:
                continue
            state_code = (attrs.get("STATE_CODE") or "").strip() or None
            rows.append(
                {
                    "airport_code": airport_code,
                    "icao_code": (attrs.get("ICAO_ID") or "").strip() or None,
                    "airport_name": (attrs.get("ARPT_NAME") or "").strip(),
                    "city": (attrs.get("CITY") or "").strip() or None,
                    "state_code": state_code,
                    "state_name": (attrs.get("STATE_NAME") or "").strip() or None,
                    "region": _region(state_code),
                    "latitude": attrs.get("LAT_DECIMAL"),
                    "longitude": attrs.get("LONG_DECIMAL"),
                    "source_effective_date": attrs.get("EFF_DATE"),
                }
            )
        return rows

    def load(self, rows: list[dict[str, Any]]) -> int:
        return self._dal.upsert_airports(rows)

    async def run(self) -> None:
        if not self._dal.needs_refresh(self.DATASET_NAME, REFRESH_HOURS[self.DATASET_NAME]):
            log.info("%s is fresh, skipping.", self.DATASET_NAME)
            return
        log.info("Starting %s ETL.", self.DATASET_NAME)
        t0 = time.monotonic()
        try:
            raw = await self.extract()
            rows = self.transform(raw)
            count = self.load(rows)
            elapsed = time.monotonic() - t0
            self._dal.update_sync_state(self.DATASET_NAME, status="success", rows_loaded=count)
            log.info(
                "%s ETL completed: extracted=%d transformed=%d stored=%d (%.1fs).",
                self.DATASET_NAME, len(raw), len(rows), count, elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            log.exception("ETL failed for %s after %.1fs: %s", self.DATASET_NAME, elapsed, exc)
            self._dal.update_sync_state(
                self.DATASET_NAME, status="error", error_message=str(exc)
            )
