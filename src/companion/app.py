import hmac
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import httpx
from anthropic.lib.bedrock import AsyncAnthropicBedrock
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from companion.agent import PROMPT_PATH, Companion, CompanionPrompt
from companion.conversation import AssistantReply, UserText
from companion.google_oauth import CodeExchangeError, GoogleOAuth
from companion.google_session import GoogleSession
from companion.linking import LinkFlow, LinkOutcome
from companion.settings import Settings
from companion.store import Store
from companion.tools import ToolDeps, build_tools

logger = logging.getLogger(__name__)

GOOGLE_TIMEOUT_SECONDS = 20.0
CHAT_PAGE = Path(__file__).parent / "static" / "chat.html"

_PAGES = {
    LinkOutcome.CONNECTED: "Connected. You can go back to Messages.",
    LinkOutcome.ALREADY_CONNECTED: "Already connected. You can go back to Messages.",
    LinkOutcome.SCOPE_DECLINED: "That access was left unchecked, so it isn't connected yet."
    " Go back to Messages and I'll send a new link.",
    LinkOutcome.CANCELED: "Nothing was connected. You can go back to Messages.",
    LinkOutcome.EXPIRED: "This link has expired. Text me and I'll send a new one.",
}
_EXCHANGE_FAILED_PAGE = "Something went wrong connecting Google. Text me for a new link."


@dataclass(frozen=True)
class Services:
    settings: Settings
    store: Store
    link_flow: LinkFlow
    companion: Companion


class InboundText(BaseModel):
    user_id: str
    text: str


class Reply(BaseModel):
    reply: str


class TranscriptLine(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    at: datetime


ServicesFactory = Callable[[], AbstractAsyncContextManager[Services]]


@asynccontextmanager
async def production_services() -> AsyncIterator[Services]:
    settings = Settings.from_environment()
    prompt = CompanionPrompt.from_yaml(PROMPT_PATH)

    def now() -> datetime:
        return datetime.now(UTC)

    async with (
        httpx.AsyncClient(timeout=GOOGLE_TIMEOUT_SECONDS) as http,
        AsyncAnthropicBedrock(aws_region=settings.aws_region) as bedrock,
    ):
        store = Store(settings.database_path, settings.token_encryption_key, now)
        oauth = GoogleOAuth(
            settings.google_client_id,
            settings.google_client_secret,
            settings.oauth_redirect_uri,
            http,
        )

        def session_for(user_id: str) -> GoogleSession:
            return GoogleSession(user_id, store, oauth, http, now)

        deps = ToolDeps(store, oauth, session_for, settings.connect_url)

        def tools_for(user_id: str, timezone: ZoneInfo):
            return build_tools(deps, user_id, timezone)

        yield Services(
            settings=settings,
            store=store,
            link_flow=LinkFlow(store, oauth, session_for, now),
            companion=Companion(bedrock, settings.model, prompt, store, tools_for, now),
        )


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def create_app(services_factory: ServicesFactory = production_services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with services_factory() as services:
            app.state.services = services
            yield

    app = FastAPI(lifespan=lifespan)

    def require_bridge(
        services: ServicesDep, x_bridge_secret: Annotated[str, Header()] = ""
    ) -> None:
        expected = services.settings.bridge_secret.get_secret_value()
        if not hmac.compare_digest(x_bridge_secret, expected):
            raise HTTPException(status_code=401)

    @app.get("/health")
    async def health() -> str:
        return "ok"

    @app.get("/chat")
    async def chat() -> FileResponse:
        """A phone-style thread for trying the companion without a messaging bridge."""
        return FileResponse(CHAT_PAGE)

    @app.post("/messages", dependencies=[Depends(require_bridge)])
    async def inbound(message: InboundText, services: ServicesDep) -> Reply:
        """The messaging bridge posts each inbound text here and delivers the reply."""
        return Reply(reply=await services.companion.reply(message.user_id, message.text))

    @app.get("/users/{user_id}/transcript", dependencies=[Depends(require_bridge)])
    async def transcript(user_id: str, services: ServicesDep) -> list[TranscriptLine]:
        """What the user saw, including replies sent outside a request, like link follow-ups."""
        lines: list[TranscriptLine] = []
        for stored in services.store.conversation(user_id):
            match stored.entry:
                case UserText(text=text):
                    lines.append(TranscriptLine(role="user", text=text, at=stored.created_at))
                case AssistantReply() as reply if reply.text:
                    lines.append(
                        TranscriptLine(role="assistant", text=reply.text, at=stored.created_at)
                    )
                case _:
                    pass
        return lines

    @app.get("/link/{state}")
    async def start_link(state: str, services: ServicesDep) -> Response:
        url = services.link_flow.authorization_url(state)
        if url is None:
            return _page(_PAGES[LinkOutcome.EXPIRED], status_code=410)
        return RedirectResponse(url, status_code=302)

    @app.get("/oauth/google/callback")
    async def finish_link(
        services: ServicesDep,
        background: BackgroundTasks,
        state: str,
        code: str | None = None,
    ) -> HTMLResponse:
        try:
            result = await services.link_flow.complete(state, code)
        except CodeExchangeError:
            logger.exception("Google code exchange failed")
            return _page(_EXCHANGE_FAILED_PAGE, status_code=502)
        event = result.event_for_companion
        if event is not None and result.user_id is not None:
            background.add_task(services.companion.handle_event, result.user_id, event)
        status = 410 if result.outcome is LinkOutcome.EXPIRED else 200
        return _page(_PAGES[result.outcome], status_code=status)

    return app


def _page(message: str, status_code: int) -> HTMLResponse:
    html = (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<body style="font:18px system-ui;padding:2rem;text-align:center">{message}</body>'
    )
    return HTMLResponse(html, status_code=status_code)
