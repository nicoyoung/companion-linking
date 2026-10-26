import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum

import httpx
from pydantic import BaseModel

from companion.calendar_api import calendar_timezone
from companion.google_oauth import CodeExchangeError, GoogleOAuth, TokenResponse
from companion.google_session import GoogleSession
from companion.models import AccessToken, Capability, GoogleLink, LinkStatus, PendingLink
from companion.store import Store

logger = logging.getLogger(__name__)


class LinkOutcome(StrEnum):
    CONNECTED = "connected"
    ALREADY_CONNECTED = "already_connected"
    SCOPE_DECLINED = "scope_declined"
    CANCELED = "canceled"
    EXPIRED = "expired"


class LinkResult(BaseModel):
    outcome: LinkOutcome
    user_id: str | None = None
    capability: Capability | None = None
    google_email: str | None = None

    @property
    def event_for_companion(self) -> str | None:
        """What the companion should hear about, so it can follow up without being asked."""
        match self.outcome:
            case LinkOutcome.CONNECTED:
                return (
                    f"The user just connected Google {self.capability} ({self.google_email})."
                    " If they asked for something that needed it, do that now."
                )
            case LinkOutcome.SCOPE_DECLINED:
                return (
                    f"The user finished the Google screen but left {self.capability} access"
                    " unchecked, so it is not connected."
                )
            case _:
                return None


class OpenLink(BaseModel):
    capability: Capability
    google_url: str


class LinkFlow:
    def __init__(
        self,
        store: Store,
        oauth: GoogleOAuth,
        session_for: Callable[[str], GoogleSession],
        now: Callable[[], datetime],
    ):
        self._store = store
        self._oauth = oauth
        self._session_for = session_for
        self._now = now

    def open_link(self, state: str) -> OpenLink | None:
        """An unexpired link with its consent URL. Read-only, so link previews cannot spend it."""
        pending = self._store.open_pending_link(state)
        if pending is None:
            return None
        return OpenLink(
            capability=pending.capability, google_url=self._oauth.authorization_url(pending)
        )

    async def complete(self, state: str, code: str | None) -> LinkResult:
        pending = self._store.consume_pending_link(state)
        if pending is None:
            return self._replayed(state)
        if code is None:
            return LinkResult(
                outcome=LinkOutcome.CANCELED,
                user_id=pending.user_id,
                capability=pending.capability,
            )
        tokens = await self._oauth.exchange_code(code, pending.code_verifier)
        email = await self._oauth.account_email(tokens.access_token)
        link = await self._save_link(pending.user_id, email, tokens)
        outcome = (
            LinkOutcome.CONNECTED if link.grants(pending.capability) else LinkOutcome.SCOPE_DECLINED
        )
        return LinkResult(
            outcome=outcome,
            user_id=pending.user_id,
            capability=pending.capability,
            google_email=email,
        )

    def _replayed(self, state: str) -> LinkResult:
        """A reload of the callback page after the state was spent."""
        pending = self._store.consumed_pending_link(state)
        if pending is not None and self._connected(pending):
            return LinkResult(
                outcome=LinkOutcome.ALREADY_CONNECTED,
                user_id=pending.user_id,
                capability=pending.capability,
            )
        return LinkResult(outcome=LinkOutcome.EXPIRED)

    def _connected(self, pending: PendingLink) -> bool:
        link = self._store.get_link(pending.user_id)
        return (
            link is not None
            and link.status is LinkStatus.ACTIVE
            and link.grants(pending.capability)
        )

    async def _save_link(self, user_id: str, email: str, tokens: TokenResponse) -> GoogleLink:
        existing = self._store.get_link(user_id)
        previous = existing if existing and existing.google_email == email else None
        if existing is not None and previous is None:
            await self._oauth.revoke(existing.refresh_token)
        refresh_token = tokens.refresh_token or (previous.refresh_token if previous else None)
        if refresh_token is None:
            raise CodeExchangeError("Google returned no refresh token")
        link = GoogleLink(
            user_id=user_id,
            google_email=email,
            granted_scopes=tokens.granted_scopes,
            refresh_token=refresh_token,
            access_token=AccessToken(
                value=tokens.access_token,
                expires_at=self._now() + timedelta(seconds=tokens.expires_in),
            ),
            timezone=previous.timezone if previous else None,
            status=LinkStatus.ACTIVE,
        )
        self._store.save_link(link)
        if link.grants(Capability.CALENDAR) and link.timezone is None:
            link = await self._with_calendar_timezone(link)
        return link

    async def _with_calendar_timezone(self, link: GoogleLink) -> GoogleLink:
        """Best effort: without it the link still works and times read as UTC."""
        try:
            timezone = await calendar_timezone(self._session_for(link.user_id))
        except httpx.HTTPError:
            logger.warning("Calendar timezone lookup failed for user %s", link.user_id)
            return link
        current = self._store.get_link(link.user_id) or link
        updated = current.model_copy(update={"timezone": timezone})
        self._store.save_link(updated)
        return updated
