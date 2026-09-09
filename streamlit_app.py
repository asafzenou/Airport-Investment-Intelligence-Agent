"""Session-only chat over the existing analytics snapshot."""

import datetime
import re
import sqlite3

import streamlit as st

from airport_agent.service import AgentError, AgentService
from airport_agent.tools import AgentTools
from data_pipeline.config import DB_PATH
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler

SETUP_COMMANDS = (
    "uv run python -m data_pipeline.data_pipeline\n"
    "uv run python -m data_pipeline.run_analytics"
)

EXAMPLE_QUESTIONS = [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels.",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
]

# Display-only label mapping for internal reason flags. Stored values are never modified.
_FLAG_LABELS: dict[str, str] = {
    "POSITIVE_PASSENGER_GROWTH": "Positive Passenger Growth",
    "NEGATIVE_PASSENGER_GROWTH": "Negative Passenger Growth",
    "HIGH_LOAD_FACTOR": "High Load Factor",
    "LOW_LOAD_FACTOR": "Low Load Factor",
    "HIGH_DELAY_RATE": "High Delay Rate",
    "LOW_DELAY_RATE": "Low Delay Rate",
    "HIGH_CANCELLATION_RATE": "High Cancellation Rate",
    "LOW_CANCELLATION_RATE": "Low Cancellation Rate",
    "CAPACITY_PRESSURE": "Capacity Pressure",
    "SUSTAINED_HIGH_LOAD_FACTOR": "Sustained High Load Factor",
    "UNMET_DEMAND": "Unmet Demand Indicator",
}

# Agent section heading (lowercased) → (display label, accent color)
_SECTION_STYLES: dict[str, tuple[str, str]] = {
    "direct answer": ("Direct Answer", "#1A56DB"),
    "supporting evidence": ("Supporting Evidence", "#0C1D3B"),
    "observed source data": ("Observed Source Data", "#0C1D3B"),
    "derived metrics": ("Derived Metrics & Analytical Proxies", "#374151"),
    "investment interpretation": ("Investment Interpretation", "#F97316"),
    "assumptions and limitations": ("Assumptions & Limitations", "#6B7280"),
}

_DATASETS = [
    ("airports", "Airport reference data"),
    ("airport_traffic", "Passenger traffic (BTS T-100)"),
    ("airport_operations", "Flight operations data"),
    ("routes", "Route segment data"),
]


@st.cache_resource(scope="session")
def get_tools() -> AgentTools:
    if not DB_PATH.is_file():
        raise AgentError("Database not found. Run ingestion and analytics first.")
    return AgentTools(AviationDAL(SQLiteHandler(DB_PATH)))


@st.cache_resource(scope="session")
def get_service() -> AgentService:
    return AgentService(get_tools())


_RANKING_ROW_RE = re.compile(
    r"^\s*(\d+)[.)]\s+\*{0,2}([^*\n]{3,}?)\*{0,2}\s*[-–—:]+\s*"
    r"(?:(?:score|composite)[:\s]+)?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _humanize_flags(text: str) -> str:
    """Replace internal UPPER_SNAKE flag names with readable labels (display only)."""
    for flag, label in _FLAG_LABELS.items():
        text = text.replace(flag, label)
    return text


def _humanize_airport_names(text: str) -> str:
    """Convert runs of ALL-CAPS words (airport names) to Title Case for display."""
    return re.sub(
        r"\b([A-Z]{2,}(?:\s+[A-Z]{2,})+)\b",
        lambda m: m.group(0).title(),
        text,
    )


def _add_airport_dividers(body: str) -> str:
    """Insert HR separators before bold airport-code headings in Supporting Evidence."""
    out: list[str] = []
    for i, line in enumerate(body.splitlines()):
        if i > 0 and re.match(r"^\*\*[A-Z]{2,5}\b", line.strip()):
            out.append("\n---\n")
        out.append(line)
    return "\n".join(out)


def _try_render_ranking_table(body: str) -> bool:
    """Render numbered rank/airport/score rows as a markdown table. Returns True if rendered."""
    rows: list[tuple[str, str, str]] = []
    other_lines: list[str] = []
    for line in body.splitlines():
        m = _RANKING_ROW_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2).strip(), m.group(3)))
        else:
            other_lines.append(line)
    if len(rows) < 3:
        return False
    preamble = "\n".join(other_lines).strip()
    if preamble:
        st.markdown(preamble)
    table_md = "| Rank | Airport | Score |\n|:----:|---------|------:|\n"
    table_md += "\n".join(f"| {r} | {a} | {s} |" for r, a, s in rows)
    st.markdown(table_md)
    return True


