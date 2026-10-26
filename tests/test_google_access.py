"""Access tokens expire hourly and users revoke grants from their Google account page. A
stale token must refresh silently, and a dead grant must turn into a reconnect prompt."""

from collections.abc import Callable
from datetime import timedelta

import pytest
import respx
from anthropic.lib.tools import ToolError

from companion.google_oauth import TOKEN_URL
from companion.google_session import GoogleAccessRevokedError, GoogleSession
from companion.models import Capability, LinkStatus
from companion.store import Store
from companion.tools import ToolDeps
from tests.conftest import (
    CALENDAR_SCOPES,
    EVENTS_URL,
    NEW_YORK,
    USER,
    Clock,
    call_tool,
    save_link,
)

EMPTY_EVENTS = {"kind": "calendar#events", "timeZone": NEW_YORK, "items": []}
TOMORROW = {"start": "2026-10-27T00:00:00", "end": "2026-10-28T00:00:00"}


def mock_refresh(google: respx.MockRouter) -> respx.Route:
    return google.post(TOKEN_URL).respond(
        json={
            "access_token": "access-2",
            "expires_in": 3599,
            "scope": " ".join(sorted(CALENDAR_SCOPES)),
            "token_type": "Bearer",
        }
    )


async def test_expired_access_token_is_refreshed_before_the_call(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES, access_token_ttl=timedelta(seconds=30))
    refresh = mock_refresh(google)
    events = google.get(EVENTS_URL).respond(json=EMPTY_EVENTS)

    await call_tool(tool_deps, "list_calendar_events", TOMORROW)

    assert refresh.call_count == 1
    assert events.calls.last.request.headers["Authorization"] == "Bearer access-2"
    link = store.get_link(USER)
    assert link is not None and link.access_token is not None
    assert link.access_token.value.get_secret_value() == "access-2"


async def test_rejected_access_token_is_refreshed_and_retried_once(
    store: Store,
    clock: Clock,
    google: respx.MockRouter,
    session_for: Callable[[str], GoogleSession],
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    mock_refresh(google)
    events = google.get(EVENTS_URL).mock(side_effect=[httpx_response(401), httpx_response(200)])

    await session_for(USER).get(Capability.CALENDAR, EVENTS_URL)

    assert events.call_count == 2
    assert events.calls.last.request.headers["Authorization"] == "Bearer access-2"


async def test_revoked_grant_marks_the_link_and_asks_for_a_reconnect(
    store: Store, clock: Clock, google: respx.MockRouter, tool_deps: ToolDeps
) -> None:
    save_link(store, clock, CALENDAR_SCOPES, access_token_ttl=timedelta(0))
    google.post(TOKEN_URL).respond(
        400,
        json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )

    with pytest.raises(ToolError, match="connect_google") as raised:
        await call_tool(tool_deps, "list_calendar_events", TOMORROW)

    assert isinstance(raised.value.__cause__, GoogleAccessRevokedError)
    link = store.get_link(USER)
    assert link is not None
    assert link.status is LinkStatus.REVOKED


def httpx_response(status_code: int) -> respx.MockResponse:
    return respx.MockResponse(status_code, json=EMPTY_EVENTS if status_code == 200 else {})
