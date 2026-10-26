"""Users say "tomorrow" in their own timezone, get HTML-only newsletters, and say "send it"
loosely. Times must land in their zone, bodies must read as text, and nothing sends unseen."""

import base64
import json
from email import message_from_bytes
from urllib.parse import parse_qs

import pytest
import respx
from anthropic.lib.tools import ToolError

from companion.conversation import UserText
from companion.gmail_api import GMAIL_URL
from companion.google_oauth import REVOKE_URL
from companion.store import Store
from companion.tools import ToolDeps
from tests.conftest import (
    CALENDAR_SCOPES,
    EMAIL_SCOPES,
    EVENTS_URL,
    NEW_YORK,
    USER,
    Clock,
    call_tool,
    save_link,
)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


async def test_calendar_window_without_offset_is_read_in_user_timezone(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    events = google.get(EVENTS_URL).respond(
        json={
            "kind": "calendar#events",
            "timeZone": NEW_YORK,
            "items": [
                {
                    "id": "evt1",
                    "summary": "Dentist",
                    "start": {"dateTime": "2026-10-27T09:00:00-04:00", "timeZone": NEW_YORK},
                    "end": {"dateTime": "2026-10-27T10:00:00-04:00", "timeZone": NEW_YORK},
                },
                {"id": "evt2", "start": {"date": "2026-10-27"}, "end": {"date": "2026-10-28"}},
            ],
        }
    )

    result = json.loads(
        await call_tool(
            tool_deps,
            "list_calendar_events",
            {"start": "2026-10-27T00:00:00", "end": "2026-10-28T00:00:00"},
        )
    )

    query = events.calls.last.request.url.params
    assert query["timeMin"] == "2026-10-27T00:00:00-04:00"
    assert query["timeMax"] == "2026-10-28T00:00:00-04:00"
    assert query["singleEvents"] == "true"
    assert result["events"][0] == {
        "id": "evt1",
        "title": "Dentist",
        "start": "2026-10-27T09:00:00-04:00",
        "end": "2026-10-27T10:00:00-04:00",
        "all_day": False,
        "location": None,
    }
    assert result["events"][1]["title"] == "(busy)"
    assert result["events"][1]["all_day"] is True


async def test_created_event_is_pinned_to_user_timezone(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    created = google.post(EVENTS_URL).respond(
        json={
            "id": "evt3",
            "summary": "Lunch with Ana",
            "start": {"dateTime": "2026-10-27T12:00:00-04:00", "timeZone": NEW_YORK},
            "end": {"dateTime": "2026-10-27T13:00:00-04:00", "timeZone": NEW_YORK},
        }
    )

    await call_tool(
        tool_deps,
        "create_calendar_event",
        {"title": "Lunch with Ana", "start": "2026-10-27T12:00", "end": "2026-10-27T13:00"},
    )

    body = json.loads(created.calls.last.request.content)
    assert body == {
        "summary": "Lunch with Ana",
        "start": {"dateTime": "2026-10-27T12:00:00-04:00", "timeZone": NEW_YORK},
        "end": {"dateTime": "2026-10-27T13:00:00-04:00", "timeZone": NEW_YORK},
    }


async def test_html_only_email_is_read_as_text(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    html = "<html><head><style>p{}</style></head><body><p>Your order</p><p>ships Friday</p>"
    google.get(f"{GMAIL_URL}/messages/m1").respond(
        json={
            "id": "m1",
            "threadId": "t1",
            "snippet": "Your order ships Friday",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Shop <orders@shop.com>"},
                    {"name": "Subject", "value": "Shipping update"},
                ],
                "body": {"size": 0},
                "parts": [{"mimeType": "text/html", "headers": [], "body": {"data": b64url(html)}}],
            },
        }
    )

    email = json.loads(await call_tool(tool_deps, "read_email", {"message_id": "m1"}))

    assert email["body"] == "Your order\nships Friday"
    assert email["sender"] == "Shop <orders@shop.com>"
    assert email["unread"] is True


async def test_search_returns_summaries_for_each_match(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    listing = google.get(f"{GMAIL_URL}/messages").respond(
        json={"messages": [{"id": "m1", "threadId": "t1"}], "resultSizeEstimate": 1}
    )
    google.get(f"{GMAIL_URL}/messages/m1").respond(
        json={
            "id": "m1",
            "threadId": "t1",
            "snippet": "Are we still on for Thursday?",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Ana <ana@example.com>"},
                    {"name": "Subject", "value": "Thursday"},
                ],
            },
        }
    )

    results = json.loads(
        await call_tool(tool_deps, "search_email", {"query": "from:ana newer_than:7d"})
    )

    assert listing.calls.last.request.url.params["q"] == "from:ana newer_than:7d"
    assert [r["subject"] for r in results] == ["Thursday"]