def _parse_sections(markdown: str) -> list[tuple[str, str]]:
    """Split a markdown string on '## ' headings into (heading, body) pairs."""
    if "## " not in markdown:
        return [("", markdown.strip())]
    parts = re.split(r"\n## ", "\n" + markdown.strip())
    sections = []
    for part in parts:
        if not part.strip():
            continue
        head, _, body = part.strip().partition("\n")
        sections.append((head.strip(), body.strip()))
    return sections


def _section_header(label: str, color: str) -> None:
    st.markdown(
        f'<div style="color:{color};font-weight:700;font-size:0.76rem;'
        f'text-transform:uppercase;letter-spacing:0.09em;'
        f'border-left:3px solid {color};padding-left:8px;margin:28px 0 8px 0;">'
        f"{label}</div>",
        unsafe_allow_html=True,
    )


def _render_answer(answer: str) -> None:
    """Render an agent response with visually distinct section headers."""
    answer = _humanize_flags(answer)
    answer = _humanize_airport_names(answer)
    sections = _parse_sections(answer)
    for heading, body in sections:
        if heading:
            label, color = _SECTION_STYLES.get(heading.lower(), (heading, "#1A56DB"))
            _section_header(label, color)
        if not body:
            continue
        key = heading.lower()
        if key == "direct answer" and _try_render_ranking_table(body):
            continue
        if key == "supporting evidence":
            body = _add_airport_dividers(body)
        st.markdown(body)


def _format_period(year: int | None, month: int | None) -> str | None:
    """Return a human-readable period string such as 'June 2026', or None on invalid input."""
    if year is None or month is None:
        return None
    try:
        return datetime.date(int(year), int(month), 1).strftime("%B %Y")
    except ValueError:
        return None


def _format_date_range(stats: dict) -> str | None:
    """Build a compact date-range label from timeseries stats.

    Returns strings like:
      'September 2023 – April 2026 · 32 months'
      'June 2026 · 1 month'
    Returns None when a valid period cannot be determined.
    """
    earliest = _format_period(stats.get("earliest_year"), stats.get("earliest_month"))
    latest = _format_period(stats.get("latest_year"), stats.get("latest_month"))
    if not earliest or not latest:
        return None
    months = stats.get("distinct_months", 0)
    month_label = f"{months} month{'s' if months != 1 else ''}"
    if earliest == latest:
        return f"{latest} · {month_label}"
    return f"{earliest} – {latest} · {month_label}"


def _render_sidebar(tools: AgentTools | None) -> None:
    with st.sidebar:
        st.markdown(
            '<p style="font-weight:700;font-size:1rem;color:#0C1D3B;margin-bottom:2px;">'
            "✈ Airport Investment Intelligence</p>",
            unsafe_allow_html=True,
        )
        st.caption("AI-powered airport investment decision support")
        st.divider()

        st.markdown("**Data Sources**")
        for _, label in _DATASETS:
            st.markdown(f"- {label}")

        if tools is not None:
            st.divider()
            st.markdown("**Data Availability**")
            dal = tools._dal
            try:
                # Airport reference data — no year/month columns
                count = dal.get_row_count("airports")
                st.caption(f"Airport reference data: **{'Available' if count else 'Not loaded'}**")

                # Passenger traffic
                stats = dal.get_timeseries_stats("airport_traffic")
                if not stats["count"]:
                    label = "Not loaded"
                else:
                    label = _format_date_range(stats) or "Available"
                st.caption(f"Passenger traffic (BTS T-100): **{label}**")

                # Flight operations data
                stats = dal.get_timeseries_stats("airport_operations")
                if not stats["count"]:
                    label = "Not loaded"
                else:
                    label = _format_date_range(stats) or "Available"
                st.caption(f"Flight operations data: **{label}**")

                # Route segment data
                stats = dal.get_timeseries_stats("routes")
                if not stats["count"]:
                    label = "Not loaded"
                else:
                    label = _format_date_range(stats) or "Available"
                st.caption(f"Route segment data: **{label}**")

            except Exception:
                st.caption("Data availability could not be determined.")

        st.divider()
        st.caption(
            "Results are deterministic screening indicators derived from BTS public data. "
            "They are not financial-return forecasts or proven investment outcomes."
        )
        st.markdown(
            '<div style="margin-top:1.25rem;padding-top:0.75rem;border-top:1px solid #E5E7EB;">'
            '<p style="font-size:0.71rem;color:#9CA3AF;margin:0;text-align:center;">'
            "Designed and developed by Asaf Zenou</p></div>",
            unsafe_allow_html=True,
        )


