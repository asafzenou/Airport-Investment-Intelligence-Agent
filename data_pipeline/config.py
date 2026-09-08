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
    "airport_metadata": 28 * 24,
    "airport_traffic": 24,
    "routes": 7 * 24,
    "airport_operations": 7 * 24,
}

# ------------------------------------------------------------------
# Data windows
# ------------------------------------------------------------------

# Number of recent traffic-history months fetched from the T-100 API.
TRAFFIC_MONTHS_WINDOW: int = 36

# Number of recent BTS On-Time months fetched for operations and routes.
# Kept at 1 for the MVP to limit runtime and memory consumption.
OPERATIONS_MONTHS_WINDOW: int = 1

# ------------------------------------------------------------------
# Business thresholds
# ------------------------------------------------------------------

# Minimum route distance (miles) counted as long-haul.
# Used for the Anchorage long-haul percentage calculation.
# Documented assumption – not an official BTS classification.
LONG_HAUL_MILES: float = 2_500.0

# US Census Bureau New England division states.
NEW_ENGLAND_STATES: frozenset[str] = frozenset(
    {"CT", "ME", "MA", "NH", "RI", "VT"}
)

# ------------------------------------------------------------------
# Terminal-expansion scoring
# ------------------------------------------------------------------

# Weights for the deterministic expansion-score composite.
# Must sum to 1.0.
EXPANSION_SCORE_WEIGHTS: dict[str, float] = {
    "passenger_growth": 0.35,
    "load_factor": 0.25,
    "departure_growth": 0.20,
    "delay_rate": 0.15,
    "cancellation_rate": 0.05,
}
