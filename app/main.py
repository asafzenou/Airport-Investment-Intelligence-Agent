import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    from data_pipeline.analytics.service import AnalyticsService
    from data_pipeline.config import DB_PATH
    from data_pipeline.dal.aviation_dal import AviationDAL
    from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
    from data_pipeline.data_pipeline import run_pipeline
    from logger import configure_logging

    configure_logging()
    log = logging.getLogger(__name__)

    import os

    missing = [
        var for var in ("OPENAI_API_KEY", "OPENAI_MODEL") if not os.environ.get(var, "").strip()
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    log.info("[1/3] Updating aviation data...")
    asyncio.run(run_pipeline(DB_PATH))

    log.info("[2/3] Calculating deterministic analytics...")
    handler = SQLiteHandler(DB_PATH)
    dal = AviationDAL(handler)

    try:
        counts = AnalyticsService(dal).run_all()
    except Exception as exc:
        log.error("Analytics failed: %s", exc)
        sys.exit(1)

    for table, count in counts.items():
        log.info("  %-45s %3d rows", table, count)

    log.info("[3/3] Launching Streamlit...")
    streamlit_app = PROJECT_ROOT / "streamlit_app.py"

    try:
        subprocess.run(
            [sys.executable, "-m", "streamlit", "run", str(streamlit_app)],
            check=True,
        )
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        log.error("Streamlit exited with code %d", exc.returncode)
        sys.exit(exc.returncode)
