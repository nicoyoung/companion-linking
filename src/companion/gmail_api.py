import asyncio
import base64
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel

from companion.google_model import GoogleModel
from companion.google_session import GoogleSession
from companion.models import Capability

GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
SUMMARY_HEADERS = ["From", "To", "Subject", "Date"]
REPLY_HEADERS = ["From", "Subject", "Message-ID", "References"]
# Keeps one long newsletter from filling the model's context window.
MAX_BODY_CHARS = 20_000


class MessageListQuery(GoogleModel):
    q: str
    max_results: int


class MessageGetQuery(GoogleModel):
    format: Literal["metadata", "full"]
    metadata_headers: list[str] | None = None


class MessageRef(GoogleModel):
    id: str
    thread_id: str


class MessageList(GoogleModel):
    messages: list[MessageRef] = []


class Header(GoogleModel):
    name: str
    value: str


class MessagePartBody(GoogleModel):
    data: str | None = None


class MessagePart(GoogleModel):
    mime_type: str
    headers: list[Header] = []
    body: MessagePartBody = MessagePartBody()
    parts: list["MessagePart"] = []


class GmailMessage(GoogleModel):
    id: str
    thread_id: str
    snippet: str = ""
    label_ids: list[str] = []
    payload: MessagePart

    def header(self, name: str) -> str | None:
        return next((h.value for h in self.payload.headers if h.name.lower() == name.lower()), None)


class RawMessage(GoogleModel):
    raw: str
    thread_id: str | None = None


class DraftCreate(GoogleModel):
    message: RawMessage


class DraftSend(GoogleModel):
    id: str


class GmailDraft(GoogleModel):
    id: str
    message: MessageRef


class EmailSummary(BaseModel):
    id: str
    sender: str | None
    to: str | None
    subject: str | None
    date: str | None
    snippet: str
    unread: bool


class Email(EmailSummary):
    body: str
    body_truncated: bool


class DraftedEmail(BaseModel):
    draft_id: str
    to: list[str]
    subject: str
    body: str


async def search(session: GoogleSession, query: str, max_results: int) -> list[EmailSummary]:
    response = await session.get(
        Capability.EMAIL,
        f"{GMAIL_URL}/messages",
        MessageListQuery(q=query, max_results=max_results),
    )
    refs = MessageList.model_validate_json(response.content).messages
    messages = await asyncio.gather(
        *(_get_message(session, ref.id, "metadata", SUMMARY_HEADERS) for ref in refs)
    )
    return [_summary(message) for message in messages]


async def read(session: GoogleSession, message_id: str) -> Email:
    message = await _get_message(session, message_id, "full", None)
    body = _body_text(message.payload) or message.snippet
    return Email(
        **_summary(message).model_dump(),
        body=body[:MAX_BODY_CHARS],
        body_truncated=len(body) > MAX_BODY_CHARS,
    )


async def create_draft(
    session: GoogleSession,
    to: list[str],
    subject: str,
    body: str,
    reply_to_message_id: str | None,
) -> DraftedEmail:
    mime = EmailMessage()
    mime["To"] = ", ".join(to)
    mime["Subject"] = subject
    mime.set_content(body)
    thread_id = None
    if reply_to_message_id is not None:
        original = await _get_message(session, reply_to_message_id, "metadata", REPLY_HEADERS)
        thread_id = original.thread_id
        original_id = original.header("Message-ID")
        if original_id:
            mime["In-Reply-To"] = original_id
            mime["References"] = " ".join(
                filter(None, [original.header("References"), original_id])
            )
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    response = await session.post(
        Capability.EMAIL,
        f"{GMAIL_URL}/drafts",
        DraftCreate(message=RawMessage(raw=raw, thread_id=thread_id)),
    )
    draft = GmailDraft.model_validate_json(response.content)
    return DraftedEmail(draft_id=draft.id, to=to, subject=subject, body=body)


async def send_draft(session: GoogleSession, draft_id: str) -> str:
    response = await session.post(
        Capability.EMAIL, f"{GMAIL_URL}/drafts/send", DraftSend(id=draft_id)
    )
    return MessageRef.model_validate_json(response.content).id


async def _get_message(
    session: GoogleSession,
    message_id: str,
    format: Literal["metadata", "full"],
    headers: list[str] | None,
) -> GmailMessage:
    response = await session.get(
        Capability.EMAIL,
        f"{GMAIL_URL}/messages/{message_id}",
        MessageGetQuery(format=format, metadata_headers=headers),
    )
    return GmailMessage.model_validate_json(response.content)


def _summary(message: GmailMessage) -> EmailSummary:
    return EmailSummary(
        id=message.id,
        sender=message.header("From"),
        to=message.header("To"),
        subject=message.header("Subject"),
        date=message.header("Date"),
        snippet=message.snippet,
        unread="UNREAD" in message.label_ids,
    )


def _body_text(part: MessagePart) -> str | None:
    """Prefers the plain-text alternative; falls back to the HTML one, stripped to text."""
    plain = _find_part(part, "text/plain")
    if plain is not None:
        return _decode(plain)
    html = _find_part(part, "text/html")
    if html is not None:
        return _html_to_text(_decode(html))
    return None


def _find_part(part: MessagePart, mime_type: str) -> MessagePart | None:
    if part.mime_type == mime_type and part.body.data:
        return part
    for child in part.parts:
        found = _find_part(child, mime_type)
        if found is not None:
            return found
    return None


def _decode(part: MessagePart) -> str:
    data = part.body.data or ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    _SKIPPED_TAGS = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return "\n".join(extractor.chunks)