def _process_question(question: str, service: AgentService) -> None:
    history = list(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Reading and explaining stored analytics…"):
                answer = service.respond(question, history)
        except AgentError as error:
            st.error(str(error))
        else:
            _render_answer(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})


def main() -> None:
    st.set_page_config(
        page_title="Airport Investment Intelligence",
        page_icon="✈️",
        layout="centered",
    )

    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.25rem; }
        .stButton > button {
            background: #F3F4F6;
            border: 1px solid #D1D5DB;
            border-radius: 6px;
            color: #111827;
            font-size: 0.85rem;
            padding: 7px 12px;
            text-align: left;
            width: 100%;
        }
        .stButton > button:hover {
            background: #EFF6FF;
            border-color: #1A56DB;
            color: #1A56DB;
        }
        .stChatMessage { border-radius: 10px; }
        .stChatMessage p {
            font-size: 0.96rem;
            line-height: 1.78;
            max-width: 70ch;
        }
        .stChatMessage ul, .stChatMessage ol {
            font-size: 0.96rem;
            line-height: 1.78;
            max-width: 70ch;
        }
        .stChatMessage li { margin-bottom: 0.25rem; }
        .stChatMessage hr { margin: 1rem 0; border-color: #E5E7EB; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Page header — compact, no Streamlit default title padding
    st.markdown(
        '<h1 style="font-size:1.6rem;font-weight:700;color:#0C1D3B;margin:0 0 2px 0;">'
        "✈ Airport Investment Intelligence</h1>"
        '<p style="font-size:0.95rem;color:#6B7280;margin:0 0 3px 0;">'
        "AI-powered airport investment decision support</p>"
        '<p style="font-size:0.78rem;color:#9CA3AF;margin:0 0 1rem 0;letter-spacing:0.02em;">'
        "Public aviation data&nbsp;•&nbsp;Deterministic analytics&nbsp;•&nbsp;LLM explanations</p>",
        unsafe_allow_html=True,
    )

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None

    # Bootstrap tools / service; populate sidebar after
    tools: AgentTools | None = None
    service: AgentService | None = None
    try:
        if not DB_PATH.is_file():
            raise AgentError("Database not found. Run ingestion and analytics first.")
        tools = get_tools()
        availability = [tool() for tool in tools.registry.values()]
        if any(r.get("error_code") == "DATABASE_ERROR" for r in availability):
            raise AgentError("Cannot read analytics. Check the SQLite database and retry.")
        if any(r.get("error_code") == "ANALYTICS_NOT_AVAILABLE" for r in availability):
            st.warning("Some stored analytics are unavailable. Run ingestion, then analytics.")
            st.code(SETUP_COMMANDS, language="bash")
        service = get_service()
    except (AgentError, sqlite3.Error) as error:
        msg = str(error) if isinstance(error, AgentError) else (
            "Cannot open the SQLite database. Check the file and permissions."
        )
        st.error(msg)
        st.code(SETUP_COMMANDS, language="bash")
        _render_sidebar(None)
        st.stop()

    _render_sidebar(tools)

    # Example questions — collapsed once the user has started a conversation
    with st.expander("Example questions", expanded=not st.session_state.messages):
        col_a, col_b = st.columns(2)
        for i, q in enumerate(EXAMPLE_QUESTIONS):
            col = col_a if i % 2 == 0 else col_b
            if col.button(q, key=f"example_{i}"):
                st.session_state.pending_question = q
                st.rerun()

    # Render prior conversation
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                _render_answer(message["content"])
            else:
                st.markdown(message["content"])

    # Process a question queued by an example-question button click
    pending = st.session_state.pending_question
    if pending:
        st.session_state.pending_question = None
        assert service is not None  # guaranteed: st.stop() above handles the None case
        _process_question(pending, service)

    if question := st.chat_input("Ask about the supported airport analyses"):
        assert service is not None
        _process_question(question, service)


if __name__ == "__main__":
    main()
