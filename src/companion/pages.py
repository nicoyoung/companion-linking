from html import escape

from pydantic import BaseModel

from companion.models import Capability
from companion.store import OAUTH_STATE_TTL

_STYLE = """
* { box-sizing: border-box; margin: 0; }
body {
  min-height: 100vh; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 18px; padding: 32px 24px; text-align: center;
  background: #fff7f2; color: #241f28;
  font: 17px/1.45 -apple-system, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
}
.mark {
  width: 64px; height: 64px; border-radius: 50%; margin-bottom: 8px;
  background: linear-gradient(135deg, #ff5b3a, #ff8a2b);
  color: #fff; font: 800 30px/64px -apple-system, "SF Pro Display", sans-serif;
}
h1 {
  font: 800 40px/1.08 -apple-system, "SF Pro Display", "Helvetica Neue", sans-serif;
  letter-spacing: -0.02em; max-width: 12em;
}
h1 em {
  font-style: normal; background: linear-gradient(90deg, #ff5b3a, #ff8a2b);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
p { color: #8a8090; max-width: 22em; }
.cta {
  display: block; width: 100%; max-width: 340px; margin-top: 12px; padding: 16px;
  border-radius: 999px; background: linear-gradient(135deg, #ff5b3a, #ff8a2b);
  color: #fff; font-weight: 800; font-size: 20px; text-decoration: none;
}
small { color: #6b6172; font-size: 13.5px; }
"""


class Page(BaseModel):
    title: str
    headline: str
    accent: str
    body: str
    action_label: str | None = None
    action_url: str | None = None
    footnote: str | None = None
    preview_description: str | None = None

    def render(self) -> str:
        action = (
            f'<a class="cta" href="{escape(self.action_url)}">{escape(self.action_label)}</a>'
            if self.action_url and self.action_label
            else ""
        )
        footnote = f"<small>{escape(self.footnote)}</small>" if self.footnote else ""
        # Messages builds its link preview from these tags.
        preview = (
            f'<meta property="og:title" content="{escape(self.title)}">'
            f'<meta property="og:description" content="{escape(self.preview_description)}">'
            if self.preview_description
            else ""
        )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(self.title)}</title>{preview}<style>{_STYLE}</style></head>"
            '<body><div class="mark">C</div>'
            f"<h1>{escape(self.headline)} <em>{escape(self.accent)}</em></h1>"
            f"<p>{escape(self.body)}</p>{action}{footnote}</body></html>"
        )


_CONNECT_COPY = {
    Capability.CALENDAR: (
        "calendar.",
        "So I can answer things like “am I free Friday?” I can read your events and"
        " add new ones. I never invite anyone.",
        "Let Companion read and add events on your calendar.",
    ),
    Capability.EMAIL: (
        "email.",
        "So I can find the reply you’re waiting on. I can read and search your Gmail and"
        " write drafts. Nothing sends until you say so.",
        "Let Companion read your Gmail and write drafts.",
    ),
}


def connect_page(capability: Capability, google_url: str) -> Page:
    accent, body, preview = _CONNECT_COPY[capability]
    noun = "Calendar" if capability is Capability.CALENDAR else "Gmail"
    return Page(
        title=f"Connect your Google {noun}",
        headline="Let me see your",
        accent=accent,
        body=body,
        action_label="Continue with Google",
        action_url=google_url,
        footnote="Text me “disconnect” anytime to undo this.",
        preview_description=preview,
    )


def connected_page() -> Page:
    return Page(
        title="Connected",
        headline="You’re",
        accent="connected.",
        body="Head back to Messages. I’m already on it.",
    )


def scope_declined_page(capability: Capability | None) -> Page:
    access = f"{capability} access" if capability else "that access"
    return Page(
        title="Not connected",
        headline="Almost",
        accent="there.",
        body=f"The box for {access} was unchecked, so I still can’t see it. Head back"
        " to Messages and I’ll send a fresh link.",
    )


def canceled_page() -> Page:
    return Page(
        title="Not connected",
        headline="No",
        accent="worries.",
        body="Nothing was connected. Head back to Messages whenever you’re ready.",
    )


def expired_page() -> Page:
    minutes = int(OAUTH_STATE_TTL.total_seconds() // 60)
    return Page(
        title="Link expired",
        headline="This link",
        accent="expired.",
        body=f"Links last {minutes} minutes. Text me and I’ll send a new one.",
    )


def exchange_failed_page() -> Page:
    return Page(
        title="Not connected",
        headline="That didn’t",
        accent="work.",
        body="Something went wrong connecting Google. Text me and I’ll send a new link.",
    )
