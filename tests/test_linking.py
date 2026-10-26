"""The connect link is opened on a phone, previewed by iMessage, reloaded, abandoned, and
partially consented to. Each of those must leave the user with a correct link state."""

import base64
import hashlib
import html
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import respx
from fastapi.testclient import TestClient

from companion.google_oauth import REVOKE_URL
from companion.models import Capability, LinkStatus
from companion.store import Store
from tests.conftest import (
    CALENDAR_SCOPES,
    EMAIL_SCOPES,
    EVENTS_URL,
    NEW_YORK,
    USER,
    Clock,
    ScriptedClaude,
    mock_calendar_timezone,
    mock_token_exchange,
    mock_userinfo,
    save_link,
)


def google_url_on(page_html: str) -> str:
    match = re.search(r'class="cta" href="([^"]+)"', page_html)
    assert match is not None
    return html.unescape(match.group(1))


def test_connect_page_sends_the_user_to_google_consent_with_pkce(
    app_client: TestClient, store: Store
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)

    response = app_client.get(f"/link/{pending.state}")

    assert response.status_code == 200
    assert '<meta property="og:title" content="Connect your Google Calendar">' in response.text
    query = parse_qs(urlparse(google_url_on(response.text)).query)
    digest = hashlib.sha256(pending.code_verifier.encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [pending.state]
    assert query["access_type"] == ["offline"]
    assert set(query["scope"][0].split()) == CALENDAR_SCOPES


def test_link_preview_fetch_does_not_spend_the_link(app_client: TestClient, store: Store) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)

    preview = app_client.get(f"/link/{pending.state}")
    tap = app_client.get(f"/link/{pending.state}")

    assert preview.status_code == tap.status_code == 200
    assert google_url_on(preview.text) == google_url_on(tap.text)


def test_callback_connects_calendar_and_companion_follows_up(
    app_client: TestClient, store: Store, google: respx.MockRouter, claude: ScriptedClaude
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)
    exchange = mock_token_exchange(google, CALENDAR_SCOPES)
    mock_userinfo(google)
    mock_calendar_timezone(google)
    claude.say("You're connected! Tomorrow you have dentist at 9.")

    response = app_client.get(
        "/oauth/google/callback", params={"state": pending.state, "code": "auth-code"}
    )

    assert response.status_code == 200
    assert "connected." in response.text
    exchange_body = parse_qs(exchange.calls.last.request.content.decode())
    assert exchange_body["code_verifier"] == [pending.code_verifier]
    link = store.get_link(USER)
    assert link is not None
    assert link.google_email == "sam@gmail.com"
    assert link.grants(Capability.CALENDAR)
    assert link.timezone == NEW_YORK
    assert link.refresh_token.get_secret_value() == "refresh-1"
    follow_up_turn = claude.requests[0]["messages"][-1]["content"][0]["text"]
    assert "connected Google calendar" in follow_up_turn


def test_reloading_the_callback_page_does_not_exchange_twice(
    app_client: TestClient, store: Store, google: respx.MockRouter, claude: ScriptedClaude
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)
    exchange = mock_token_exchange(google, CALENDAR_SCOPES)
    mock_userinfo(google)
    mock_calendar_timezone(google)
    claude.say("All set.")
    params = {"state": pending.state, "code": "auth-code"}

    app_client.get("/oauth/google/callback", params=params)
    reload = app_client.get("/oauth/google/callback", params=params)

    assert reload.status_code == 200
    assert "connected." in reload.text
    assert exchange.call_count == 1
    assert len(claude.requests) == 1


def test_unknown_state_is_rejected(app_client: TestClient, store: Store) -> None:
    response = app_client.get(
        "/oauth/google/callback", params={"state": "forged", "code": "auth-code"}
    )

    assert response.status_code == 410
    assert store.get_link(USER) is None


def test_expired_link_is_rejected_without_exchanging_the_code(
    app_client: TestClient, store: Store, google: respx.MockRouter, clock: Clock
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)
    exchange = mock_token_exchange(google, CALENDAR_SCOPES)
    clock.advance(timedelta(minutes=16))

    tap = app_client.get(f"/link/{pending.state}")
    callback = app_client.get(
        "/oauth/google/callback", params={"state": pending.state, "code": "auth-code"}
    )

    assert tap.status_code == callback.status_code == 410
    assert exchange.call_count == 0


