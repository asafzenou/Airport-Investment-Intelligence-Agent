"""Deterministic analytics layer.

Reads source tables through AviationDAL, calculates all four analytics use
cases in memory, then writes the results into the four analytics tables inside
a single SQLite transaction.  On any failure the transaction is rolled back so
the previous complete snapshot remains available.
"""

import datetime
import logging
import math
from typing import Any

from data_pipeline.config import (
    ANALYSIS_WINDOW_MONTHS,
    CONGESTION_CANCEL_RATE_CAP,
    CONGESTION_DELAY_MIN_CAP,
    CONGESTION_DELAY_RATE_CAP,
    CONGESTION_WEIGHTS,
    EXPANSION_MIN_PASSENGERS,
    EXPANSION_WEIGHTS,
    FLAG_ELEVATED_CANCELLATIONS,
    FLAG_HIGH_DELAY_RATE,
    FLAG_HIGH_LOAD_FACTOR,
    LONG_HAUL_MILES,
    NEW_ENGLAND_STATES,
    SFO_GROWTH_PRESSURE_CAP,
    SFO_LOAD_FACTOR_CEIL,
    SFO_LOAD_FACTOR_FLOOR,
    SFO_SCORE_WEIGHTS,
    TARGET_LOAD_FACTOR,
)
from data_pipeline.dal.aviation_dal import AviationDAL

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# INSERT statements
# ---------------------------------------------------------------------------

_INSERT_EXPANSION = """
INSERT INTO analytics_expansion_scores (
    airport_code, airport_name, state_code,
    current_passengers, previous_passengers, passenger_growth_percent,
    load_factor_percent, delay_rate_percent,
    expansion_score, rank_position, is_rankable,
    calculated_at, period_start, period_end, data_scope, limitations
) VALUES (
    :airport_code, :airport_name, :state_code,
    :current_passengers, :previous_passengers, :passenger_growth_percent,
    :load_factor_percent, :delay_rate_percent,
    :expansion_score, :rank_position, :is_rankable,
    :calculated_at, :period_start, :period_end, :data_scope, :limitations
)
"""

_INSERT_CONGESTION = """
INSERT INTO analytics_congestion (
    comparison_key, airport_code,
    scheduled_departures, delayed_departures, cancelled_departures,
    delay_rate_percent, cancellation_rate_percent, average_delay_minutes,
    congestion_index, comparison_result,
    calculated_at, period_start, period_end, data_scope, limitations
) VALUES (
    :comparison_key, :airport_code,
    :scheduled_departures, :delayed_departures, :cancelled_departures,
    :delay_rate_percent, :cancellation_rate_percent, :average_delay_minutes,
    :congestion_index, :comparison_result,
    :calculated_at, :period_start, :period_end, :data_scope, :limitations
)
"""

_INSERT_LONG_HAUL = """
INSERT INTO analytics_long_haul (
    airport_code, long_haul_threshold_miles,
    long_haul_flights, total_departing_flights, long_haul_percentage,
    calculated_at, period_start, period_end, data_scope, limitations
) VALUES (
    :airport_code, :long_haul_threshold_miles,
    :long_haul_flights, :total_departing_flights, :long_haul_percentage,
    :calculated_at, :period_start, :period_end, :data_scope, :limitations
)
"""

_INSERT_UNMET = """
INSERT INTO analytics_unmet_demand (
    airport_code, current_passengers, current_seats,
    load_factor_percent, passenger_growth_percent,
    delay_rate_percent, cancellation_rate_percent,
    target_load_factor_percent,
    estimated_additional_seats_needed, unmet_passenger_capacity_proxy,
    unmet_demand_score, reason_flags,
    calculated_at, period_start, period_end, data_scope, limitations
) VALUES (
    :airport_code, :current_passengers, :current_seats,
    :load_factor_percent, :passenger_growth_percent,
    :delay_rate_percent, :cancellation_rate_percent,
    :target_load_factor_percent,
    :estimated_additional_seats_needed, :unmet_passenger_capacity_proxy,
    :unmet_demand_score, :reason_flags,
    :calculated_at, :period_start, :period_end, :data_scope, :limitations
)
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _ym_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _offset_ym(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift year/month by delta months (may be negative)."""
    total = (year - 1) * 12 + (month - 1) + delta
    return total // 12 + 1, total % 12 + 1