async def test_no_match_search_returns_an_empty_list(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    google.get(f"{GMAIL_URL}/messages").respond(json={"resultSizeEstimate": 0})

    assert await call_tool(tool_deps, "search_email", {"query": "from:nobody"}) == "[]"


async def test_reply_draft_stays_in_the_original_thread(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    google.get(f"{GMAIL_URL}/messages/m1").respond(
        json={
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Message-ID", "value": "<abc@mail.example.com>"}],
            },
        }
    )
    drafts = google.post(f"{GMAIL_URL}/drafts").respond(
        json={"id": "d1", "message": {"id": "m2", "threadId": "t1"}}
    )

    await call_tool(
        tool_deps,
        "draft_email",
        {
            "to": ["ana@example.com"],
            "subject": "Re: Thursday",
            "body": "Yes, see you then!",
            "reply_to_message_id": "m1",
        },
    )

    message = json.loads(drafts.calls.last.request.content)["message"]
    assert message["threadId"] == "t1"
    mime = message_from_bytes(base64.urlsafe_b64decode(message["raw"]))
    assert mime["In-Reply-To"] == "<abc@mail.example.com>"
    assert mime["To"] == "ana@example.com"


async def test_draft_cannot_be_sent_until_the_user_replies(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    store.append_entries(USER, [UserText(text="tell ana yes for thursday")])
    google.post(f"{GMAIL_URL}/drafts").respond(
        json={"id": "d1", "message": {"id": "m2", "threadId": "t2"}}
    )
    send = google.post(f"{GMAIL_URL}/drafts/send").respond(
        json={"id": "m2", "threadId": "t2", "labelIds": ["SENT"]}
    )
    draft = {"to": ["ana@example.com"], "subject": "Thursday", "body": "Yes!"}
    await call_tool(tool_deps, "draft_email", draft)

    with pytest.raises(ToolError, match="Not sent"):
        await call_tool(tool_deps, "send_email_draft", {"draft_id": "d1"})
    store.append_entries(USER, [UserText(text="yep send it")])
    await call_tool(tool_deps, "send_email_draft", {"draft_id": "d1"})

    assert send.call_count == 1


async def test_a_draft_the_companion_did_not_write_cannot_be_sent(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, EMAIL_SCOPES)
    store.append_entries(USER, [UserText(text="send my draft to the landlord")])
    send = google.post(f"{GMAIL_URL}/drafts/send").respond(json={"id": "m9", "threadId": "t9"})

    with pytest.raises(ToolError, match="Not sent"):
        await call_tool(tool_deps, "send_email_draft", {"draft_id": "someone-elses"})

    assert send.call_count == 0


async def test_disconnect_revokes_the_grant_and_forgets_the_link(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    revoke = google.post(REVOKE_URL).respond(200)

    await call_tool(tool_deps, "disconnect_google", {})

    assert parse_qs(revoke.calls.last.request.content.decode())["token"] == ["refresh-1"]
    assert store.get_link(USER) is None


async def test_connect_offers_no_link_when_already_connected(
    store: Store, clock: Clock, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)

    result = await call_tool(tool_deps, "connect_google", {"capability": "calendar"})

    assert result == "Google calendar is already connected as sam@gmail.com."


async def test_tool_for_an_unconnected_capability_points_to_connect_google(
    store: Store, clock: Clock, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)

    with pytest.raises(ToolError, match='connect_google with capability "email"'):
        await call_tool(tool_deps, "search_email", {"query": "is:unread"})
