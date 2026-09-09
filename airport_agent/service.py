"""Bounded OpenAI Responses API loop with an injectable client."""

import json
import os
from pathlib import Path

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from airport_agent.tools import TOOL_SCHEMAS, AgentTools, tool_error

_PROMPT_PATH = Path(__file__).parent.parent / "agents" / "airport-agent.md"


def _load_instructions() -> str:
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        raise AgentError(
            "Agent prompt file is missing or unreadable. Check agents/airport-agent.md."
        )
    if not text:
        raise AgentError(
            "Agent prompt file is empty. Check agents/airport-agent.md."
        )
    return text


class AgentError(Exception):
    """A short, safe message suitable for display in the chat UI."""


class AgentService:
    def __init__(self, tools: AgentTools, client=None) -> None:
        self.model = os.environ.get("OPENAI_MODEL", "").strip()
        if not self.model or any(char.isspace() for char in self.model):
            raise AgentError("Set OPENAI_MODEL to a valid model ID and restart the app.")
        if client is None:
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                raise AgentError("Set OPENAI_API_KEY in the environment and restart the app.")
            client = OpenAI(timeout=60.0, max_retries=0)
        self._client = client
        self._tools = tools
        self._instructions = _load_instructions()

    def respond(self, message: str, history: list[dict[str, str]]) -> str:
        """History contains only prior user/assistant text, excluding the new message."""
        items = []
        for entry in history:
            if entry.get("role") not in ("user", "assistant") or not isinstance(
                entry.get("content"), str,
            ):
                raise AgentError("Conversation history is invalid. Start a new chat.")
            items.append({"role": entry["role"], "content": entry["content"]})
        if not isinstance(message, str) or not message.strip():
            raise AgentError("Enter a question to continue.")
        items.append({"role": "user", "content": message})

        for round_number in range(4):
            try:
                response = self._client.responses.create(
                    model=self.model,
                    instructions=self._instructions,
                    input=list(items),
                    tools=TOOL_SCHEMAS,
                    store=False,
                    include=["reasoning.encrypted_content"],
                )
            except AuthenticationError:
                raise AgentError("OpenAI authentication failed. Check OPENAI_API_KEY.") from None
            except RateLimitError:
                raise AgentError(
                    "OpenAI rate limit reached. Check quota or try again later.",
                ) from None
            except APITimeoutError:
                raise AgentError("OpenAI timed out. Please try again.") from None
            except APIConnectionError:
                raise AgentError(
                    "Cannot connect to OpenAI. Check your connection and retry.",
                ) from None
            except APIError:
                raise AgentError(
                    "OpenAI request failed. Check model access or try again later.",
                ) from None

            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                if response.output_text and response.output_text.strip():
                    return response.output_text
                raise AgentError("OpenAI returned no answer. Please try again.")
            if round_number == 3:
                raise AgentError("Maximum tool rounds reached. Try a more focused question.")

            # Preserve all output, including reasoning items, for tool continuations.
            items.extend(response.output)
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                except (ValueError, TypeError, RecursionError):
                    result = tool_error("INVALID_ARGUMENT", "Tool arguments must be a JSON object.")
                else:
                    result = self._tools.execute(call.name, arguments)
                items.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result),
                })

        raise AgentError("Maximum tool rounds reached. Try a more focused question.")
