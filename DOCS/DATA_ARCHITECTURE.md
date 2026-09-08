# Aviation Data Ingestion and Storage Architecture

## 1. Purpose

This document describes the data-ingestion and local-storage layer for the Airport Investment Intelligence Agent.

The layer consists of:

- Four source-specific ETL objects, each fully responsible for its own data source.
- One DAL containing aviation-oriented database operations.
- One low-level SQLite handler.
- One pipeline that coordinates the four ETL objects.
- No vector database, ORM, message queue, ETL framework, or class hierarchy.

The chat agent and scoring logic will be built on top of this layer and are outside the scope of this document.

## 2. Code structure

```
data_pipeline/
├── config.py
├── data_pipeline.py
├── dal/
│   └── aviation_dal.py
├── data_handlers/
│   └── sqlite_handler.py
├── etls/
│   ├── _http_retry.py
│   ├── airport_metadata_etl.py
│   ├── airport_traffic_etl.py
│   ├── routes_etl.py
│   └── airport_operations_etl.py
└── logger/
    └── __init__.py

storage/
└── aviation.db

logs/
└── data_pipeline_<YYYYMMDD_HHMMSS>.log
```

Run directly:

```
python -m data_pipeline.data_pipeline
```

## 3. Component responsibilities

### `data_pipeline.py`

Creates the four ETL objects and runs them concurrently. On startup it calls `configure_logging()` to set up console and file logging. After all ETLs complete it prints a per-dataset status summary and the elapsed time.

```python
async with httpx.AsyncClient(timeout=timeout) as client:
    etls = [
        AirportMetadataETL(dal, client),
        AirportTrafficETL(dal, client),
        RoutesETL(dal, client),
        AirportOperationsETL(dal, client),
    ]
    await asyncio.gather(*[etl.run() for etl in etls])
```

It contains no source-specific request logic, no transformation logic, and no refresh-check logic. Those live entirely inside each ETL object.

### ETL objects

Each ETL exposes four methods and owns everything related to its data source:

| Method | Responsibility |
|---|---|
| `extract()` | Makes the HTTP request(s), handles pagination, returns raw records |
| `transform()` | Renames fields, converts types, aggregates rows, rejects invalid values |
| `load()` | Calls the appropriate `AviationDAL` upsert method, returns row count |
| `run()` | Checks `needs_refresh`, orchestrates extract → transform → load, updates `sync_state` |

`run()` catches all exceptions. On success it calls `update_sync_state(status="success", rows_loaded=count)`. On failure it calls `update_sync_state(status="error", error_message=...)` and does not roll back previously committed rows.

### `AviationDAL`

Knows the database schema and exposes domain-level operations. Does not make HTTP requests.

- `upsert_airports(rows)` — `INSERT ... ON CONFLICT DO UPDATE SET` on `airports`
- `upsert_traffic(rows)` — same pattern on `airport_traffic`
- `upsert_routes(rows)` — same pattern on `routes`
- `upsert_operations(rows)` — same pattern on `airport_operations`
- `update_sync_state(dataset_name, *, status, rows_loaded, error_message, latest_source_period)` — upserts a `sync_state` row; uses `COALESCE` so an error call never clears `last_successful_sync`
- `get_sync_state(dataset_name)` — returns the current `sync_state` row or `None`
- `needs_refresh(dataset_name, max_age_hours)` — returns `True` when no successful sync exists or the last sync exceeds `max_age_hours`

### `SQLiteHandler`

Manages connections and SQL execution. Contains no aviation or BTS concepts.

- Initialises the schema (all five tables) on construction via `CREATE TABLE IF NOT EXISTS`
- `_connect()` context manager: commits on success, rolls back on exception, always closes
- `execute(sql, params)` — single statement
- `executemany(sql, params) -> int` — batch upsert, returns row count
- `fetchall(sql, params)` and `fetchone(sql, params)`

### `logger/__init__.py`

Centralised logging configuration. Call `configure_logging()` once at startup (or let `run_pipeline()` do it). Safe to call multiple times — handlers are added only once per session.

- Attaches a `StreamHandler` (console) and a timestamped `FileHandler` under `logs/`.
- Keeps at most two `data_pipeline_*.log` files; older ones are deleted automatically.
- Suppresses verbose `httpx` / `httpcore` output below `WARNING`.
- Returns the `Path` of the log file created for this session.

