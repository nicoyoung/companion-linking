from datetime import date as CalendarDate
from datetime import datetime

from pydantic import BaseModel

from companion.google_model import GoogleModel
from companion.google_session import GoogleSession
from companion.models import Capability

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_EVENTS = 50


class EventTime(GoogleModel):
    date_time: datetime | None = None
    date: CalendarDate | None = None
    time_zone: str | None = None


class GoogleEvent(GoogleModel):
    id: str
    summary: str | None = None
    start: EventTime
    end: EventTime
    location: str | None = None


class GoogleEventList(GoogleModel):
    time_zone: str
    items: list[GoogleEvent] = []


class EventListQuery(GoogleModel):
    time_min: datetime | None = None
    time_max: datetime | None = None
    time_zone: str | None = None
    single_events: bool = True
    order_by: str | None = None
    max_results: int


class NewGoogleEvent(GoogleModel):
    summary: str
    start: EventTime
    end: EventTime
    location: str | None = None


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str
    all_day: bool
    location: str | None


class CalendarEvents(BaseModel):
    timezone: str
    events: list[CalendarEvent]


async def list_events(
    session: GoogleSession, start: datetime, end: datetime, timezone: str
) -> CalendarEvents:
    query = EventListQuery(
        time_min=start,
        time_max=end,
        time_zone=timezone,
        order_by="startTime",
        max_results=MAX_EVENTS,
    )
    response = await session.get(Capability.CALENDAR, EVENTS_URL, query)
    listing = GoogleEventList.model_validate_json(response.content)
    return CalendarEvents(
        timezone=listing.time_zone, events=[_calendar_event(item) for item in listing.items]
    )


async def create_event(
    session: GoogleSession,
    title: str,
    start: datetime,
    end: datetime,
    timezone: str,
    location: str | None,
) -> CalendarEvent:
    event = NewGoogleEvent(
        summary=title,
        start=EventTime(date_time=start, time_zone=timezone),
        end=EventTime(date_time=end, time_zone=timezone),
        location=location,
    )
    response = await session.post(Capability.CALENDAR, EVENTS_URL, event)
    return _calendar_event(GoogleEvent.model_validate_json(response.content))


async def calendar_timezone(session: GoogleSession) -> str:
    response = await session.get(Capability.CALENDAR, EVENTS_URL, EventListQuery(max_results=1))
    return GoogleEventList.model_validate_json(response.content).time_zone


def _calendar_event(event: GoogleEvent) -> CalendarEvent:
    return CalendarEvent(
        id=event.id,
        title=event.summary or "(busy)",
        start=_event_time(event.start),
        end=_event_time(event.end),
        all_day=event.start.date is not None,
        location=event.location,
    )


def _event_time(time: EventTime) -> str:
    moment = time.date_time or time.date
    if moment is None:
        raise ValueError("Google returned an event time with neither dateTime nor date")
    return moment.isoformat()
