from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel

from companion.google_oauth import GoogleOAuth, GrantRevokedError
from companion.models import AccessToken, Capability, GoogleLink, LinkStatus
from companion.store import Store

# Refresh slightly early so a token never expires between the check and Google's check.
ACCESS_TOKEN_EXPIRY_MARGIN = timedelta(seconds=60)


class GoogleNotConnectedError(Exception):
    def __init__(self, capability: Capability):
        super().__init__(capability)
        self.capability = capability


class GoogleAccessRevokedError(Exception):
    pass


class GoogleSession:
    """Authorized Google API calls for one user, refreshing the access token as needed."""

    def __init__(
        self,
        user_id: str,
        store: Store,
        oauth: GoogleOAuth,
        http: httpx.AsyncClient,
        now: Callable[[], datetime],
    ):
        self._user_id = user_id
        self._store = store
        self._oauth = oauth
        self._http = http
        self._now = now

    async def get(
        self, capability: Capability, url: str, query: BaseModel | None = None
    ) -> httpx.Response:
        return await self._request(capability, "GET", url, query=query, body=None)

    async def post(
        self, capability: Capability, url: str, body: BaseModel, query: BaseModel | None = None
    ) -> httpx.Response:
        return await self._request(capability, "POST", url, query=query, body=body)

    async def _request(
        self,
        capability: Capability,
        method: str,
        url: str,
        query: BaseModel | None,
        body: BaseModel | None,
    ) -> httpx.Response:
        link = self._active_link(capability)
        request = self._http.build_request(
            method,
            url,
            params=query.model_dump(mode="json", by_alias=True, exclude_none=True)
            if query
            else None,
            content=body.model_dump_json(by_alias=True, exclude_none=True) if body else None,
            headers={"Content-Type": "application/json"} if body else None,
        )
        response = await self._send(request, await self._valid_access_token(link))
        if response.status_code == httpx.codes.UNAUTHORIZED:
            response = await self._send(request, await self._refresh(link))
        response.raise_for_status()
        return response

    def _active_link(self, capability: Capability) -> GoogleLink:
        link = self._store.get_link(self._user_id)
        if link is None or not link.grants(capability):
            raise GoogleNotConnectedError(capability)
        if link.status is LinkStatus.REVOKED:
            raise GoogleAccessRevokedError
        return link

    async def _valid_access_token(self, link: GoogleLink) -> AccessToken:
        token = link.access_token
        if token and token.expires_at - ACCESS_TOKEN_EXPIRY_MARGIN > self._now():
            return token
        return await self._refresh(link)

    async def _refresh(self, link: GoogleLink) -> AccessToken:
        try:
            tokens = await self._oauth.refresh(link.refresh_token)
        except GrantRevokedError as error:
            self._store.save_link(link.model_copy(update={"status": LinkStatus.REVOKED}))
            raise GoogleAccessRevokedError from error
        token = AccessToken(
            value=tokens.access_token,
            expires_at=self._now() + timedelta(seconds=tokens.expires_in),
        )
        self._store.save_link(link.model_copy(update={"access_token": token}))
        return token

    async def _send(self, request: httpx.Request, token: AccessToken) -> httpx.Response:
        request.headers["Authorization"] = f"Bearer {token.value.get_secret_value()}"
        return await self._http.send(request)