### `etls/_http_retry.py`

Shared retry helper used by `RoutesETL` and `AirportOperationsETL` when downloading BTS PREZIP ZIP files.

- `get_with_retry(client, url, **kwargs)` — retries up to 3 times on transient transport errors (`ReadError`, `ReadTimeout`, `ConnectTimeout`).
- Backoff: 1 s after the first failure, 2 s after the second.
- 4xx/5xx HTTP responses are returned as-is; the caller decides whether to `raise_for_status()`.

### `config.py`

Central location for tunable constants:

| Name | Value | Meaning |
|---|---|---|
| `DB_PATH` | `storage/aviation.db` | Default database path |
| `REFRESH_HOURS["airport_metadata"]` | 672 h (28 days) | Max age before re-fetch |
| `REFRESH_HOURS["airport_traffic"]` | 24 h | Max age before re-fetch |
| `REFRESH_HOURS["routes"]` | 168 h (7 days) | Max age before re-fetch |
| `REFRESH_HOURS["airport_operations"]` | 168 h (7 days) | Max age before re-fetch |
| `TRAFFIC_MONTHS_WINDOW` | 36 | Months of traffic history requested |
| `OPERATIONS_MONTHS_WINDOW` | 1 | Months of on-time/routes history loaded (MVP: 1 to limit runtime) |
| `LONG_HAUL_MILES` | 2 500.0 | Min distance to count as long-haul (Anchorage queries) |
| `NEW_ENGLAND_STATES` | CT, ME, MA, NH, RI, VT | US Census Bureau New England division; used to derive the `region` field in `airports` |
| `EXPANSION_SCORE_WEIGHTS` | see below | Per-signal weights for the terminal-expansion composite score; must sum to 1.0 |

`EXPANSION_SCORE_WEIGHTS` values:

| Signal | Weight |
|---|---:|
| `passenger_growth` | 0.35 |
| `load_factor` | 0.25 |
| `departure_growth` | 0.20 |
| `delay_rate` | 0.15 |
| `cancellation_rate` | 0.05 |

```mermaid
flowchart TD
    P["data_pipeline.py\nasyncio.gather"] --> M["AirportMetadataETL"]
    P --> T["AirportTrafficETL"]
    P --> R["RoutesETL"]
    P --> O["AirportOperationsETL"]

    M -->|"extract()"| S1["ArcGIS REST"]
    T -->|"extract()"| S2["Socrata SODA"]
    R -->|"extract()"| S4["BTS PREZIP ZIPs ×1"]
    O -->|"extract()"| S4

    M -->|"load()"| DAL["AviationDAL"]
    T -->|"load()"| DAL
    R -->|"load()"| DAL
    O -->|"load()"| DAL

    DAL --> H["SQLiteHandler"]
    H --> DB[("aviation.db")]
```

## 4. Data sources

### 4.1 Airport metadata — `AirportMetadataETL`

**Provider:** USDOT/BTS National Transportation Atlas Database (FAA NASR data).

**API:** ArcGIS REST Feature Service, anonymous and read-only.

**Endpoint:**

```
https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_Aviation_Facilities/FeatureServer/0/query
```

**Fixed query parameters:**

```
where=COUNTRY_CODE='US'
outFields=ARPT_ID,ICAO_ID,ARPT_NAME,CITY,STATE_CODE,STATE_NAME,LAT_DECIMAL,LONG_DECIMAL,EFF_DATE
returnGeometry=false
f=json
resultRecordCount=1000
```

**Pagination:** The service may return `exceededTransferLimit: true`. The extractor increments `resultOffset` by `PAGE_SIZE` (1 000) until a page is smaller than `PAGE_SIZE` or `exceededTransferLimit` is absent or false.

**Field mapping:**

| Source field | Local field | Notes |
|---|---|---|
| `ARPT_ID` | `airport_code` | Required; row skipped if blank |
| `ICAO_ID` | `icao_code` | Stored as `NULL` if blank |
| `ARPT_NAME` | `airport_name` | |
| `CITY` | `city` | |
| `STATE_CODE` | `state_code` | |
| `STATE_NAME` | `state_name` | |
| `LAT_DECIMAL` | `latitude` | |
| `LONG_DECIMAL` | `longitude` | |
| `EFF_DATE` | `source_effective_date` | |
| *(derived)* | `region` | `"New England"` when state is CT, ME, MA, NH, RI, or VT; otherwise `NULL` |

