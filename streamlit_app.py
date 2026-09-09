"""Session-only chat over the existing analytics snapshot."""

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


@st.cache_resource(scope="session")
def get_tools() -> AgentTools:
    # SQLiteHandler initializes the schema, so never construct it on a missing DB.
    if not DB_PATH.is_file():
        raise AgentError("Database not found. Run ingestion and analytics first.")
    return AgentTools(AviationDAL(SQLiteHandler(DB_PATH)))


@st.cache_resource(scope="session")
def get_service() -> AgentService:
    return AgentService(get_tools())


def main() -> None:
    st.set_page_config(page_title="Airport Investment Intelligence", page_icon="✈️")
    st.title("Airport Investment Intelligence")
    st.write(
        "Explore New England expansion rankings, LAX versus SNA congestion, "
        "ANC long-haul departures, and SFO capacity pressure from stored analytics."
    )
    with st.expander("Example questions", expanded=True):
        st.markdown(
            "- Which airports in New England are strong candidates for terminal expansion?\n"
            "- Compare LA and Santa Ana airport congestion levels.\n"
            "- What is the percentage of long haul flights out of Anchorage airport?\n"
            "- What is the unmet flight demand in SFO airport and why?"
        )

    if "messages" not in st.session_state:
        st.session_state.messages = []
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    try:
        if not DB_PATH.is_file():
            raise AgentError("Database not found. Run ingestion and analytics first.")
        tools = get_tools()
        availability = [tool() for tool in tools.registry.values()]
        if any(result.get("error_code") == "DATABASE_ERROR" for result in availability):
            raise AgentError("Cannot read analytics. Check the SQLite database and retry.")
        if any(result.get("error_code") == "ANALYTICS_NOT_AVAILABLE" for result in availability):
            st.warning("Some stored analytics are unavailable. Run ingestion, then analytics.")
            st.code(SETUP_COMMANDS, language="bash")
        service = get_service()
    except (AgentError, sqlite3.Error) as error:
        st.error(str(error) if isinstance(error, AgentError) else
                 "Cannot open the SQLite database. Check the file and permissions.")
        st.code(SETUP_COMMANDS, language="bash")
        st.stop()

    if question := st.chat_input("Ask about the supported airport analyses"):
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
                st.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
