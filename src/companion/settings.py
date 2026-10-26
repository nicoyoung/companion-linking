import os
from pathlib import Path

from pydantic import BaseModel, SecretStr

ENV_PREFIX = "COMPANION_"


class Settings(BaseModel):
    google_client_id: str
    google_client_secret: SecretStr
    public_base_url: str
    token_encryption_key: SecretStr
    bridge_secret: SecretStr
    aws_region: str = "us-east-1"
    database_path: Path = Path("companion.db")
    model: str = "us.anthropic.claude-haiku-5-5"

    @classmethod
    def from_environment(cls) -> "Settings":
        """Reads `COMPANION_*` variables; a missing one fails startup with every gap named."""
        values = {
            name.removeprefix(ENV_PREFIX).lower(): value
            for name, value in os.environ.items()
            if name.startswith(ENV_PREFIX)
        }
        return cls.model_validate(values)

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/oauth/google/callback"

    def connect_url(self, state: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/link/{state}"
