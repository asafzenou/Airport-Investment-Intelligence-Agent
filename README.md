# Airport Investment Intelligence Agent

A Streamlit chat interface over deterministic analytics stored in SQLite.
OpenAI interprets questions, selects one of four read-only tools, and explains
the returned evidence. The tools return stored numbers unchanged; the model is
instructed never to calculate KPIs or invent missing values.

## Run locally

Use Python 3.11 or newer and install dependencies with `uv sync`.
Set `OPENAI_API_KEY` and `OPENAI_MODEL` in the environment of the terminal that
launches Streamlit. Choose a model available to your account that supports the
Responses API and function calling. No model is hard-coded.

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

`.env.example` lists these variables; the application does not load `.env` files.
Keep credentials out of source control. Then run, in order:

```bash
uv run python -m data_pipeline.data_pipeline
uv run python -m data_pipeline.run_analytics
uv run streamlit run streamlit_app.py
```

Ingestion downloads aviation data into `storage/aviation.db`. Analytics reads
that data and replaces the stored result snapshot. Chat never runs either step
automatically. If a database or analytical result is missing, run the first two
commands manually. If results remain unavailable, inspect pipeline logs and
source coverage. Restart Streamlit after changing environment configuration.

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
