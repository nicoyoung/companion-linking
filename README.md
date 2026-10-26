# companion-linking

Connects a text-message AI companion to the user's Google Calendar and Gmail.

The user texts in plain language. When a request needs Google access, the companion texts back a link, and the user approves it on their phone. The companion then answers the original request without being asked again. Claude does the reasoning through tool use, and the service is Python and FastAPI.

## What users can ask

- "whats on tmrw" or "am i free friday after 5?"
- "add lunch w ana thursday at noon"
- "did the landlord ever reply?" or "anything from the school this week"
- "read me the second one"
- "tell ana yes for thursday": the companion drafts the reply, shows it, and sends it only after "yep send it"
- "disconnect my email"

## How linking works

1. The companion asks only for the access the current request needs: `calendar` or `email`. The link it texts is single-use and expires in 15 minutes.
2. Opening the link redirects to Google's consent screen with PKCE. The link has no side effects, so the iMessage link preview cannot use it up.
3. The OAuth callback records the scopes the user actually granted. Google lets users uncheck boxes, so an unchecked capability is reported as not connected. Reloading the page is safe.
4. Refresh tokens are encrypted at rest, and access tokens refresh before they expire. If the user revokes access from their Google account, the companion says so and offers a new link.

## Safety

- The prompt tells the companion to treat email content as information, never as instructions. The send check below holds even if the model slips.
- The service refuses to send a draft until the user has texted again after seeing it. The model cannot bypass this check.
- Each user's turns run one at a time, so two quick texts never interleave a tool call with its result.

## Try it

1. In Google Cloud Console, create a project and enable the Gmail API and the Google Calendar API.
2. Configure the OAuth consent screen as External, in Testing, with your Google account as a test user.
3. Create an OAuth client of type Web application.
4. Start a tunnel: `brew install cloudflared && cloudflared tunnel --url http://localhost:8000`.
5. Add `<tunnel URL>/oauth/google/callback` to the client's authorized redirect URIs.
6. Copy `.env.example` to `.env` and fill it in.
7. Start the app: `uv run uvicorn companion.app:create_app --factory --env-file .env --port 8000`.
8. Open `<tunnel URL>/chat` on your phone and text it.

`/chat` is a stand-in for a messaging bridge. A real bridge posts each inbound text to `POST /messages` with the `X-Bridge-Secret` header and delivers the reply. It also reads `GET /users/{user_id}/transcript` for replies sent outside a request, such as the follow-up after linking.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run basedpyright
```

## Before launching to real users

- `gmail.readonly` and `gmail.compose` are restricted scopes. Production use needs Google's app verification plus an annual security assessment from a Google-empanelled assessor.
- Until the app is verified, it is limited to 100 test users, and each grant expires 7 days after consent.

## Limits

- One Google account per user.
- Google Calendar and Gmail only. Outlook and iCloud are not supported.
- Events go on the user's own calendar. It does not invite others.
- The full conversation replays on each turn, with no compaction.
- The per-user turn lock lives in one process, so run a single instance.
