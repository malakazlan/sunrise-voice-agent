"""The language model behind a call's turns: what a request and a reply are, and the one
implementation, Anthropic's Messages API through the official SDK, async and streamed.

The turn module (app/agent/turn.py) sees only `Model`; tests give it a scripted fake. The
request carries the model id from the settings, a bounded `max_tokens`, the effort level and
no sampling parameter. The SDK does not retry: a failed request is the turn module's to
handle, at once, with its fixed line.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types import (
    ContentBlock,
    Message,
    MessageParam,
    OutputConfigParam,
    ThinkingConfigParam,
    ToolParam,
)
from httpx2 import AsyncClient

from app.config import Settings

# A spoken reply is one or two sentences.
MAX_TOKENS = 300
# A phone turn does not think: thinking would delay the first word and count against
# MAX_TOKENS. Claude Haiku 5.5 accepts thinking off at effort low, medium or high; models that
# refuse it (Opus 5.5 and Sonnet 5.5 among them) answer such a request with an error.
THINKING: ThinkingConfigParam = {"type": "disabled"}
# The lowest effort level, for the least time to the first word.
OUTPUT_CONFIG: OutputConfigParam = {"effort": "low"}


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the model asked for, with the arguments it gave."""

    id: str
    name: str
    arguments: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The system prompt, the conversation so far and the tools offered, each tool as
    {"name", "description", "input_schema"}."""

    system: str = field(repr=False)
    messages: list[dict[str, Any]] = field(repr=False)
    tools: list[dict[str, Any]] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelReply:
    """Why the model stopped, its message as it is sent back in the conversation, its text
    and the tools it asked for."""

    stop_reason: str | None
    content: list[dict[str, Any]] = field(repr=False)
    text: str = field(repr=False)
    tool_calls: list[ToolCall] = field(repr=False)


class Model(Protocol):
    async def reply(self, request: ModelRequest, first_token: asyncio.Event) -> ModelReply:
        """The model's reply to the request. `first_token` is set when the first piece of
        the reply arrives."""


def _sent_back(block: ContentBlock) -> dict[str, Any]:
    """A block of the model's message as it goes back in the conversation. Thinking blocks
    go back unchanged, signature included. Only the kinds a request without server tools can
    hold are known; any other stops the turn."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    raise ValueError(f"a {block.type} block is not expected in a reply")


def _reply(message: Message) -> ModelReply:
    return ModelReply(
        stop_reason=message.stop_reason,
        content=[_sent_back(block) for block in message.content],
        text="".join(block.text for block in message.content if block.type == "text"),
        tool_calls=[
            ToolCall(id=block.id, name=block.name, arguments=block.input)
            for block in message.content
            if block.type == "tool_use"
        ],
    )


class AnthropicModel:
    """Claude through the Messages API, streamed."""

    def __init__(self, client: AsyncAnthropic, model_id: str) -> None:
        self._client = client
        self._model_id = model_id

    async def reply(self, request: ModelRequest, first_token: asyncio.Event) -> ModelReply:
        async with self._client.messages.stream(
            model=self._model_id,
            max_tokens=MAX_TOKENS,
            system=request.system,
            messages=cast("list[MessageParam]", request.messages),
            tools=cast("list[ToolParam]", request.tools),
            thinking=THINKING,
            output_config=OUTPUT_CONFIG,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    first_token.set()
            message = await stream.get_final_message()
        return _reply(message)

    async def close(self) -> None:
        await self._client.close()


def anthropic_model(
    settings: Settings, http_client: AsyncClient | None = None
) -> AnthropicModel | None:
    """The model of the settings, or None without an API key. `http_client` replaces the
    SDK's own (tests answer with a local transport)."""
    if settings.anthropic_api_key is None:
        return None
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=0,
        http_client=http_client,
    )
    return AnthropicModel(client, settings.llm_model)
