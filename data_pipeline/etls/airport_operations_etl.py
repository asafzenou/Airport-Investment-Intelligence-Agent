"""Delays and cancellations ETL.

Source: BTS TranStats Marketing Carrier On-Time Performance (from Jan 2018).
Access: BTS PREZIP monthly bulk download (ZIP containing a CSV).

The PREZIP directory is queried at runtime to discover which months are
actually published.  Assuming the previous calendar month is already available
is incorrect; BTS typically publishes each month 4-6 weeks after it closes.

Correct URL format (no parentheses around "Beginning_January_2018"):
    https://transtats.bts.gov/PREZIP/
    On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{YYYY}_{M}.zip
where M has no leading zero (1–12).

The CSV inside each ZIP uses title-case column names (e.g. 'Origin',
'DepDelay', 'Cancelled') — not the all-caps names shown in BTS documentation.

KNOWN LIMITATION: Covers domestic scheduled passenger flights reported by
marketing carriers only.  International flights, charter services, and general
aviation are excluded.  Always surface this scope restriction when presenting
congestion or unmet-demand indicators derived from this data.

Window: the OPERATIONS_MONTHS_WINDOW most recently published months, determined
by parsing the PREZIP index.  The actual latest source period is recorded in
sync_state after each successful load.
"""

import csv
import io
import logging
import re
import zipfile
from typing import Any

import httpx

from data_pipeline.config import OPERATIONS_MONTHS_WINDOW, REFRESH_HOURS
from data_pipeline.dal.aviation_dal import AviationDAL

log = logging.getLogger(__name__)

_PREZIP_INDEX = "https://transtats.bts.gov/PREZIP/"
_MKTG_FILE_RE = re.compile(
    r"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018"
    r"_(\d{4})_(\d{1,2})\.zip"
)
_DELAY_THRESHOLD_MIN = 15.0


def _prezip_url(year: int, month: int) -> str:
    return (
        f"{_PREZIP_INDEX}"
        f"On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018"
        f"_{year}_{month}.zip"
    )


def _fv(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", " ") else 0.0
    except (ValueError, TypeError):
        return 0.0


async def _available_months(client: httpx.AsyncClient) -> list[tuple[int, int]]:
    """Parse the PREZIP index; return available marketing-carrier months newest-first."""
    resp = await client.get(_PREZIP_INDEX)
    resp.raise_for_status()
    months = sorted(
        {(int(y), int(m)) for y, m in _MKTG_FILE_RE.findall(resp.text)},
        reverse=True,
    )
    return months


class AirportOperationsETL:
    DATASET_NAME = "airport_operations"

    def __init__(self, dal: AviationDAL, client: httpx.AsyncClient) -> None:
        self._dal = dal
        self._client = client
        self._latest_period: str | None = None

    async def extract(self) -> list[dict[str, Any]]:
        months = await _available_months(self._client)
        if not months:
            raise ValueError("No marketing-carrier on-time files found in PREZIP index")
        selected = months[:OPERATIONS_MONTHS_WINDOW]
        self._latest_period = f"{selected[0][0]}-{selected[0][1]:02d}"
        records: list[dict[str, Any]] = []
        for year, month in selected:
            url = _prezip_url(year, month)
            resp = await self._client.get(url, follow_redirects=True)
            resp.raise_for_status()
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
                    records.extend(dict(r) for r in reader)
        return records

    def transform(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from collections import defaultdict

        agg: dict[tuple[str, int, int], dict[str, Any]] = defaultdict(
            lambda: {
                "scheduled_flights": 0,
                "delayed_flights": 0,
                "cancelled_flights": 0,
                "diverted_flights": 0,
                "dep_delay_sum": 0.0,
                "dep_delay_count": 0,
                "arr_delay_sum": 0.0,
                "arr_delay_count": 0,
                "carrier_delay_minutes": 0.0,
                "weather_delay_minutes": 0.0,
                "nas_delay_minutes": 0.0,
                "security_delay_minutes": 0.0,
                "late_aircraft_delay_minutes": 0.0,
            }
        )

        for item in raw:
            origin = (item.get("Origin") or "").strip()
            if not origin:
                continue
            try:
                year = int(item.get("Year") or 0)
                month = int(item.get("Month") or 0)
            except (ValueError, TypeError):
                continue
            if not year or not month:
                continue

            key = (origin, year, month)
            b = agg[key]

            cancelled = _fv(item.get("Cancelled"))
            diverted = _fv(item.get("Diverted"))
            dep_delay = _fv(item.get("DepDelay"))
            arr_delay = _fv(item.get("ArrDelay"))

            b["scheduled_flights"] += 1
            b["cancelled_flights"] += int(cancelled)
            b["diverted_flights"] += int(diverted)

            if not cancelled:
                if dep_delay > _DELAY_THRESHOLD_MIN:
                    b["delayed_flights"] += 1
                b["dep_delay_sum"] += dep_delay
                b["dep_delay_count"] += 1
                b["arr_delay_sum"] += arr_delay
                b["arr_delay_count"] += 1

            b["carrier_delay_minutes"] += _fv(item.get("CarrierDelay"))
            b["weather_delay_minutes"] += _fv(item.get("WeatherDelay"))
            b["nas_delay_minutes"] += _fv(item.get("NASDelay"))
            b["security_delay_minutes"] += _fv(item.get("SecurityDelay"))
            b["late_aircraft_delay_minutes"] += _fv(item.get("LateAircraftDelay"))

        rows: list[dict[str, Any]] = []
        for (airport_code, year, month), b in agg.items():
            dep_count = b["dep_delay_count"]
            arr_count = b["arr_delay_count"]
            rows.append(
                {
                    "airport_code": airport_code,
                    "year": year,
                    "month": month,
                    "scheduled_flights": b["scheduled_flights"],
                    "delayed_flights": b["delayed_flights"],
                    "cancelled_flights": b["cancelled_flights"],
                    "diverted_flights": b["diverted_flights"],
                    "average_departure_delay": (
                        b["dep_delay_sum"] / dep_count if dep_count else None
                    ),
                    "average_arrival_delay": (
                        b["arr_delay_sum"] / arr_count if arr_count else None
                    ),
                    "carrier_delay_minutes": b["carrier_delay_minutes"],
                    "weather_delay_minutes": b["weather_delay_minutes"],
                    "nas_delay_minutes": b["nas_delay_minutes"],
                    "security_delay_minutes": b["security_delay_minutes"],
                    "late_aircraft_delay_minutes": b["late_aircraft_delay_minutes"],
                }
            )
        return rows

    def load(self, rows: list[dict[str, Any]]) -> int:
        return self._dal.upsert_operations(rows)

    async def run(self) -> None:
        if not self._dal.needs_refresh(self.DATASET_NAME, REFRESH_HOURS[self.DATASET_NAME]):
            log.info("%s is fresh, skipping.", self.DATASET_NAME)
            return
        try:
            raw = await self.extract()
            rows = self.transform(raw)
            count = self.load(rows)
            self._dal.update_sync_state(
                self.DATASET_NAME,
                status="success",
                rows_loaded=count,
                latest_source_period=self._latest_period,
            )
            log.info("Loaded %d rows for %s (latest: %s)", count, self.DATASET_NAME,
                     self._latest_period)
        except Exception as exc:  # noqa: BLE001
            log.error("ETL failed for %s: %s", self.DATASET_NAME, exc)
            self._dal.update_sync_state(
                self.DATASET_NAME, status="error", error_message=str(exc)
            )
