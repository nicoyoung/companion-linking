# companion-linking

A timed experiment: how fast can a text-message AI companion get Google Calendar and Gmail linking live?

A user texts "what's on tomorrow?". The companion sees Google is not connected, texts back a link, and the user taps it on their phone and consents. When the callback lands, the companion answers the original question without being asked again. After that, Claude reads and adds calendar events, searches and reads email, and drafts replies through tool use. It sends a draft only after the user approves it in a later text.

## How linking works

1. The `connect_google` tool creates a single-use OAuth state (15 minutes) bound to the user and one capability (`calendar` or `email`). It asks only for what the current request needs.
2. `GET /link/{state}` redirects to Google's consent screen with PKCE. It has no side effects, so the iMessage link preview cannot spend the link.
3. `GET /oauth/google/callback` consumes the state, exchanges the code, and records the scopes the user actually granted. Google lets users uncheck boxes, so an unchecked capability is reported as not connected. Reloading the page is safe.
4. Refresh tokens are encrypted at rest. Access tokens refresh before expiry. A revoked grant marks the link revoked, and the companion offers a new link.

## Run it

```bash
uv sync
uv run pytest
uv run ruff check . && uv run basedpyright
```

To run it live:

1. In Google Cloud Console, create a project, enable the Gmail API and the Google Calendar API, and configure the OAuth consent screen (External, Testing, with your Google account as a test user).
2. Create an OAuth client of type Web application.
3. Start a tunnel: `brew install cloudflared && cloudflared tunnel --url http://localhost:8000`.
4. Add `<tunnel URL>/oauth/google/callback` to the OAuth client's authorized redirect URIs.
5. Copy `.env.example` to `.env` and fill it in.
6. Start the app: `uv run uvicorn companion.app:create_app --factory --env-file .env --port 8000`.
7. Text as a user through the bridge endpoint, then open the link it sends on your phone:

```bash
curl -s localhost:8000/messages -H "X-Bridge-Secret: $SECRET" \
  -H 'content-type: application/json' -d '{"user_id": "+15555550100", "text": "whats on tmrw"}'
curl -s localhost:8000/users/+15555550100/transcript -H "X-Bridge-Secret: $SECRET"
```

The transcript endpoint shows replies sent outside a request, such as the follow-up after linking.

## What stands between this and real users

Google gates the scopes, not the code:

- `gmail.readonly` and `gmail.compose` are restricted scopes. Production use needs Google's verification plus an annual security assessment from a Google-empanelled assessor.
- Until the app is verified, a Testing-status app is capped at 100 test users, and every grant expires 7 days after consent.

## Not built

- A real iMessage bridge. `POST /messages` stands in for it.
- Outlook and iCloud calendars.
- Multiple Google accounts per user, and inviting others to events.
- Conversation compaction. The full history replays each turn.
- Multi-process deploys. The per-user turn lock lives in one process.

## Timing log

| Time (EDT, 2026-10-26) | Milestone |
| --- | --- |
| 13:52 | Start |
| 14:13 | Code complete: linking flow, Calendar and Gmail tools, agent loop; 30 tests, lint and typecheck clean |
