import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from anthropic import AsyncAnthropic
from anthropic.lib.tools import BetaAsyncFunctionTool, ToolError
from anthropic.types.beta import (
    BetaMessageParam,
    BetaTextBlock,
    BetaTextBlockParam,
    BetaToolResultBlockParam,
    BetaToolUseBlock,
)
from pydantic import BaseModel

from companion.conversation import (
    AssistantReply,
    ConversationEntry,
    Event,
    ToolResult,
    ToolResults,
    UserText,
)
from companion.models import GoogleLink, LinkStatus
from companion.store import Store

PROMPT_PATH = Path(__file__).parent / "prompts" / "companion.yaml"
MAX_TOKENS = 16_000
MAX_TOOL_ROUNDS = 10
REFUSAL_REPLY = "Sorry, I can't help with that one."
STUCK_REPLY = "Sorry, I got stuck on that. Could you ask me another way?"
UNKNOWN_TIMEZONE = ZoneInfo("UTC")

logger = logging.getLogger(__name__)

ToolsFactory = Callable[[str, ZoneInfo], list[BetaAsyncFunctionTool[Any]]]


class CompanionPrompt(BaseModel):
    system: str

    @classmethod
    def from_yaml(cls, path: Path) -> "CompanionPrompt":
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Companion:
    def __init__(
        self,
        anthropic: AsyncAnthropic,
        model: str,
        prompt: CompanionPrompt,
        store: Store,
        tools_for: ToolsFactory,
        now: Callable[[], datetime],
    ):
        self._anthropic = anthropic
        self._model = model
        self._prompt = prompt
        self._store = store
        self._tools_for = tools_for
        self._now = now
        # One turn at a time per user, so two quick texts never interleave a tool exchange.
        self._turn_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def reply(self, user_id: str, text: str) -> str:
        return await self._take_turn(user_id, UserText(text=text))

    async def handle_event(self, user_id: str, text: str) -> str:
        return await self._take_turn(user_id, Event(text=text))

    async def _take_turn(self, user_id: str, opening: ConversationEntry) -> str:
        async with self._turn_locks[user_id]:
            self._store.append_entries(user_id, [opening])
            return await self._respond(user_id)

    async def _respond(self, user_id: str) -> str:
        link = self._store.get_link(user_id)
        timezone = ZoneInfo(link.timezone) if link and link.timezone else UNKNOWN_TIMEZONE
        tools = {tool.name: tool for tool in self._tools_for(user_id, timezone)}
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._anthropic.beta.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": self._prompt.system,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": self._context(link, timezone)},
                ],
                tools=[tool.to_dict() for tool in tools.values()],
                messages=self._history(user_id),
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                return self._say(user_id, REFUSAL_REPLY)
            reply = AssistantReply(content=list(response.content))
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                self._store.append_entries(user_id, [reply])
                return reply.text
            results = await asyncio.gather(*(_run_tool(tools, use) for use in tool_uses))
            # Stored together: a tool call saved without its result breaks every later turn.
            self._store.append_entries(user_id, [reply, ToolResults(results=list(results))])
        logger.warning("Tool loop hit its round limit for user %s", user_id)
        return self._say(user_id, STUCK_REPLY)

    def _say(self, user_id: str, text: str) -> str:
        self._store.append_entries(
            user_id, [AssistantReply(content=[BetaTextBlock(type="text", text=text)])]
        )
        return text

    def _context(self, link: GoogleLink | None, timezone: ZoneInfo) -> str:
        local_now = self._now().astimezone(timezone)
        zone = timezone.key if timezone is not UNKNOWN_TIMEZONE else "UTC, user timezone unknown"
        lines = [f"Now: {local_now:%A, %B %-d, %Y, %-I:%M %p} ({zone})"]
        if link is None:
            lines.append("Google: not connected.")
        elif link.status is LinkStatus.REVOKED:
            lines.append(f"Google: access for {link.google_email} was revoked or expired.")
        else:
            connected = ", ".join(link.capabilities) or "nothing"
            lines.append(f"Google: {link.google_email}, connected for {connected}.")
        return "\n".join(lines)

    def _history(self, user_id: str) -> list[BetaMessageParam]:
        return [_message_param(stored.entry) for stored in self._store.conversation(user_id)]


async def _run_tool(
    tools: dict[str, BetaAsyncFunctionTool[Any]], tool_use: BetaToolUseBlock
) -> ToolResult:
    tool = tools.get(tool_use.name)
    if tool is None:
        return ToolResult(tool_use_id=tool_use.id, content="Unknown tool.", is_error=True)
    try:
        content = await tool.call(tool_use.input)
    except (ToolError, ValueError) as error:
        return ToolResult(tool_use_id=tool_use.id, content=str(error), is_error=True)
    if not isinstance(content, str):
        raise TypeError(f"Tool {tool_use.name} returned {type(content).__name__}, not text")
    return ToolResult(tool_use_id=tool_use.id, content=content, is_error=False)


def _message_param(entry: ConversationEntry) -> BetaMessageParam:
    match entry:
        case UserText(text=text):
            return {"role": "user", "content": [_text(text)]}
        case Event(text=text):
            return {"role": "user", "content": [_text(f"<event>{text}</event>")]}
        case ToolResults(results=results):
            return {"role": "user", "content": [_tool_result(result) for result in results]}
        case AssistantReply(content=content):
            return {"role": "assistant", "content": content}


def _text(text: str) -> BetaTextBlockParam:
    return {"type": "text", "text": text}


def _tool_result(result: ToolResult) -> BetaToolResultBlockParam:
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }
