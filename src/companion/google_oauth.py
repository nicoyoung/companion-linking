import base64
import hashlib
from typing import Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, SecretStr

from companion.models import CAPABILITY_SCOPES, PendingLink

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
IDENTITY_SCOPES = ("openid", "email")


class AuthorizationRequest(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: Literal["code"] = "code"
    scope: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"] = "S256"
    access_type: Literal["offline"] = "offline"
    include_granted_scopes: Literal["true"] = "true"
    # Forces a refresh token on every link, including a relink after a revoke.
    prompt: Literal["consent"] = "consent"


class ClientCredentials(BaseModel):
    client_id: str
    client_secret: str


class AuthorizationCodeGrant(ClientCredentials):
    grant_type: Literal["authorization_code"] = "authorization_code"
    code: str
    code_verifier: str
    redirect_uri: str


class RefreshTokenGrant(ClientCredentials):
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str


class RevokeRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: SecretStr
    expires_in: int
    refresh_token: SecretStr | None = None
    scope: str

    @property
    def granted_scopes(self) -> frozenset[str]:
        return frozenset(self.scope.split())


class TokenError(BaseModel):
    error: str
    error_description: str | None = None


class UserInfo(BaseModel):
    email: str


class GrantRevokedError(Exception):
    """Google rejected the refresh token: the user revoked access or the grant expired."""


class CodeExchangeError(Exception):
    pass


class GoogleOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        http: httpx.AsyncClient,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = http

    def authorization_url(self, pending: PendingLink) -> str:
        scopes = [*IDENTITY_SCOPES, *sorted(CAPABILITY_SCOPES[pending.capability])]
        request = AuthorizationRequest(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            scope=" ".join(scopes),
            state=pending.state,
            code_challenge=_code_challenge(pending.code_verifier),
        )
        return f"{AUTHORIZATION_URL}?{urlencode(request.model_dump())}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenResponse:
        grant = AuthorizationCodeGrant(
            client_id=self._client_id,
            client_secret=self._client_secret.get_secret_value(),
            code=code,
            code_verifier=code_verifier,
            redirect_uri=self._redirect_uri,
        )
        response = await self._http.post(TOKEN_URL, data=grant.model_dump())
        if response.is_error:
            raise CodeExchangeError(TokenError.model_validate_json(response.content).error)
        return TokenResponse.model_validate_json(response.content)

    async def refresh(self, refresh_token: SecretStr) -> TokenResponse:
        grant = RefreshTokenGrant(
            client_id=self._client_id,
            client_secret=self._client_secret.get_secret_value(),
            refresh_token=refresh_token.get_secret_value(),
        )
        response = await self._http.post(TOKEN_URL, data=grant.model_dump())
        if response.status_code == httpx.codes.BAD_REQUEST:
            error = TokenError.model_validate_json(response.content)
            if error.error == "invalid_grant":
                raise GrantRevokedError(error.error_description)
        response.raise_for_status()
        return TokenResponse.model_validate_json(response.content)

    async def revoke(self, token: SecretStr) -> None:
        revoke = RevokeRequest(token=token.get_secret_value())
        response = await self._http.post(REVOKE_URL, data=revoke.model_dump())
        # Google answers 400 for a token that is already revoked or expired: the goal holds.
        if response.status_code != httpx.codes.BAD_REQUEST:
            response.raise_for_status()

    async def account_email(self, access_token: SecretStr) -> str:
        response = await self._http.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token.get_secret_value()}"},
        )
        response.raise_for_status()
        return UserInfo.model_validate_json(response.content).email


def _code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
