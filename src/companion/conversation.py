from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from anthropic.types.beta import BetaContentBlock
from pydantic import BaseModel, Field


class EntryKind(StrEnum):
    USER_TEXT = "user_text"
    EVENT = "event"
    TOOL_RESULTS = "tool_results"
    ASSISTANT = "assistant"


class UserText(BaseModel):
    kind: Literal[EntryKind.USER_TEXT] = EntryKind.USER_TEXT
    text: str


class Event(BaseModel):
    """Something that happened outside the chat, such as the user finishing a Google link."""

    kind: Literal[EntryKind.EVENT] = EntryKind.EVENT
    text: str


class ToolResult(BaseModel):
    tool_use_id: str
    content: str
    is_error: bool


class ToolResults(BaseModel):
    kind: Literal[EntryKind.TOOL_RESULTS] = EntryKind.TOOL_RESULTS
    results: list[ToolResult]


class AssistantReply(BaseModel):
    kind: Literal[EntryKind.ASSISTANT] = EntryKind.ASSISTANT
    content: list[BetaContentBlock]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.content if block.type == "text")


ConversationEntry = Annotated[
    UserText | Event | ToolResults | AssistantReply, Field(discriminator="kind")
]


class StoredEntry(BaseModel):
    id: int
    created_at: datetime
    entry: ConversationEntry
