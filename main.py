import asyncio
import os
import sys

from agent.service import AgentError, AgentService
from agent.tools import AgentTools
from data_pipeline.analytics.service import AnalyticsService
from data_pipeline.config import DB_PATH, REFRESH_HOURS
from data_pipeline.dal.aviation_dal import AviationDAL
from data_pipeline.data_handlers.sqlite_handler import SQLiteHandler
from data_pipeline.data_pipeline import run_pipeline
from logger import configure_logging


def validate_environment() -> None:
    missing = [
        var for var in ("OPENAI_API_KEY", "OPENAI_MODEL") if not os.environ.get(var, "").strip()
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        print("Set them before running:  export OPENAI_API_KEY=...  OPENAI_MODEL=...")
        sys.exit(1)


def check_pipeline_warnings(dal: AviationDAL) -> None:
    failed = [
        name
        for name in REFRESH_HOURS
        if (s := dal.get_sync_state(name)) and s.get("status") == "error"
    ]
    if failed:
        print(f"Warning: ETL errors for datasets: {', '.join(failed)}")
        print("Analytics will use any previously stored data.")


def run_chat(service: AgentService) -> None:
    print("\nAsk a question, or type 'exit' to stop.")
    history: list[dict[str, str]] = []

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if question.lower() in {"exit", "quit"}:
            break

        if not question:
            continue

        try:
            answer = service.respond(question, history)
        except AgentError as exc:
            print(f"Error: {exc}")
            continue

        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        print(f"Assistant: {answer}")

    print("Goodbye.")


def main() -> None:
    configure_logging()
    validate_environment()

    print("[1/3] Updating aviation data...")
    asyncio.run(run_pipeline(DB_PATH))

    handler = SQLiteHandler(DB_PATH)
    dal = AviationDAL(handler)

    check_pipeline_warnings(dal)

    print("[2/3] Calculating deterministic analytics...")
    try:
        counts = AnalyticsService(dal).run_all()
    except Exception as exc:
        print(f"Analytics failed: {exc}")
        sys.exit(1)

    for table, count in counts.items():
        print(f"  {table}: {count}")

    print("[3/3] Starting AI assistant...")
    tools = AgentTools(dal)
    service = AgentService(tools)
    run_chat(service)


if __name__ == "__main__":
    main()
