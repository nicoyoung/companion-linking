"""A user who never connected Google asks about their calendar, taps the link, and comes
back expecting an answer; they also fire off two texts at once. The conversation must stay
replayable through all of it, or every later turn fails at the API."""

import asyncio
from typing import Any

import respx

from companion.agent import Companion
from companion.conversation import AssistantReply, EntryKind, ToolResults
from companion.models import Capability
from companion.store import Store
from tests.conftest import (
    CALENDAR_SCOPES,
    EVENTS_URL,
    NEW_YORK,
    USER,
    Clock,
    ScriptedClaude,
    save_link,
)

TOMORROW = {"start": "2026-10-27T00:00:00", "end": "2026-10-28T00:00:00"}


def tool_results(store: Store) -> list[Any]:
    return [
        result
        for stored in store.conversation(USER)
        if isinstance(stored.entry, ToolResults)
        for result in stored.entry.results
    ]


async def test_unconnected_user_asking_about_calendar_gets_a_connect_link(
    companion: Companion, claude: ScriptedClaude, store: Store
) -> None:
    claude.call("list_calendar_events", TOMORROW)
    claude.call("connect_google", {"capability": "calendar"})
    claude.say("I need access to your calendar first: https://companion.test/link/abc")

    reply = await companion.reply(USER, "whats on tmrw")

    assert reply.startswith("I need access to your calendar")
    not_connected, link = tool_results(store)
    assert not_connected.is_error
    assert 'capability "calendar"' in not_connected.content
    assert link.content.startswith("Link for the user: https://companion.test/link/")
    state = link.content.split("/link/")[1].split(" ")[0]
    pending = store.open_pending_link(state)
    assert pending is not None and pending.capability is Capability.CALENDAR
    assert "Google: not connected." in claude.requests[0]["system"][1]["text"]


async def test_tool_calls_and_thinking_replay_unchanged_on_the_next_turn(
    companion: Companion,
    claude: ScriptedClaude,
    store: Store,
    clock: Clock,
    google: respx.MockRouter,
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    google.get(EVENTS_URL).respond(json={"timeZone": NEW_YORK, "items": []})
    claude.call("list_calendar_events", TOMORROW, tool_use_id="toolu_events")
    claude.say("Nothing tomorrow!")
    claude.say("Still nothing.")

    await companion.reply(USER, "anything tomorrow?")
    await companion.reply(USER, "u sure?")

    history = claude.requests[-1]["messages"]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    thinking, tool_use = history[1]["content"]
    assert thinking == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert tool_use["id"] == "toolu_events"
    assert "caller" not in tool_use
    assert history[2]["content"][0]["tool_use_id"] == "toolu_events"
    assert (
        "Now: Monday, October 26, 2026, 2:00 PM (America/New_York)"
        in (claude.requests[-1]["system"][1]["text"])
    )


async def test_two_quick_texts_never_interleave_a_tool_exchange(
    companion: Companion,
    claude: ScriptedClaude,
    store: Store,
    clock: Clock,
    google: respx.MockRouter,
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    google.get(EVENTS_URL).respond(json={"timeZone": NEW_YORK, "items": []})
    claude.call("list_calendar_events", TOMORROW)
    claude.say("Free all day.")
    claude.say("Sure, 7pm works.")

    await asyncio.gather(
        companion.reply(USER, "am i free tmrw"), companion.reply(USER, "for dinner i mean")
    )

    kinds = [stored.entry.kind for stored in store.conversation(USER)]
    assert kinds == [
        EntryKind.USER_TEXT,
        EntryKind.ASSISTANT,
        EntryKind.TOOL_RESULTS,
        EntryKind.ASSISTANT,
        EntryKind.USER_TEXT,
        EntryKind.ASSISTANT,
    ]


async def test_refusal_is_stored_as_the_text_the_user_saw(
    companion: Companion, claude: ScriptedClaude, store: Store
) -> None:
    claude.say("partial")
    claude.replies[0].stop_reason = "refusal"

    reply = await companion.reply(USER, "something it declines")

    last = store.conversation(USER)[-1].entry
    assert isinstance(last, AssistantReply)
    assert last.text == reply == "Sorry, I can't help with that one."
