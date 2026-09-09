"""Four fixed read-only tools; stored row values pass through unchanged."""

import sqlite3
from collections.abc import Callable
from typing import Any

from data_pipeline.dal.aviation_dal import AviationDAL

_TOOLS = {
    "get_new_england_expansion_ranking": (
        "new_england_expansion_ranking", "Stored New England terminal expansion ranking.",
    ),
    "get_lax_sna_congestion_comparison": (
        "lax_sna_congestion", "Stored LAX versus SNA delay and cancellation pressure.",
    ),
    "get_anc_long_haul_percentage": (
        "anc_long_haul", "Stored ANC long-haul share of reported US domestic departures.",
    ),
    "get_sfo_unmet_demand_analysis": (
        "sfo_unmet_demand", "Stored SFO capacity-pressure proxy, not measured unserved demand.",
    ),
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": (
                {"limit": {"type": "integer", "minimum": 1, "maximum": 10}}
                if analysis == "new_england_expansion_ranking" else {}
            ),
            "required": ["limit"] if analysis == "new_england_expansion_ranking" else [],
            "additionalProperties": False,
        },
    }
    for name, (analysis, description) in _TOOLS.items()
]


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "message": message}


class AgentTools:
    def __init__(self, dal: AviationDAL) -> None:
        self._dal = dal
        self.registry: dict[str, Callable[..., dict[str, Any]]] = {
            name: getattr(self, name) for name in _TOOLS
        }

    def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        """Validate untrusted model arguments before invoking an approved callable."""
        if name not in self.registry:
            return tool_error("UNKNOWN_TOOL", "Requested tool is not supported.")
        required = {"limit"} if name == "get_new_england_expansion_ranking" else set()
        if not isinstance(arguments, dict) or set(arguments) != required:
            return tool_error("INVALID_ARGUMENT", "Supply only the tool's required arguments.")
        return self.registry[name](**arguments)

    def _read(self, analysis: str, read: Callable[[], Any]) -> dict[str, Any]:
        try:
            data = read()
        except sqlite3.Error:
            return tool_error("DATABASE_ERROR", "Cannot read analytics. Check the SQLite database.")
        if not data:
            return tool_error(
                "ANALYTICS_NOT_AVAILABLE",
                "No stored analytics result is available. Run the analytics command first.",
            )
        return {"status": "ok", "analysis_type": analysis, "data": data}

    def get_new_england_expansion_ranking(self, limit: int = 5) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 10:
            return tool_error("INVALID_ARGUMENT", "limit must be an integer between 1 and 10.")
        try:
            data = self._dal.get_expansion_scores(limit)
        except sqlite3.Error:
            return tool_error("DATABASE_ERROR", "Cannot read analytics. Check the SQLite database.")
        if not data["ranked_airports"] and not data["excluded_airports"]:
            return tool_error(
                "ANALYTICS_NOT_AVAILABLE",
                "No stored analytics result is available. Run the analytics command first.",
            )
        return {"status": "ok", "analysis_type": "new_england_expansion_ranking", "data": data}

    def get_lax_sna_congestion_comparison(self) -> dict[str, Any]:
        return self._read("lax_sna_congestion", self._dal.get_congestion_comparison)

    def get_anc_long_haul_percentage(self) -> dict[str, Any]:
        return self._read("anc_long_haul", self._dal.get_long_haul_analysis)

    def get_sfo_unmet_demand_analysis(self) -> dict[str, Any]:
        return self._read("sfo_unmet_demand", self._dal.get_unmet_demand_analysis)
