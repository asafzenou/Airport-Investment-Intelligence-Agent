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
from pathlib import Path

import httpx

from data_pipeline.config import DB_PATH
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.etls.airport_metadata_etl import AirportMetadataETL
from data_pipeline.etls.airport_operations_etl import AirportOperationsETL
from data_pipeline.etls.airport_traffic_etl import AirportTrafficETL
from data_pipeline.etls.routes_etl import RoutesETL

log = logging.getLogger(__name__)


async def run_pipeline(db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run_pipeline())
