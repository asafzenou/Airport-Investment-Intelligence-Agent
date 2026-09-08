"""Data pipeline coordinator.

Creates the four ETL objects and runs them concurrently.
Network extraction happens in parallel via asyncio.gather; SQLite writes
are serialised naturally because transform() and load() are synchronous
and asyncio never yields during them.

Run directly:
    python -m data_pipeline.data_pipeline
"""

import asyncio
import logging
import time
from pathlib import Path

import httpx

from data_pipeline.config import DB_PATH
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_metadata_etl import AirportMetadataETL
from data_pipeline.etls.airport_operations_etl import AirportOperationsETL
from data_pipeline.etls.airport_traffic_etl import AirportTrafficETL
from data_pipeline.etls.routes_etl import RoutesETL
from logger import configure_logging

log = logging.getLogger(__name__)


async def run_pipeline(db_path: Path | None = None) -> None:
    log_path = configure_logging()

    path = db_path or DB_PATH
    t0 = time.monotonic()
    log.info("Pipeline starting. DB: %s", path)

    path.parent.mkdir(parents=True, exist_ok=True)

    handler = SQLiteHandler(path)
    dal = AviationDAL(handler)

    timeout = httpx.Timeout(120.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        etls = [
            AirportMetadataETL(dal, client),
            AirportTrafficETL(dal, client),
            RoutesETL(dal, client),
            AirportOperationsETL(dal, client),
        ]
        await asyncio.gather(*[etl.run() for etl in etls])

    elapsed = time.monotonic() - t0

    sync_rows = handler.fetchall(
        "SELECT dataset_name, status, rows_loaded FROM sync_state ORDER BY dataset_name"
    )
    for r in sync_rows:
        rows_loaded = r["rows_loaded"]
        log.info(
            "  %-25s status=%-8s rows=%s",
            r["dataset_name"],
            r["status"],
            rows_loaded if rows_loaded is not None else "—",
        )

    failed = [r["dataset_name"] for r in sync_rows if r["status"] == "error"]
    if failed:
        log.warning(
            "Pipeline finished in %.1fs — %d failed: %s",
            elapsed,
            len(failed),
            ", ".join(failed),
        )
    else:
        log.info("Pipeline finished successfully in %.1fs.", elapsed)

    log.info("Log file: %s", log_path)


if __name__ == "__main__":
    asyncio.run(run_pipeline())
