from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
from anthropic.lib.tools import BetaAsyncFunctionTool, ToolError, beta_async_tool
from pydantic import Field, TypeAdapter

from companion import calendar_api, gmail_api
from companion.google_oauth import GoogleOAuth
from companion.google_session import (
    GoogleAccessRevokedError,
    GoogleNotConnectedError,
    GoogleSession,
)
from companion.models import Capability, LinkStatus
from companion.store import OAUTH_STATE_TTL, Store

_EMAIL_SUMMARIES = TypeAdapter(list[gmail_api.EmailSummary])


@dataclass(frozen=True)
class ToolDeps:
    store: Store
    oauth: GoogleOAuth
    session_for: Callable[[str], GoogleSession]
    connect_url: Callable[[str], str]


@contextmanager
def _google_errors() -> Iterator[None]:
    try:
        yield
    except GoogleNotConnectedError as error:
        raise ToolError(
            f"The user's Google {error.capability} is not connected. Call connect_google with"
            f' capability "{error.capability}" and send them the link.'
        ) from error
    except GoogleAccessRevokedError as error:
        raise ToolError(
            "The user's Google access was revoked or has expired. Call connect_google to get"
            " them a new link."
        ) from error
    except httpx.HTTPStatusError as error:
        raise ToolError(
            f"Google returned HTTP {error.response.status_code}. Tell the user it did not work."
        ) from error
    except httpx.TransportError as error:
        raise ToolError("Could not reach Google. Tell the user it did not work.") from error


def build_tools(
    deps: ToolDeps, user_id: str, timezone: ZoneInfo
) -> list[BetaAsyncFunctionTool[Any]]:
    session = deps.session_for(user_id)

    def local(moment: datetime) -> datetime:
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone)

    async def connect_google(capability: Capability) -> str:
        """Create a link the user taps to connect their Google account, then send it to them.

        Args:
            capability: The access the user's request needs: "calendar" or "email".
        """
        link = deps.store.get_link(user_id)
        if link and link.status is LinkStatus.ACTIVE and link.grants(capability):
            return f"Google {capability} is already connected as {link.google_email}."
        pending = deps.store.create_pending_link(user_id, capability)
        minutes = int(OAUTH_STATE_TTL.total_seconds() // 60)
        return f"Link for the user: {deps.connect_url(pending.state)} (expires in {minutes} min)"

    async def list_calendar_events(start: datetime, end: datetime) -> str:
        """List events on the user's primary Google Calendar that overlap a time window.

        Args:
            start: Window start, ISO 8601. A time without an offset is in the user's timezone.
            end: Window end, ISO 8601. A time without an offset is in the user's timezone.
        """
        with _google_errors():
            events = await calendar_api.list_events(session, local(start), local(end), timezone.key)
        return events.model_dump_json()

    async def create_calendar_event(
        title: str, start: datetime, end: datetime, location: str | None = None
    ) -> str:
        """Add an event to the user's primary Google Calendar. It invites nobody else.

        Args:
            title: Event title.
            start: Start, ISO 8601. A time without an offset is in the user's timezone.
            end: End, ISO 8601. A time without an offset is in the user's timezone.
            location: Optional place or address.
        """
        with _google_errors():
            event = await calendar_api.create_event(
                session, title, local(start), local(end), timezone.key, location
            )
        return event.model_dump_json()

    async def search_email(query: str, max_results: Annotated[int, Field(ge=1, le=25)] = 10) -> str:
        """Search the user's Gmail. Returns sender, subject, date and a snippet per message.

        Args:
            query: Gmail search syntax, e.g. "from:sam newer_than:7d" or "is:unread".
            max_results: How many messages to return, newest first.
        """
        with _google_errors():
            summaries = await gmail_api.search(session, query, max_results)
        return _EMAIL_SUMMARIES.dump_json(summaries).decode()

    async def read_email(message_id: str) -> str:
        """Read the full text of one email found with search_email.

        Args:
            message_id: The id from search_email.
        """
        with _google_errors():
            email = await gmail_api.read(session, message_id)
        return email.model_dump_json()

    async def draft_email(
        to: list[str], subject: str, body: str, reply_to_message_id: str | None = None
    ) -> str:
        """Save an email as a Gmail draft. Nothing is sent until send_email_draft.

        Args:
            to: Recipient email addresses.
            subject: Subject line.
            body: Plain-text body, signed the way the user signs.
            reply_to_message_id: The id of the email this replies to, to keep the thread.
        """
        with _google_errors():
            draft = await gmail_api.create_draft(session, to, subject, body, reply_to_message_id)
        deps.store.record_draft(user_id, draft.draft_id)
        return draft.model_dump_json()

    async def send_email_draft(draft_id: str) -> str:
        """Send a draft the user has seen and approved in a later message.

        Args:
            draft_id: The draft_id from draft_email.
        """
        if not deps.store.draft_seen_by_user(user_id, draft_id):
            raise ToolError(
                "Not sent. Show the user the draft and send it only after they approve it."
            )
        with _google_errors():
            message_id = await gmail_api.send_draft(session, draft_id)
        return f"Sent. Gmail message id {message_id}."

    async def disconnect_google() -> str:
        """Disconnect the user's Google account and revoke this app's access to it."""
        link = deps.store.get_link(user_id)
        if link is None:
            return "No Google account was connected."
        with _google_errors():
            await deps.oauth.revoke(link.refresh_token)
        deps.store.delete_link(user_id)
        return f"Disconnected {link.google_email} and revoked access."

    return [
        beta_async_tool(tool)
        for tool in (
            connect_google,
            list_calendar_events,
            create_calendar_event,
            search_email,
            read_email,
            draft_email,
            send_email_draft,
            disconnect_google,
        )
    ]
