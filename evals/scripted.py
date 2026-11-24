"""A scripted model, for the tests and the scripted evals: it answers each request with the next
step it was given and keeps a copy of every request. Nothing reaches the network."""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.model import ModelReply, ModelRequest, ToolCall

# A reply, or a function that makes one (and may set the first-token event, wait, or raise).
Step = ModelReply | Callable[[asyncio.Event], Awaitable[ModelReply]]


class ScriptExhaustedError(RuntimeError):
    """The model was asked once more than its script has steps."""


def says(words: str) -> ModelReply:
    """A plain answer."""
    return ModelReply(
        stop_reason="end_turn",
        content=[{"type": "text", "text": words}],
        text=words,
        tool_calls=[],
    )


def calls(*tools: tuple[str, object], words: str = "") -> ModelReply:
    """A reply that asks for tools, each with an id of its own."""
    tool_calls = [
        ToolCall(id=f"toolu_{index}", name=n, arguments=a) for index, (n, a) in enumerate(tools)
    ]
    content: list[dict[str, Any]] = [{"type": "text", "text": words}] if words else []
    content += [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in tool_calls
    ]
    return ModelReply(stop_reason="tool_use", content=content, text=words, tool_calls=tool_calls)


class ScriptedModel:
    """Answers each request with the next step and keeps a copy of every request."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []

    async def reply(self, request: ModelRequest, first_token: asyncio.Event) -> ModelReply:
        self.requests.append(copy.deepcopy(request))
        if not self.steps:
            raise ScriptExhaustedError("the model was asked more often than its script has steps")
        step = self.steps.pop(0)
        if isinstance(step, ModelReply):
            first_token.set()
            return step
        return await step(first_token)

    def then(self, *steps: Step) -> None:
        self.steps.extend(steps)
