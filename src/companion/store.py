import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr, TypeAdapter

from companion.conversation import ConversationEntry, EntryKind, StoredEntry
from companion.models import AccessToken, Capability, GoogleLink, LinkStatus, PendingLink

OAUTH_STATE_TTL = timedelta(minutes=15)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS google_links (
    user_id TEXT PRIMARY KEY,
    google_email TEXT NOT NULL,
    granted_scopes TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    access_token TEXT,
    access_token_expires_at TEXT,
    timezone TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    entry TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversation_by_user ON conversation (user_id, id);
CREATE TABLE IF NOT EXISTS email_drafts (
    draft_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    drafted_after_entry_id INTEGER NOT NULL
);
"""


class Store:
    """SQLite persistence; OAuth tokens are encrypted at rest with the given Fernet key."""

    def __init__(self, path: Path, encryption_key: SecretStr, now: Callable[[], datetime]):
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._cipher = Fernet(encryption_key.get_secret_value())
        self._now = now

    def create_pending_link(self, user_id: str, capability: Capability) -> PendingLink:
        pending = PendingLink(
            state=secrets.token_urlsafe(32),
            user_id=user_id,
            capability=capability,
            code_verifier=secrets.token_urlsafe(64),
        )
        self._db.execute(
            "INSERT INTO oauth_states (state, user_id, capability, code_verifier, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                pending.state,
                pending.user_id,
                pending.capability,
                pending.code_verifier,
                self._now().isoformat(),
            ),
        )
        return pending

    def open_pending_link(self, state: str) -> PendingLink | None:
        """Returns the unexpired, unconsumed link for `state` without consuming it."""
        row = self._db.execute(
            "SELECT * FROM oauth_states WHERE state = ? AND consumed_at IS NULL AND created_at > ?",
            (state, self._state_cutoff()),
        ).fetchone()
        return _pending_link(row) if row else None

    def consume_pending_link(self, state: str) -> PendingLink | None:
        row = self._db.execute(
            "UPDATE oauth_states SET consumed_at = ?"
            " WHERE state = ? AND consumed_at IS NULL AND created_at > ? RETURNING *",
            (self._now().isoformat(), state, self._state_cutoff()),
        ).fetchone()
        return _pending_link(row) if row else None

    def consumed_pending_link(self, state: str) -> PendingLink | None:
        row = self._db.execute(
            "SELECT * FROM oauth_states WHERE state = ? AND consumed_at IS NOT NULL", (state,)
        ).fetchone()
        return _pending_link(row) if row else None

    def save_link(self, link: GoogleLink) -> None:
        access_token = link.access_token
        self._db.execute(
            "INSERT OR REPLACE INTO google_links (user_id, google_email, granted_scopes,"
            " refresh_token, access_token, access_token_expires_at, timezone, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link.user_id,
                link.google_email,
                " ".join(sorted(link.granted_scopes)),
                self._encrypt(link.refresh_token),
                self._encrypt(access_token.value) if access_token else None,
                access_token.expires_at.isoformat() if access_token else None,
                link.timezone,
                link.status,
            ),
        )

    def get_link(self, user_id: str) -> GoogleLink | None:
        row = self._db.execute(
            "SELECT * FROM google_links WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        access_token = None
        if row["access_token"] is not None:
            access_token = AccessToken(
                value=self._decrypt(row["access_token"]),
                expires_at=datetime.fromisoformat(row["access_token_expires_at"]),
            )
        return GoogleLink(
            user_id=row["user_id"],
            google_email=row["google_email"],
            granted_scopes=frozenset(row["granted_scopes"].split()),
            refresh_token=self._decrypt(row["refresh_token"]),
            access_token=access_token,
            timezone=row["timezone"],
            status=LinkStatus(row["status"]),
        )

    def delete_link(self, user_id: str) -> None:
        self._db.execute("DELETE FROM google_links WHERE user_id = ?", (user_id,))

    def append_entries(self, user_id: str, entries: list[ConversationEntry]) -> None:
        """Appends all entries or none of them."""
        created_at = self._now().isoformat()
        # Unset fields stay unset on reload, so the SDK never sends an explicit null back.
        rows = [
            (user_id, entry.kind, entry.model_dump_json(exclude_none=True), created_at)
            for entry in entries
        ]
        self._db.execute("BEGIN")
        try:
            self._db.executemany(
                "INSERT INTO conversation (user_id, kind, entry, created_at) VALUES (?, ?, ?, ?)",
                rows,
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def conversation(self, user_id: str) -> list[StoredEntry]:
        rows = self._db.execute(
            "SELECT * FROM conversation WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        return [_stored_entry(row) for row in rows]

    def record_draft(self, user_id: str, draft_id: str) -> None:
        self._db.execute(
            "INSERT INTO email_drafts (draft_id, user_id, drafted_after_entry_id)"
            " SELECT ?, ?, COALESCE(MAX(id), 0) FROM conversation WHERE user_id = ?",
            (draft_id, user_id, user_id),
        )

    def draft_seen_by_user(self, user_id: str, draft_id: str) -> bool:
        """True when the user has texted since the draft was written."""
        row = self._db.execute(
            "SELECT 1 FROM email_drafts d JOIN conversation c"
            " ON c.user_id = d.user_id AND c.id > d.drafted_after_entry_id AND c.kind = ?"
            " WHERE d.draft_id = ? AND d.user_id = ? LIMIT 1",
            (EntryKind.USER_TEXT, draft_id, user_id),
        ).fetchone()
        return row is not None

    def _state_cutoff(self) -> str:
        return (self._now() - OAUTH_STATE_TTL).isoformat()

    def _encrypt(self, secret: SecretStr) -> str:
        return self._cipher.encrypt(secret.get_secret_value().encode()).decode()

    def _decrypt(self, token: str) -> SecretStr:
        return SecretStr(self._cipher.decrypt(token.encode()).decode())


def _pending_link(row: sqlite3.Row) -> PendingLink:
    return PendingLink(
        state=row["state"],
        user_id=row["user_id"],
        capability=Capability(row["capability"]),
        code_verifier=row["code_verifier"],
    )


_ENTRY_ADAPTER: TypeAdapter[ConversationEntry] = TypeAdapter(ConversationEntry)


def _stored_entry(row: sqlite3.Row) -> StoredEntry:
    return StoredEntry(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        entry=_ENTRY_ADAPTER.validate_json(row["entry"]),
    )