### 4.2 Airport traffic and capacity — `AirportTrafficETL`

**Provider:** USDOT Bureau of Transportation Statistics.

**Dataset:** AFF — T-100 Segment Summary By Origin Airport.

**API:** Socrata SODA REST API, no key required.

**Endpoint:**

```
https://data.bts.gov/resource/r495-tyji.json
```

**Query parameters (SoQL):**

```
$select=origin_airport_code,year,reporting_month,total_departures,
        total_passengers,total_seats,total_load_factor,total_passengers_flight
$where=reporting_month >= '<36 months ago>'
$order=reporting_month ASC
$limit=50000
$offset=<incremented per page>
```

**Pagination:** Pages of 50 000; stops when a batch is smaller than the page size.

**Validation (rows failing any check are dropped):**

- `passengers` must not be negative
- `seats` must not be negative
- `load_factor` must be in `[0, 100]`

**Field mapping:**

| Source field | Local field |
|---|---|
| `origin_airport_code` | `airport_code` |
| `year` / `reporting_month[:4]` | `year` |
| `reporting_month[5:7]` | `month` |
| `total_departures` | `departures` |
| `total_passengers` | `passengers` |
| `total_seats` | `seats` |
| `total_load_factor` | `load_factor` |
| `total_passengers_flight` | `passengers_per_flight` |

### 4.3 Origin-destination routes — `RoutesETL`

**Provider:** USDOT Bureau of Transportation Statistics, TranStats.

**Dataset:** Marketing Carrier On-Time Performance (Beginning January 2018) — same dataset used by `AirportOperationsETL`.

**Access:** BTS PREZIP monthly bulk downloads. The PREZIP directory is parsed at runtime (via the shared `_available_months()` helper from `airport_operations_etl.py`) to discover which months are actually published. The `OPERATIONS_MONTHS_WINDOW` (1) most recently published months are loaded.

**URL format:**

```
https://transtats.bts.gov/PREZIP/On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{YYYY}_{M}.zip
```

**Aggregation:** Individual flight rows are grouped by `(Origin, Dest, Year, Month)`. `scheduled_departures` is incremented for every row; `performed_departures` is incremented when `Cancelled != 1`. The `Distance` field supplies `distance_miles`. The On-Time dataset does not contain passenger or seat totals — `passengers` and `seats` are stored as `NULL`.

**Field mapping:**

| Source field | Local field | Notes |
|---|---|---|
| `Origin` | `origin_airport` | |
| `Dest` | `destination_airport` | |
| `Year` | `year` | |
| `Month` | `month` | |
| `Distance` | `distance_miles` | |
| count of rows | `scheduled_departures` | |
| count where `Cancelled != 1` | `performed_departures` | |
| *(not available)* | `passengers` | Always `NULL` |
| *(not available)* | `seats` | Always `NULL` |

**Long-haul definition:** `distance_miles >= LONG_HAUL_MILES` (2 500 miles). This is a documented assumption, not an official BTS classification.

**Scope limitation:** Covers domestic scheduled flights reported by marketing carriers only. International flights, charter services, cargo-only routes, and general aviation are excluded. Long-haul percentages are therefore percentages within this domestic-flight scope.

**`latest_source_period`:** Set to the newest period loaded (format: `YYYY-MM`) and recorded in `sync_state` after each successful run.

### 4.4 Delays and cancellations — `AirportOperationsETL`

**Provider:** USDOT Bureau of Transportation Statistics, TranStats.

**Dataset:** Marketing Carrier On-Time Performance (Beginning January 2018).

**Access:** BTS PREZIP monthly bulk downloads. The PREZIP directory (`https://transtats.bts.gov/PREZIP/`) is parsed at runtime to discover which months are actually published. URL format (no parentheses, `M` has no leading zero):

    https://transtats.bts.gov/PREZIP/On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_{YYYY}_{M}.zip

**Window:** The `OPERATIONS_MONTHS_WINDOW` (1) most recently published months as discovered from the PREZIP index. BTS typically publishes each month 4–6 weeks after it closes, so the pipeline reads the index rather than assuming the previous calendar month is available. The actual latest published period is recorded in `sync_state.latest_source_period` after each successful run.

