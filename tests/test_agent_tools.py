"""Stored-result reads and tool boundaries using temporary SQLite fixtures."""

import sqlite3
from contextlib import contextmanager

import pytest

from airport_agent.tools import TOOL_SCHEMAS, AgentTools

CASES = [
    ("get_new_england_expansion_ranking", "get_expansion_scores",
     "analytics_expansion_scores", "BOS", "new_england_expansion_ranking"),
    ("get_lax_sna_congestion_comparison", "get_congestion_comparison",
     "analytics_congestion", "LAX", "lax_sna_congestion"),
    ("get_anc_long_haul_percentage", "get_long_haul_analysis",
     "analytics_long_haul", "ANC", "anc_long_haul"),
    ("get_sfo_unmet_demand_analysis", "get_unmet_demand_analysis",
     "analytics_unmet_demand", "SFO", "sfo_unmet_demand"),
]


def insert_result(db, table, code, **values):
    """Fixture SQL only: populate every column to check faithful pass-through."""
    row = {}
    for column in db.fetchall(f"PRAGMA table_info({table})"):
        row[column["name"]] = "stored; text, unchanged" if column["type"] == "TEXT" else 12.5
    row.update(
        airport_code=code, calculated_at="2025-01-02T03:04:05",
        period_start="2024-01", period_end="2024-12", **values,
    )
    columns = ",".join(row)
    placeholders = ",".join("?" for _ in row)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))
    return dict(db.fetchall(f"SELECT * FROM {table}")[-1])


@pytest.mark.parametrize("name,method,table,code,analysis", CASES)
def test_dal_and_tool_preserve_all_fields_read_only(
    tmp_db, dal, monkeypatch, name, method, table, code, analysis,
):
    if method == "get_expansion_scores":
        # Force is_rankable=1 so the row lands in ranked_airports under the new split query.
        expected = insert_result(tmp_db, table, code, is_rankable=1, rank_position=1)
    else:
        values = {"comparison_key": "LAX_SNA"} if table == "analytics_congestion" else {}
        expected = insert_result(tmp_db, table, code, **values)
        other = {"comparison_key": "JFK_LGA"} if values else {}
        insert_result(tmp_db, table, "JFK", **other)

    original_connect = tmp_db._connect

    @contextmanager
    def read_only_connect():
        with original_connect() as conn:
            conn.execute("PRAGMA query_only = ON")
            yield conn

    monkeypatch.setattr(tmp_db, "_connect", read_only_connect)
    args = {"limit": 5} if method == "get_expansion_scores" else {}

    if method == "get_expansion_scores":
        dal_result = getattr(dal, method)(**args)
        assert dal_result["ranked_airports"] == [expected]
        assert dal_result["rankable_airport_count"] == 1
        assert dal_result["excluded_airports"] == []
        assert AgentTools(dal).execute(name, args) == {
            "status": "ok", "analysis_type": analysis, "data": dal_result,
        }
    else:
        expected_data = [expected] if method == "get_congestion_comparison" else expected
        assert getattr(dal, method)(**args) == expected_data
        assert AgentTools(dal).execute(name, args) == {
            "status": "ok", "analysis_type": analysis, "data": expected_data,
        }


def test_ranking_order_and_limit(tmp_db, dal):
    for code, rank, rankable in [("ZZZ", None, 0), ("BOS", 2, 1),
                                 ("AAA", None, 0), ("PVD", 1, 1)]:
        insert_result(tmp_db, "analytics_expansion_scores", code,
                      rank_position=rank, is_rankable=rankable,
                      expansion_score=10 if rankable else None)

    result_all = dal.get_expansion_scores(10)
    assert [r["airport_code"] for r in result_all["ranked_airports"]] == ["PVD", "BOS"]
    assert result_all["rankable_airport_count"] == 2
    assert [r["airport_code"] for r in result_all["excluded_airports"]] == ["AAA", "ZZZ"]

    tools = AgentTools(dal)
    for limit in (1, 2, 3, 10):
        data = tools.get_new_england_expansion_ranking(limit)["data"]
        assert data["ranked_airports"] == result_all["ranked_airports"][:limit]
        assert data["excluded_airports"] == result_all["excluded_airports"]

    for airport in result_all["excluded_airports"]:
        assert airport["expansion_score"] is None


def test_congestion_order(tmp_db, dal):
    for code in ("SNA", "LAX"):
        insert_result(tmp_db, "analytics_congestion", code, comparison_key="LAX_SNA")
    assert [r["airport_code"] for r in dal.get_congestion_comparison()] == ["LAX", "SNA"]


@pytest.mark.parametrize("limit", [0, 11, -1, True, False, 1.0, "5", None, [], {}])
def test_invalid_limits(dal, limit):
    with pytest.raises(ValueError, match="integer between 1 and 10"):
        dal.get_expansion_scores(limit)
    result = AgentTools(dal).get_new_england_expansion_ranking(limit)
    assert result["error_code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("name,method,table,code,analysis", CASES)
def test_empty_results(dal, name, method, table, code, analysis):
    args = {"limit": 5} if method == "get_expansion_scores" else {}
    assert AgentTools(dal).execute(name, args) == {
        "status": "error", "error_code": "ANALYTICS_NOT_AVAILABLE",
        "message": "No stored analytics result is available. Run the analytics command first.",
    }


def test_tool_schemas_and_argument_boundaries(dal):
    tools = AgentTools(dal)
    assert set(tools.registry) == {case[0] for case in CASES}
    assert {schema["name"] for schema in TOOL_SCHEMAS} == set(tools.registry)
    for schema in TOOL_SCHEMAS:
        assert schema["strict"] is True
        assert schema["parameters"]["additionalProperties"] is False
        for args in ([], None, {"sql": "DELETE FROM airports"}):
            assert tools.execute(schema["name"], args)["error_code"] == "INVALID_ARGUMENT"
    assert tools.execute(CASES[0][0], {})["error_code"] == "INVALID_ARGUMENT"
    assert tools.execute("execute_sql", {})["error_code"] == "UNKNOWN_TOOL"


def test_excluded_airports_are_returned_separately(tmp_db, dal):
    """Unrankable airports appear in excluded_airports with no score."""
    insert_result(tmp_db, "analytics_expansion_scores", "BOS",
                  rank_position=1, is_rankable=1, expansion_score=75.0)
    insert_result(tmp_db, "analytics_expansion_scores", "PVD",
                  rank_position=None, is_rankable=0, expansion_score=None)

    result = dal.get_expansion_scores(5)
    assert [r["airport_code"] for r in result["ranked_airports"]] == ["BOS"]
    assert result["rankable_airport_count"] == 1
    assert [r["airport_code"] for r in result["excluded_airports"]] == ["PVD"]
    assert result["excluded_airports"][0]["expansion_score"] is None

    tool_data = AgentTools(dal).get_new_england_expansion_ranking(5)["data"]
    assert tool_data["ranked_airports"] == result["ranked_airports"]
    assert tool_data["rankable_airport_count"] == 1
    assert tool_data["excluded_airports"] == result["excluded_airports"]


def test_sqlite_failure_is_safe(dal, monkeypatch):
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(dal, "get_long_haul_analysis", fail)
    result = AgentTools(dal).get_anc_long_haul_percentage()
    assert result["error_code"] == "DATABASE_ERROR"
    assert "private" not in str(result)

    monkeypatch.setattr(dal, "get_expansion_scores", fail)
    result = AgentTools(dal).get_new_england_expansion_ranking(5)
    assert result["error_code"] == "DATABASE_ERROR"
    assert "private" not in str(result)
