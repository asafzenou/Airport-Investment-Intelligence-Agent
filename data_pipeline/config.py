"""Central configuration for the data pipeline.

Tune these values without touching ETL or DAL logic.
"""

from pathlib import Path

# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "storage" / "aviation.db"

# ------------------------------------------------------------------
# Refresh intervals (hours between forced re-fetches)
# ------------------------------------------------------------------

REFRESH_HOURS: dict[str, float] = {
    "airport_metadata": 7 * 24,   # source updates every 28 days
    "airport_traffic": 24,
    "routes": 24,
    "airport_operations": 24,
}

# ------------------------------------------------------------------
# Data windows
# ------------------------------------------------------------------

# Number of months of T-100 traffic history to keep locally
TRAFFIC_MONTHS_WINDOW: int = 1

# Number of months of on-time performance history to keep locally
OPERATIONS_MONTHS_WINDOW: int = 1

# ------------------------------------------------------------------
# Business thresholds
# ------------------------------------------------------------------

# Minimum route distance (miles) to be counted as long-haul.
# Used for the Anchorage long-haul percentage calculation.
# Documented assumption – not an official BTS classification.
LONG_HAUL_MILES: float = 2_500.0