**Aggregation:** Individual flight rows are aggregated into one row per `(Origin, Year, Month)`. A flight is counted as delayed when `DepDelay > 15` minutes (BTS standard threshold). Cancelled flights are excluded from delay averages.

**Note on column names:** The CSV inside each ZIP uses title-case column names (`Origin`, `Year`, `Month`, `DepDelay`, `ArrDelay`, `Cancelled`, `Diverted`, `CarrierDelay`, `WeatherDelay`, `NASDelay`, `SecurityDelay`, `LateAircraftDelay`), not the all-caps names shown in some BTS documentation.

**Field mapping (source → aggregated local field):**

| Source field | Aggregated local field |
|---|---|
| `Origin` | `airport_code` |
| `Year` | `year` |
| `Month` | `month` |
| count of rows | `scheduled_flights` |
| count where `Cancelled = 1` | `cancelled_flights` |
| count where `Diverted = 1` | `diverted_flights` |
| count where `DepDelay > 15` (non-cancelled) | `delayed_flights` |
| mean `DepDelay` (non-cancelled) | `average_departure_delay` |
| mean `ArrDelay` (non-cancelled) | `average_arrival_delay` |
| sum `CarrierDelay` | `carrier_delay_minutes` |
| sum `WeatherDelay` | `weather_delay_minutes` |
| sum `NASDelay` | `nas_delay_minutes` |
| sum `SecurityDelay` | `security_delay_minutes` |
| sum `LateAircraftDelay` | `late_aircraft_delay_minutes` |

**Scope limitation:** Covers domestic scheduled passenger flights reported by marketing carriers only. International flights, charter services, and general aviation are excluded. This must be disclosed when presenting congestion or unmet-demand indicators.

## 5. SQLite schema

```mermaid
erDiagram
    AIRPORTS ||--o{ AIRPORT_TRAFFIC : has
    AIRPORTS ||--o{ ROUTES : "origin"
    AIRPORTS ||--o{ ROUTES : "destination"
    AIRPORTS ||--o{ AIRPORT_OPERATIONS : has

    AIRPORTS {
        text airport_code PK
        text icao_code
        text airport_name
        text city
        text state_code
        text state_name
        text region
        real latitude
        real longitude
        text source_effective_date
    }

    AIRPORT_TRAFFIC {
        text airport_code PK
        int year PK
        int month PK
        int departures
        int passengers
        int seats
        real load_factor
        real passengers_per_flight
    }

    ROUTES {
        text origin_airport PK
        text destination_airport PK
        int year PK
        int month PK
        real distance_miles
        int scheduled_departures
        int performed_departures
        int passengers
        int seats
    }

    AIRPORT_OPERATIONS {
        text airport_code PK
        int year PK
        int month PK
        int scheduled_flights
        int delayed_flights
        int cancelled_flights
        int diverted_flights
        real average_departure_delay
        real average_arrival_delay
        real carrier_delay_minutes
        real weather_delay_minutes
        real nas_delay_minutes
        real security_delay_minutes
        real late_aircraft_delay_minutes
    }

    SYNC_STATE {
        text dataset_name PK
        text last_successful_sync
        text latest_source_period
        int rows_loaded
        text status
        text error_message
    }
```

### `airports`

One row per airport. Primary key is `airport_code`.

### `airport_traffic`

One row per `(airport_code, year, month)`. Source publishes one row per airport per reporting month; no client-side aggregation is needed.

### `routes`

One row per `(origin_airport, destination_airport, year, month)`, aggregated from individual flight rows in the on-time performance file. `passengers` and `seats` are always `NULL` because the On-Time dataset does not contain totals for those fields.

### `airport_operations`

One row per `(airport_code, year, month)`, aggregated from individual flight rows in the on-time performance file.

### `sync_state`

One row per dataset (`airport_metadata`, `airport_traffic`, `routes`, `airport_operations`):

| Column | Meaning |
|---|---|
| `dataset_name` | Primary key |
| `last_successful_sync` | UTC ISO timestamp of the last successful `run()` |
| `latest_source_period` | Newest period published by the provider; populated by `RoutesETL` and `AirportOperationsETL` |
| `rows_loaded` | Row count from the last successful load |
| `status` | `"success"` or `"error"` |
| `error_message` | Exception string on failure; `NULL` on success |

