# Airport Investment Intelligence Agent

A Streamlit chat interface over deterministic analytics stored in SQLite.
OpenAI interprets questions, selects one of four read-only tools, and explains
the returned evidence. The tools return stored numbers unchanged; the model is
instructed never to calculate KPIs or invent missing values.

## Run locally

Use Python 3.11 or newer and install dependencies with `uv sync`.
`OPENAI_API_KEY` and `OPENAI_MODEL` must be set in the environment before
running either entrypoint. Choose a model available to your account that
supports the Responses API and function calling. No model is hard-coded.

PowerShell (replace the empty values locally):

```powershell
$env:OPENAI_API_KEY = ""
$env:OPENAI_MODEL = ""
```

Bash (replace the empty values locally):

```bash
export OPENAI_API_KEY=""
export OPENAI_MODEL=""
```

`.env.example` lists these variable names with empty values. Copy it to `.env`
and fill in your values. The `--env-file .env` flag (used in the run commands
below) loads that file automatically.
Keep credentials out of source control.

### Terminal interface (ingestion + analytics + chat)

```bash
uv run python main.py
```

This runs the full process in order: data ingestion, deterministic analytics
calculation, then an interactive terminal chat. The first run may take longer
because aviation data must be downloaded from public sources. Later runs are
typically faster because the ETL freshness checks skip datasets that were
updated recently.

### Graphical chat interface

```bash
uv run --env-file .env streamlit run streamlit_app.py
```

Streamlit reads the analytics snapshot already stored in `storage/aviation.db`.
It does not run ingestion or recalculate analytics. If the database or result
rows are missing, run `uv run python main.py` first (or the individual pipeline
commands below). Restart Streamlit after changing environment configuration.

### Running pipeline stages individually

```bash
uv run python -m data_pipeline.data_pipeline
uv run python -m data_pipeline.run_analytics
uv run --env-file .env streamlit run streamlit_app.py
```

Use this sequence when you want to refresh data without entering the terminal
chat, or to troubleshoot individual pipeline stages.

## Supported questions and limitations

| Example question | Scope and interpretation |
| --- | --- |
| Which five New England airports are strongest candidates for terminal expansion? | New England only; ranking limit 1–10. Unrankable airports retain null scores. |
| How does congestion at LAX compare with SNA? | Delay and cancellation pressure only; not crowding, road traffic, gates, or security queues. |
| What percentage of departing ANC flights are long-haul? | Reported US domestic departures; international flights may be omitted. The distance threshold is a project assumption. |
| What does the SFO unmet-demand proxy show, and why? | Capacity-pressure proxy, not measured unserved demand or passengers denied travel. |

Other airports, regions, custom periods, ROI projections and forecasts are not
implemented. Traffic requests cover 36 months, while operations/routes currently
cover one published month. Actual periods can differ and missing inputs produce
null scores. Answers should disclose the stored period, scope and material
limitations, including project assumptions. See [Analytics Design](DOCS/ANALYTICS_DESIGN.md)
for formulas and [Agent Design](DOCS/AGENT_DESIGN.md) for the conversational layer.

Conversation history is plain user/assistant text held only in the active
Streamlit session. Each request sends that history and relevant tool results to
OpenAI using the [Responses function-calling API](https://developers.openai.com/api/docs/guides/function-calling).
Responses use `store=False`; the application adds no persistent chat storage.
Tool continuations preserve response output, including reasoning context, and
execute at most three tool rounds. Voice is not implemented.

## Verification

```bash
uv run pytest
uv run ruff check .
```

Tests use temporary SQLite databases and fake OpenAI clients; no API key or live
OpenAI request is required. For manual acceptance, ask the four examples and a
follow-up, compare numeric claims with the stored tables, and check that an
unsupported request (such as JFK versus LGA congestion) receives a scope explanation.
