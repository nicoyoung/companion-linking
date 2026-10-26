from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, SecretStr


class Capability(StrEnum):
    CALENDAR = "calendar"
    EMAIL = "email"


class LinkStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


CAPABILITY_SCOPES: dict[Capability, frozenset[str]] = {
    Capability.CALENDAR: frozenset({"https://www.googleapis.com/auth/calendar.events"}),
    Capability.EMAIL: frozenset(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        }
    ),
}


class PendingLink(BaseModel):
    state: str
    user_id: str
    capability: Capability
    code_verifier: str


class AccessToken(BaseModel):
    value: SecretStr
    expires_at: datetime


class GoogleLink(BaseModel):
    user_id: str
    google_email: str
    granted_scopes: frozenset[str]
    refresh_token: SecretStr
    access_token: AccessToken | None
    timezone: str | None
    status: LinkStatus

    def grants(self, capability: Capability) -> bool:
        return CAPABILITY_SCOPES[capability] <= self.granted_scopes

    @property
    def capabilities(self) -> list[Capability]:
        return [capability for capability in Capability if self.grants(capability)]
