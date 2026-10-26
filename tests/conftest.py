import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest
import respx
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from anthropic.types.beta import (
    BetaContentBlock,
    BetaMessage,
    BetaStopReason,
    BetaTextBlock,
    BetaThinkingBlock,
    BetaToolUseBlock,
    BetaUsage,
)
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.agent import PROMPT_PATH, Companion, CompanionPrompt
from companion.app import Services, create_app
from companion.google_oauth import TOKEN_URL, USERINFO_URL, GoogleOAuth
from companion.google_session import GoogleSession
from companion.linking import LinkFlow
from companion.models import CAPABILITY_SCOPES, AccessToken, Capability, GoogleLink, LinkStatus
from companion.settings import Settings
from companion.store import Store
from companion.tools import ToolDeps, build_tools

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
USER = "+15555550100"
BRIDGE_SECRET = "bridge-secret"
NEW_YORK = "America/New_York"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_SCOPES = {"openid", "email", *CAPABILITY_SCOPES[Capability.CALENDAR]}
EMAIL_SCOPES = {"openid", "email", *CAPABILITY_SCOPES[Capability.EMAIL]}


class Clock:
    def __init__(self, start: datetime):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class ScriptedClaude:
    """Answers Messages API calls with queued replies and records each request body."""

    def __init__(self) -> None:
        self.replies: list[BetaMessage] = []
        self.requests: list[Any] = []

    def say(self, text: str) -> None:
        self._queue([BetaTextBlock(type="text", text=text)], "end_turn")

    def call(self, name: str, tool_input: dict[str, Any], tool_use_id: str = "") -> None:
        tool_use = BetaToolUseBlock(
            type="tool_use",
            id=tool_use_id or f"toolu_{len(self.replies)}",
            name=name,
            input=tool_input,
        )
        thinking = BetaThinkingBlock(type="thinking", thinking="", signature="sig")
        self._queue([thinking, tool_use], "tool_use")

    def client(self) -> AsyncAnthropic:
        transport = httpx2.MockTransport(self._handle)
        return AsyncAnthropic(
            api_key="test-key",
            http_client=DefaultAsyncHttpxClient(transport=transport),
            max_retries=0,
        )

    def _queue(self, content: list[BetaContentBlock], stop_reason: BetaStopReason) -> None:
        self.replies.append(
            BetaMessage(
                id=f"msg_{len(self.replies)}",
                type="message",
                role="assistant",
                model="claude-opus-5-5",
                content=content,
                stop_reason=stop_reason,
                usage=BetaUsage(input_tokens=1, output_tokens=1),
            )
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return httpx2.Response(200, json=self.replies.pop(0).model_dump(mode="json"))


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        google_client_id="client-id",
        google_client_secret=SecretStr("client-secret"),
        public_base_url="https://companion.test",
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        bridge_secret=SecretStr(BRIDGE_SECRET),
        anthropic_api_key=SecretStr("test-key"),
        database_path=tmp_path / "companion.db",
    )


@pytest.fixture
def store(settings: Settings, clock: Clock) -> Store:
    return Store(settings.database_path, settings.token_encryption_key, clock)


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def oauth(settings: Settings) -> GoogleOAuth:
    return GoogleOAuth(
        settings.google_client_id,
        settings.google_client_secret,
        settings.oauth_redirect_uri,
        httpx.AsyncClient(),
    )


@pytest.fixture
def session_for(store: Store, oauth: GoogleOAuth, clock: Clock) -> Callable[[str], GoogleSession]:
    http = httpx.AsyncClient()
    return lambda user_id: GoogleSession(user_id, store, oauth, http, clock)


@pytest.fixture
def tool_deps(
    store: Store,
    oauth: GoogleOAuth,
    session_for: Callable[[str], GoogleSession],
    settings: Settings,
) -> ToolDeps:
    return ToolDeps(store, oauth, session_for, settings.connect_url)


@pytest.fixture
def claude() -> ScriptedClaude:
    return ScriptedClaude()


@pytest.fixture
def companion(claude: ScriptedClaude, store: Store, tool_deps: ToolDeps, clock: Clock) -> Companion:
    return Companion(
        claude.client(),
        "claude-opus-5-5",
        CompanionPrompt.from_yaml(PROMPT_PATH),
        store,
        lambda user_id, timezone: build_tools(tool_deps, user_id, timezone),
        clock,
    )


@pytest.fixture
def link_flow(
    store: Store, oauth: GoogleOAuth, session_for: Callable[[str], GoogleSession], clock: Clock
) -> LinkFlow:
    return LinkFlow(store, oauth, session_for, clock)


@pytest.fixture
def app_client(
    settings: Settings, store: Store, link_flow: LinkFlow, companion: Companion
) -> Iterator[TestClient]:
    @asynccontextmanager
    async def services() -> AsyncIterator[Services]:
        yield Services(settings=settings, store=store, link_flow=link_flow, companion=companion)

    with TestClient(create_app(services)) as client:
        yield client


def mock_token_exchange(
    google: respx.MockRouter, scopes: set[str], refresh_token: str = "refresh-1"
) -> respx.Route:
    return google.post(TOKEN_URL).respond(
        json={
            "access_token": "access-1",
            "expires_in": 3599,
            "refresh_token": refresh_token,
            "scope": " ".join(sorted(scopes)),
            "token_type": "Bearer",
            "id_token": "header.payload.signature",
        }
    )


def mock_userinfo(google: respx.MockRouter, email: str = "sam@gmail.com") -> respx.Route:
    return google.get(USERINFO_URL).respond(
        json={"sub": "1234", "email": email, "email_verified": True}
    )


def mock_calendar_timezone(google: respx.MockRouter, timezone: str = NEW_YORK) -> respx.Route:
    return google.get(EVENTS_URL, params={"maxResults": "1"}).respond(
        json={"kind": "calendar#events", "summary": "sam@gmail.com", "timeZone": timezone}
    )


def save_link(
    store: Store,
    clock: Clock,
    scopes: set[str],
    status: LinkStatus = LinkStatus.ACTIVE,
    access_token_ttl: timedelta = timedelta(hours=1),
) -> GoogleLink:
    link = GoogleLink(
        user_id=USER,
        google_email="sam@gmail.com",
        granted_scopes=frozenset(scopes),
        refresh_token=SecretStr("refresh-1"),
        access_token=AccessToken(
            value=SecretStr("access-1"), expires_at=clock() + access_token_ttl
        ),
        timezone=NEW_YORK,
        status=status,
    )
    store.save_link(link)
    return link


async def call_tool(deps: ToolDeps, name: str, tool_input: dict[str, Any]) -> str:
    """Runs one companion tool the way the agent loop does, validation included."""
    tools = {tool.name: tool for tool in build_tools(deps, USER, ZoneInfo(NEW_YORK))}
    result = await tools[name].call(tool_input)
    assert isinstance(result, str)
    return result