`update_sync_state` uses `COALESCE` on `last_successful_sync`, `latest_source_period`, and `rows_loaded`, so an error call never clears a previously recorded successful timestamp or count.

## 6. Async extraction and sequential writes

```mermaid
flowchart LR
    G["asyncio.gather"] --> M["AirportMetadataETL.run()"]
    G --> T["AirportTrafficETL.run()"]
    G --> R["RoutesETL.run()"]
    G --> O["AirportOperationsETL.run()"]

    M -->|async extract| S1["ArcGIS"]
    T -->|async extract| S2["Socrata"]
    R -->|async extract| S4["BTS ZIPs ×1"]
    O -->|async extract| S4

    M -->|sync transform + load| DB[("aviation.db")]
    T -->|sync transform + load| DB
    R -->|sync transform + load| DB
    O -->|sync transform + load| DB
```

The four `run()` coroutines are launched concurrently with `asyncio.gather`. Inside each coroutine, `transform()` and `load()` are synchronous — asyncio cannot interleave them with other coroutines while they execute. This naturally serialises SQLite writes without explicit locking.

`RoutesETL` and `AirportOperationsETL` both hit the same BTS PREZIP index URL concurrently. Each independently parses the index and downloads the ZIP(s) it needs; there is no shared state between them.

## 7. Refresh and failure strategy

Each `run()` call follows this sequence:

1. Call `needs_refresh(dataset_name, max_age_hours)`. Skip the rest if the cached copy is fresh.
2. Call `extract()` — makes the HTTP request(s) and returns raw records.
3. Call `transform()` — renames fields, converts types, aggregates, and rejects invalid values in memory.
4. Call `load()` — upserts normalized rows into SQLite via `AviationDAL`.
5. Call `update_sync_state(status="success", rows_loaded=count)`.
6. On any exception: call `update_sync_state(status="error", error_message=...)` and log the error. Previously committed rows are preserved; `last_successful_sync` is not cleared.

Refresh intervals (from `config.py`):

| Dataset | Interval |
|---|---:|
| Airport metadata | 672 h (28 days) |
| Airport traffic | 24 h |
| Routes | 168 h (7 days) |
| Airport operations | 168 h (7 days) |

Checking on a fixed interval does not imply the provider publishes on the same schedule. The intervals are maximum staleness tolerances, not publication schedules.

## 8. Assumptions and known limitations

- **`unmet demand`** is not directly published by any of these sources. It will be represented by a proxy derived from passenger growth, load factor, departures, delays, and cancellations.
- **Routes data scope:** `RoutesETL` derives origin-destination pairs from the BTS Marketing Carrier On-Time Performance dataset (the same source as `AirportOperationsETL`). This covers domestic scheduled flights reported by marketing carriers only. International flights, charters, cargo-only routes, and general aviation are excluded. `passengers` and `seats` are always `NULL` in the `routes` table because the On-Time dataset does not provide those totals at the route level.
- **On-time performance scope:** Marketing Carrier On-Time Performance covers domestic scheduled passenger flights reported by marketing carriers. International flights, charters, and general aviation are excluded.
- **PREZIP index discovery:** Both `RoutesETL` and `AirportOperationsETL` query `https://transtats.bts.gov/PREZIP/` at runtime to discover which monthly files are actually published. This avoids assuming the previous calendar month is available, since BTS typically publishes with a 4–6 week lag. The PREZIP URL format itself is stable but not formally documented and could change without notice.
- **`latest_source_period`** is populated by `RoutesETL` and `AirportOperationsETL` after each successful run (format: `YYYY-MM`). It is not populated by `AirportMetadataETL` or `AirportTrafficETL`.
- **MVP window:** `OPERATIONS_MONTHS_WINDOW = 1` limits both `RoutesETL` and `AirportOperationsETL` to the single most recently published month. This keeps runtime and memory low during development; increase the value in `config.py` to extend historical coverage.
- **Long-haul threshold** (`LONG_HAUL_MILES = 2 500`) is a configurable assumption documented in `config.py`, not an official BTS classification.
- **Local database purpose:** The database supports analytical comparison. It does not estimate construction costs or project ROI.
- **No vector database:** All selected sources are structured and require exact filtering, aggregation, and deterministic calculations.
