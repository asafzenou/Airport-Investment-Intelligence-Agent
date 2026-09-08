"""Run the analytics layer against the existing local database.

Usage:
    uv run python -m data_pipeline.run_analytics

This command reads the source tables that were populated by the ETL pipeline
and writes the four analytics result tables.  It does not download any data.
"""

import logging
import sys

from data_pipeline.analytics.service import AnalyticsService
from data_pipeline.config import DB_PATH
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from logger import configure_logging


def main() -> None:
    configure_logging()
    log = logging.getLogger(__name__)
    log.info("Analytics run starting against %s", DB_PATH)

    handler = SQLiteHandler(DB_PATH)
    dal = AviationDAL(handler)
    service = AnalyticsService(dal)

    try:
        counts = service.run_all()
    except Exception:
        log.exception("Analytics run failed.")
        sys.exit(1)

    for table, n in counts.items():
        log.info("  %-45s %3d rows", table, n)
    log.info("Analytics run complete.")


if __name__ == "__main__":
    main()