def test_canceling_on_google_connects_nothing_and_sends_nothing(
    app_client: TestClient, store: Store, claude: ScriptedClaude
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)

    response = app_client.get(
        "/oauth/google/callback", params={"state": pending.state, "error": "access_denied"}
    )

    assert "Nothing was connected" in response.text
    assert store.get_link(USER) is None
    assert claude.requests == []


def test_unchecked_scope_is_not_reported_as_connected(
    app_client: TestClient, store: Store, google: respx.MockRouter, claude: ScriptedClaude
) -> None:
    pending = store.create_pending_link(USER, Capability.EMAIL)
    mock_token_exchange(google, {"openid", "email"})
    mock_userinfo(google)
    claude.say("Looks like the Gmail box was unchecked. Want a new link?")

    response = app_client.get(
        "/oauth/google/callback", params={"state": pending.state, "code": "auth-code"}
    )

    assert "email access was unchecked" in response.text
    link = store.get_link(USER)
    assert link is not None
    assert not link.grants(Capability.EMAIL)
    assert "unchecked" in claude.requests[0]["messages"][-1]["content"][0]["text"]


def test_adding_email_keeps_calendar_on_the_same_account(
    app_client: TestClient,
    store: Store,
    google: respx.MockRouter,
    claude: ScriptedClaude,
    clock: Clock,
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    pending = store.create_pending_link(USER, Capability.EMAIL)
    mock_token_exchange(google, CALENDAR_SCOPES | EMAIL_SCOPES, refresh_token="refresh-2")
    mock_userinfo(google)
    revoke = google.post(REVOKE_URL).respond(200)
    claude.say("Email's connected too.")

    app_client.get("/oauth/google/callback", params={"state": pending.state, "code": "c"})

    link = store.get_link(USER)
    assert link is not None
    assert link.capabilities == [Capability.CALENDAR, Capability.EMAIL]
    assert link.timezone == NEW_YORK
    assert link.refresh_token.get_secret_value() == "refresh-2"
    assert revoke.call_count == 0


def test_linking_a_different_account_revokes_the_old_grant(
    app_client: TestClient,
    store: Store,
    google: respx.MockRouter,
    claude: ScriptedClaude,
    clock: Clock,
) -> None:
    save_link(store, clock, CALENDAR_SCOPES)
    pending = store.create_pending_link(USER, Capability.CALENDAR)
    mock_token_exchange(google, CALENDAR_SCOPES, refresh_token="refresh-work")
    mock_userinfo(google, email="sam@work.com")
    mock_calendar_timezone(google, timezone="America/Chicago")
    revoke = google.post(REVOKE_URL).respond(200)
    claude.say("Switched to your work calendar.")

    app_client.get("/oauth/google/callback", params={"state": pending.state, "code": "c"})

    assert parse_qs(revoke.calls.last.request.content.decode())["token"] == ["refresh-1"]
    link = store.get_link(USER)
    assert link is not None
    assert link.google_email == "sam@work.com"
    assert link.timezone == "America/Chicago"
    assert link.status is LinkStatus.ACTIVE


def test_bridge_routes_require_the_shared_secret(app_client: TestClient) -> None:
    inbound = app_client.post("/messages", json={"user_id": USER, "text": "hi"})
    transcript = app_client.get(f"/users/{USER}/transcript")

    assert inbound.status_code == transcript.status_code == 401


def test_calendar_still_connects_when_the_timezone_lookup_fails(
    app_client: TestClient, store: Store, google: respx.MockRouter, claude: ScriptedClaude
) -> None:
    pending = store.create_pending_link(USER, Capability.CALENDAR)
    mock_token_exchange(google, CALENDAR_SCOPES)
    mock_userinfo(google)
    google.get(EVENTS_URL).respond(503)
    claude.say("Connected!")

    response = app_client.get(
        "/oauth/google/callback", params={"state": pending.state, "code": "auth-code"}
    )

    assert response.status_code == 200
    link = store.get_link(USER)
    assert link is not None
    assert link.grants(Capability.CALENDAR)
    assert link.timezone is None