def _window_keys(end_year: int, end_month: int, n: int) -> set[str]:
    """Return the set of ym-keys for n months ending at end_year/end_month."""
    keys: set[str] = set()
    y, m = end_year, end_month
    for _ in range(n):
        keys.add(_ym_key(y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return keys


def _minmax_normalize(values: list[float | None]) -> list[float | None]:
    """Min-max normalize; ties yield 0.5; None stays None."""
    valid = [v for v in values if v is not None]
    if not valid:
        return list(values)
    lo, hi = min(valid), max(valid)
    if math.isclose(lo, hi):
        return [0.5 if v is not None else None for v in values]
    return [(v - lo) / (hi - lo) if v is not None else None for v in values]


def _ops_period_label(keys_present: set[str]) -> str:
    """Human-readable period description for an ops coverage set."""
    if not keys_present:
        return "no operational data in the traffic window"
    if len(keys_present) == 1:
        return f"operational data covers {next(iter(keys_present))} only (1 month)"
    return (
        f"operational data covers {min(keys_present)} to {max(keys_present)} "
        f"({len(keys_present)} month(s) within the 12-month traffic window)"
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AnalyticsService:
    def __init__(self, dal: AviationDAL) -> None:
        self._dal = dal

    # ------------------------------------------------------------------
    # Use case 1 – New England terminal expansion ranking
    # ------------------------------------------------------------------

    def calculate_expansion_scores(self) -> list[dict[str, Any]]:
        """Rank New England airports by evidence of growing demand."""
        now = _now_iso()

        ne_airports = self._dal.get_airports_in_states(NEW_ENGLAND_STATES)
        if not ne_airports:
            log.warning("No New England airports found in the database.")
            return []

        codes = [a["airport_code"] for a in ne_airports]
        airport_info = {a["airport_code"]: a for a in ne_airports}

        traffic_rows = self._dal.get_traffic_for_airports(codes)
        ops_rows = self._dal.get_operations_for_airports(codes)

        if not traffic_rows:
            log.warning("No traffic data for New England airports.")
            return []

        # Traffic window is anchored at the latest available traffic month only.
        # Operations are sourced from whatever months fall within that window.
        t_latest = max(_ym_key(r["year"], r["month"]) for r in traffic_rows)
        end_y, end_m = int(t_latest[:4]), int(t_latest[5:])

        curr_keys = _window_keys(end_y, end_m, ANALYSIS_WINDOW_MONTHS)
        prev_end_y, prev_end_m = _offset_ym(end_y, end_m, -ANALYSIS_WINDOW_MONTHS)
        prev_keys = _window_keys(prev_end_y, prev_end_m, ANALYSIS_WINDOW_MONTHS)

        period_end = _ym_key(end_y, end_m)
        ps_y, ps_m = _offset_ym(end_y, end_m, -(ANALYSIS_WINDOW_MONTHS - 1))
        period_start = _ym_key(ps_y, ps_m)

        # Index by airport → ym_key → row
        t_idx: dict[str, dict[str, dict]] = {}
        for r in traffic_rows:
            t_idx.setdefault(r["airport_code"], {})[_ym_key(r["year"], r["month"])] = r

        o_idx: dict[str, dict[str, dict]] = {}
        for r in ops_rows:
            o_idx.setdefault(r["airport_code"], {})[_ym_key(r["year"], r["month"])] = r

        metrics: list[dict] = []
        for code in codes:
            t_map = t_idx.get(code, {})
            o_map = o_idx.get(code, {})

            # Validate current window completeness per airport.
            curr_months_present = sum(1 for k in curr_keys if k in t_map)
            curr_window_complete = curr_months_present == ANALYSIS_WINDOW_MONTHS

            curr_pax = sum(t_map[k]["passengers"] or 0 for k in curr_keys if k in t_map)
            curr_seats = sum(t_map[k]["seats"] or 0 for k in curr_keys if k in t_map)

            if curr_pax < EXPANSION_MIN_PASSENGERS:
                continue

            # Validate previous window completeness per airport.
            prev_months_present = sum(1 for k in prev_keys if k in t_map)
            prev_pax = sum(t_map[k]["passengers"] or 0 for k in prev_keys if k in t_map)
            prev_window_complete = prev_months_present == ANALYSIS_WINDOW_MONTHS and prev_pax > 0

            growth_pct: float | None = None
            if curr_window_complete and prev_window_complete:
                growth_pct = ((curr_pax - prev_pax) / prev_pax) * 100

            lf_pct: float | None = (curr_pax / curr_seats * 100) if curr_seats > 0 else None

            # Ops period: months within the current traffic window that have data.
            ops_keys_in_window = {k for k in curr_keys if k in o_map}
            sched = sum(o_map[k]["scheduled_flights"] or 0 for k in ops_keys_in_window)
            delayed = sum(o_map[k]["delayed_flights"] or 0 for k in ops_keys_in_window)
            delay_rate: float | None = (delayed / sched * 100) if sched > 0 else None

            caveats: list[str] = []
            if not curr_window_complete:
                caveats.append(
                    f"incomplete current traffic window "
                    f"({curr_months_present}/12 months available); "
                    "passenger total is partial"
                )
            if not prev_window_complete:
                caveats.append("passenger growth unavailable (insufficient prior-year history)")
            if lf_pct is None:
                caveats.append("load factor unavailable (zero seats)")
            if delay_rate is None:
                caveats.append("delay rate unavailable (no operations data in traffic window)")
            else:
                caveats.append(_ops_period_label(ops_keys_in_window))

            metrics.append({
                "airport_code": code,
                "airport_name": airport_info[code]["airport_name"],
                "state_code": airport_info[code]["state_code"],
                "current_passengers": curr_pax,
                "previous_passengers": prev_pax if prev_window_complete else None,
                "_growth": growth_pct,
                "_lf": lf_pct,
                "_delay": delay_rate,
                "_pax": float(curr_pax),
                "_caveats": caveats,
            })

        if not metrics:
            return []

        pos_growths = [max(m["_growth"], 0) if m["_growth"] is not None else None for m in metrics]
        norm_g = _minmax_normalize(pos_growths)
        norm_l = _minmax_normalize([m["_lf"] for m in metrics])
        norm_d = _minmax_normalize([m["_delay"] for m in metrics])
        norm_p = _minmax_normalize([m["_pax"] for m in metrics])

        results: list[dict[str, Any]] = []
        for i, m in enumerate(metrics):
            ng, nl, nd, np_ = norm_g[i], norm_l[i], norm_d[i], norm_p[i]
            is_rankable = all(v is not None for v in (ng, nl, nd, np_))
            score: float | None = None
            if is_rankable:
                score = round(
                    100 * (
                        EXPANSION_WEIGHTS["passenger_growth"] * ng  # type: ignore[operator]
                        + EXPANSION_WEIGHTS["load_factor"] * nl
                        + EXPANSION_WEIGHTS["delay_rate"] * nd
                        + EXPANSION_WEIGHTS["current_passengers"] * np_
                    ),
                    1,
                )

            results.append({
                "airport_code": m["airport_code"],
                "airport_name": m["airport_name"],
                "state_code": m["state_code"],
                "current_passengers": m["current_passengers"],
                "previous_passengers": m["previous_passengers"],
                "passenger_growth_percent": (
                    round(m["_growth"], 2) if m["_growth"] is not None else None
                ),
                "load_factor_percent": round(m["_lf"], 2) if m["_lf"] is not None else None,
                "delay_rate_percent": (
                    round(m["_delay"], 2) if m["_delay"] is not None else None
                ),
                "expansion_score": score,
                "rank_position": None,
                "is_rankable": 1 if is_rankable else 0,
                "calculated_at": now,
                "period_start": period_start,
                "period_end": period_end,
                "data_scope": (
                    f"New England airports with >= {EXPANSION_MIN_PASSENGERS:,} passengers "
                    "in the current 12-month window"
                ),
                "limitations": "; ".join(m["_caveats"]) if m["_caveats"] else "None",
            })

        # Assign ranks only to rankable airports, ordered by score DESC then code.
        rankable = sorted(
            [r for r in results if r["is_rankable"]],
            key=lambda r: (-(r["expansion_score"] or 0), r["airport_code"]),
        )
        for rank, r in enumerate(rankable, start=1):
            r["rank_position"] = rank

        # Sort final output: rankable first (by rank), then non-rankable by code.
        results.sort(
            key=lambda r: (
                0 if r["is_rankable"] else 1,
                r["rank_position"] if r["rank_position"] is not None else 0,
                r["airport_code"],
            )
        )

        return results

    # ------------------------------------------------------------------
    # Use case 2 – LAX vs SNA congestion comparison
    # ------------------------------------------------------------------

    def calculate_congestion(self) -> list[dict[str, Any]]:
        """Compare operational congestion at LAX and SNA."""
        now = _now_iso()

        ops_rows = self._dal.get_operations_for_airports(["LAX", "SNA"])

        lax_rows = [r for r in ops_rows if r["airport_code"] == "LAX"]
        sna_rows = [r for r in ops_rows if r["airport_code"] == "SNA"]

        if not lax_rows or not sna_rows:
            missing = "LAX" if not lax_rows else "SNA"
            log.warning("No operations data found for %s.", missing)
            return []

        lax_keys = {_ym_key(r["year"], r["month"]) for r in lax_rows}
        sna_keys = {_ym_key(r["year"], r["month"]) for r in sna_rows}
        common = lax_keys & sna_keys

        if not common:
            log.warning("LAX and SNA have no overlapping operations months.")
            return []

        period_start, period_end = min(common), max(common)

        def _agg(rows: list[dict], keys: set[str]) -> dict:
            filtered = [r for r in rows if _ym_key(r["year"], r["month"]) in keys]
            sched = sum(r["scheduled_flights"] or 0 for r in filtered)
            delayed = sum(r["delayed_flights"] or 0 for r in filtered)
            cancelled = sum(r["cancelled_flights"] or 0 for r in filtered)
            total_delay = sum(
                (r.get("carrier_delay_minutes") or 0)
                + (r.get("weather_delay_minutes") or 0)
                + (r.get("nas_delay_minutes") or 0)
                + (r.get("security_delay_minutes") or 0)
                + (r.get("late_aircraft_delay_minutes") or 0)
                for r in filtered
            )
            return {
                "sched": sched,
                "delayed": delayed,
                "cancelled": cancelled,
                "total_delay": total_delay,
            }

        def _metrics(agg: dict) -> dict:
            s, d, c, td = agg["sched"], agg["delayed"], agg["cancelled"], agg["total_delay"]
            dr = round(d / s * 100, 2) if s > 0 else None
            cr = round(c / s * 100, 2) if s > 0 else None
            ad = round(td / d, 2) if d > 0 else None

            dp = min(dr / CONGESTION_DELAY_RATE_CAP, 1) * 100 if dr is not None else None
            cp = min(cr / CONGESTION_CANCEL_RATE_CAP, 1) * 100 if cr is not None else None
            sp = min(ad / CONGESTION_DELAY_MIN_CAP, 1) * 100 if ad is not None else None

            idx: float | None = None
            if all(v is not None for v in (dp, cp, sp)):
                idx = round(
                    CONGESTION_WEIGHTS["delay"] * dp  # type: ignore[operator]
                    + CONGESTION_WEIGHTS["cancellation"] * cp
                    + CONGESTION_WEIGHTS["severity"] * sp,
                    2,
                )

            return {
                "scheduled_departures": s,
                "delayed_departures": d,
                "cancelled_departures": c,
                "delay_rate_percent": dr,
                "cancellation_rate_percent": cr,
                "average_delay_minutes": ad,
                "congestion_index": idx,
            }

        lax_m = _metrics(_agg(lax_rows, common))
        sna_m = _metrics(_agg(sna_rows, common))

        lax_idx = lax_m["congestion_index"]
        sna_idx = sna_m["congestion_index"]
        if lax_idx is not None and sna_idx is not None:
            if math.isclose(lax_idx, sna_idx):
                lax_result = sna_result = "equally_congested"
            elif lax_idx > sna_idx:
                lax_result, sna_result = "more_congested", "less_congested"
            else:
                lax_result, sna_result = "less_congested", "more_congested"
        else:
            lax_result = sna_result = None

        limitations = (
            "Congestion index measures flight-operation delays and cancellations only. "
            "It does not measure terminal crowding, security queues, gate availability, "
            "or road traffic. "
            f"Caps ({CONGESTION_DELAY_RATE_CAP:.0f}% delay rate, "
            f"{CONGESTION_CANCEL_RATE_CAP:.0f}% cancellation rate, "
            f"{CONGESTION_DELAY_MIN_CAP:.0f} min average delay) are "
            "project assumptions, not official BTS thresholds."
        )
        scope = "Airport operations: scheduled, delayed, and cancelled departures"

        rows = []
        for code, m, result in (("LAX", lax_m, lax_result), ("SNA", sna_m, sna_result)):
            rows.append({
                "comparison_key": "LAX_SNA",
                "airport_code": code,
                **m,
                "comparison_result": result,
                "calculated_at": now,
                "period_start": period_start,
                "period_end": period_end,
                "data_scope": scope,
                "limitations": limitations,
            })

        return rows

    # ------------------------------------------------------------------
    # Use case 3 – ANC long-haul flight percentage
    # ------------------------------------------------------------------

    def calculate_long_haul(self) -> list[dict[str, Any]]:
        """Calculate the long-haul departure percentage for ANC."""
        now = _now_iso()

        routes = self._dal.get_routes_from_airport("ANC")
        if not routes:
            log.warning("No route data found for ANC.")
            return []

        period_start = min(_ym_key(r["year"], r["month"]) for r in routes)
        period_end = max(_ym_key(r["year"], r["month"]) for r in routes)

        total = sum(r["performed_departures"] or 0 for r in routes)
        long_haul = sum(
            r["performed_departures"] or 0
            for r in routes
            if (r["distance_miles"] or 0) >= LONG_HAUL_MILES
        )

        pct: float | None = round(long_haul / total * 100, 2) if total > 0 else None

        return [
            {
                "airport_code": "ANC",
                "long_haul_threshold_miles": LONG_HAUL_MILES,
                "long_haul_flights": long_haul,
                "total_departing_flights": total,
                "long_haul_percentage": pct,
                "calculated_at": now,
                "period_start": period_start,
                "period_end": period_end,
                "data_scope": (
                    "US domestic departing flights from ANC as reported by airlines to BTS"
                ),
                "limitations": (
                    f"Long-haul threshold of {LONG_HAUL_MILES:,.0f} miles is a project assumption, "
                    "not an official BTS classification. "
                    "The routes source represents US domestic reported flights only; "
                    "international long-haul departures from ANC may be omitted."
                ),
            }
        ]

    # ------------------------------------------------------------------
    # Use case 4 – SFO unmet demand analysis
    # ------------------------------------------------------------------

    def calculate_unmet_demand(self) -> list[dict[str, Any]]:
        """Estimate capacity pressure at SFO."""
        now = _now_iso()
        target_pct = TARGET_LOAD_FACTOR * 100

        traffic_rows = self._dal.get_traffic_for_airports(["SFO"])
        ops_rows = self._dal.get_operations_for_airports(["SFO"])

        if not traffic_rows:
            log.warning("No traffic data found for SFO.")
            return []

        # Traffic window is anchored at the latest available traffic month only.
        t_latest = max(_ym_key(r["year"], r["month"]) for r in traffic_rows)
        end_y, end_m = int(t_latest[:4]), int(t_latest[5:])

        curr_keys = _window_keys(end_y, end_m, ANALYSIS_WINDOW_MONTHS)
        prev_end_y, prev_end_m = _offset_ym(end_y, end_m, -ANALYSIS_WINDOW_MONTHS)
        prev_keys = _window_keys(prev_end_y, prev_end_m, ANALYSIS_WINDOW_MONTHS)

        period_end = _ym_key(end_y, end_m)
        ps_y, ps_m = _offset_ym(end_y, end_m, -(ANALYSIS_WINDOW_MONTHS - 1))
        period_start = _ym_key(ps_y, ps_m)

        t_map = {_ym_key(r["year"], r["month"]): r for r in traffic_rows}
        o_map = {_ym_key(r["year"], r["month"]): r for r in ops_rows}

        curr_pax = sum(t_map[k]["passengers"] or 0 for k in curr_keys if k in t_map)
        curr_seats = sum(t_map[k]["seats"] or 0 for k in curr_keys if k in t_map)

        # Validate both windows per the growth requirements.
        curr_months_present = sum(1 for k in curr_keys if k in t_map)
        prev_months_present = sum(1 for k in prev_keys if k in t_map)
        prev_pax = sum(t_map[k]["passengers"] or 0 for k in prev_keys if k in t_map)

        curr_window_complete = curr_months_present == ANALYSIS_WINDOW_MONTHS
        prev_window_complete = prev_months_present == ANALYSIS_WINDOW_MONTHS and prev_pax > 0

        growth_pct: float | None = None
        if curr_window_complete and prev_window_complete:
            growth_pct = round(((curr_pax - prev_pax) / prev_pax) * 100, 2)

        lf = curr_pax / curr_seats if curr_seats > 0 else None
        lf_pct = round(lf * 100, 2) if lf is not None else None

        add_seats: int | None = None
        unmet_proxy: int | None = None
        if lf is not None:
            add_seats = max(0, round((curr_pax / TARGET_LOAD_FACTOR) - curr_seats))
            unmet_proxy = max(0, round(curr_pax - curr_seats * TARGET_LOAD_FACTOR))

        # Operations within the current traffic window.
        ops_keys_in_window = {k for k in curr_keys if k in o_map}
        sched = sum(o_map[k]["scheduled_flights"] or 0 for k in ops_keys_in_window)
        delayed = sum(o_map[k]["delayed_flights"] or 0 for k in ops_keys_in_window)
        cancelled = sum(o_map[k]["cancelled_flights"] or 0 for k in ops_keys_in_window)

        delay_rate: float | None = round(delayed / sched * 100, 2) if sched > 0 else None
        cancel_rate: float | None = round(cancelled / sched * 100, 2) if sched > 0 else None

        flags: list[str] = []
        if lf_pct is not None and lf_pct >= FLAG_HIGH_LOAD_FACTOR:
            flags.append("HIGH_LOAD_FACTOR")
        if growth_pct is not None and growth_pct > 0:
            flags.append("POSITIVE_PASSENGER_GROWTH")
        if growth_pct is None:
            flags.append("INSUFFICIENT_HISTORY")
        if delay_rate is not None and delay_rate >= FLAG_HIGH_DELAY_RATE:
            flags.append("HIGH_DELAY_RATE")
        if cancel_rate is not None and cancel_rate >= FLAG_ELEVATED_CANCELLATIONS:
            flags.append("ELEVATED_CANCELLATIONS")

        # Score is NULL if any required component is unavailable; do not substitute zero.
        score: float | None = None
        if all(v is not None for v in (growth_pct, lf_pct, delay_rate, cancel_rate)):
            load_range = SFO_LOAD_FACTOR_CEIL - SFO_LOAD_FACTOR_FLOOR
            lp = min(
                max((lf_pct - SFO_LOAD_FACTOR_FLOOR) / load_range, 0),  # type: ignore[operator]
                1,
            ) * 100
            gp = min(max(growth_pct / SFO_GROWTH_PRESSURE_CAP, 0), 1) * 100  # type: ignore[operator]
            dp = min(delay_rate / CONGESTION_DELAY_RATE_CAP, 1) * 100  # type: ignore[operator]
            cp = min(cancel_rate / CONGESTION_CANCEL_RATE_CAP, 1) * 100  # type: ignore[operator]
            score = round(
                SFO_SCORE_WEIGHTS["load"] * lp
                + SFO_SCORE_WEIGHTS["growth"] * gp
                + SFO_SCORE_WEIGHTS["delay"] * dp
                + SFO_SCORE_WEIGHTS["cancellation"] * cp,
                1,
            )

        caveats: list[str] = []
        if not curr_window_complete:
            caveats.append(
                f"incomplete current traffic window ({curr_months_present}/12 months); "
                "passenger total is partial"
            )
        if not prev_window_complete:
            caveats.append("growth unavailable: prior 12-month window incomplete")
        caveats.append(_ops_period_label(ops_keys_in_window))

        return [
            {
                "airport_code": "SFO",
                "current_passengers": curr_pax,
                "current_seats": curr_seats,
                "load_factor_percent": lf_pct,
                "passenger_growth_percent": growth_pct,
                "delay_rate_percent": delay_rate,
                "cancellation_rate_percent": cancel_rate,
                "target_load_factor_percent": target_pct,
                "estimated_additional_seats_needed": add_seats,
                "unmet_passenger_capacity_proxy": unmet_proxy,
                "unmet_demand_score": score,
                "reason_flags": ",".join(flags),
                "calculated_at": now,
                "period_start": period_start,
                "period_end": period_end,
                "data_scope": (
                    "Passenger and scheduled-seat data from BTS T-100; "
                    "delay and cancellation metrics from BTS On-Time Performance."
                ),
                "limitations": (
                    "True unmet demand cannot be measured from BTS public data. "
                    "Failed booking attempts, rejected passengers, and willingness-to-pay "
                    "are not recorded. "
                    f"The target load factor of {target_pct:.0f}% and all thresholds are "
                    "project assumptions. "
                    + "; ".join(caveats)
                ),
            }
        ]

    # ------------------------------------------------------------------
    # Coordinator
    # ------------------------------------------------------------------

    def run_all(self) -> dict[str, int]:
        """Calculate all analytics and persist in one atomic transaction.

        Returns the number of rows written per analytics table.
        Rolls back the entire write on any failure so the previous complete
        snapshot is preserved.
        """
        log.info("Calculating expansion scores ...")
        expansion = self.calculate_expansion_scores()

        log.info("Calculating LAX vs SNA congestion ...")
        congestion = self.calculate_congestion()

        log.info("Calculating ANC long-haul percentage ...")
        long_haul = self.calculate_long_haul()

        log.info("Calculating SFO unmet demand ...")
        unmet = self.calculate_unmet_demand()

        log.info("Writing analytics results in a single transaction ...")
        try:
            with self._dal.transaction() as conn:
                conn.execute("DELETE FROM analytics_expansion_scores")
                conn.execute("DELETE FROM analytics_congestion")
                conn.execute("DELETE FROM analytics_long_haul")
                conn.execute("DELETE FROM analytics_unmet_demand")

                if expansion:
                    conn.executemany(_INSERT_EXPANSION, expansion)
                if congestion:
                    conn.executemany(_INSERT_CONGESTION, congestion)
                if long_haul:
                    conn.executemany(_INSERT_LONG_HAUL, long_haul)
                if unmet:
                    conn.executemany(_INSERT_UNMET, unmet)
        except Exception:
            log.exception("Analytics write failed; previous snapshot preserved.")
            raise

        counts = {
            "analytics_expansion_scores": len(expansion),
            "analytics_congestion": len(congestion),
            "analytics_long_haul": len(long_haul),
            "analytics_unmet_demand": len(unmet),
        }
        log.info("Analytics complete: %s", counts)
        return counts
