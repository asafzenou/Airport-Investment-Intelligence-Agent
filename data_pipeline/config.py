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

# Months in each rolling analysis window (§3.2).
ANALYSIS_WINDOW_MONTHS: int = 12

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
# Terminal-expansion scoring (§5.4)
# ------------------------------------------------------------------

# Weights for the four-metric expansion-score composite (must sum to 1.0).
EXPANSION_WEIGHTS: dict[str, float] = {
    "passenger_growth": 0.40,
    "load_factor": 0.30,
    "delay_rate": 0.20,
    "current_passengers": 0.10,
}

# Minimum current-window passengers for expansion-score eligibility (§5.2).
EXPANSION_MIN_PASSENGERS: int = 100_000

# ------------------------------------------------------------------
# LAX/SNA congestion index (§6.3)
# Caps are project assumptions, not official BTS thresholds.
# ------------------------------------------------------------------

CONGESTION_DELAY_RATE_CAP: float = 40.0    # delay rate (%) above which pressure = 1.0
CONGESTION_CANCEL_RATE_CAP: float = 10.0   # cancellation rate (%) cap
CONGESTION_DELAY_MIN_CAP: float = 60.0     # average delay minutes cap

# Congestion index weights (must sum to 1.0).
CONGESTION_WEIGHTS: dict[str, float] = {
    "delay": 0.50,
    "cancellation": 0.20,
    "severity": 0.30,
}

# ------------------------------------------------------------------
# SFO unmet demand analysis (§8.2–8.5)
# ------------------------------------------------------------------

# Target sustainable load factor for the capacity-gap proxy (§8.2).
# Documented project assumption.
TARGET_LOAD_FACTOR: float = 0.85

# Load-pressure ramp bounds for the demand score (§8.5).
SFO_LOAD_FACTOR_FLOOR: float = 70.0    # below this → zero load pressure
SFO_LOAD_FACTOR_CEIL: float = 95.0     # above this → full load pressure

# Growth rate (%) above which growth pressure saturates at 1.0 (§8.5).
SFO_GROWTH_PRESSURE_CAP: float = 20.0

# SFO demand-score weights (must sum to 1.0; §8.5).
SFO_SCORE_WEIGHTS: dict[str, float] = {
    "load": 0.45,
    "growth": 0.25,
    "delay": 0.20,
    "cancellation": 0.10,
}

# SFO reason-flag thresholds (§8.4) – project assumptions.
FLAG_HIGH_LOAD_FACTOR: float = 85.0         # load_factor_percent threshold
FLAG_HIGH_DELAY_RATE: float = 20.0          # delay_rate_percent threshold
FLAG_ELEVATED_CANCELLATIONS: float = 2.0    # cancellation_rate_percent threshold
