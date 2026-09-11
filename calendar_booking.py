"""
calendar_booking.py
V2 — Calendar booking.

Authorizes as YOUR actual Google account (OAuth, not a service account) —
required because Google service accounts cannot send calendar invites to
attendees without a paid Workspace account + Domain-Wide Delegation, which
a personal Gmail account doesn't have. This way, meeting invites are sent
exactly like using Google Calendar normally.

First run opens a browser for one-time consent and saves token.json for
reuse. credentials.json (from Google Cloud Console) must already exist in
this folder — see setup instructions.

Design: the actual Google API calls are kept in small, thin functions
(_get_busy_periods, book_meeting's API portion) separate from the slot
-computation logic (_compute_free_slots), which is a pure function and
fully testable without hitting the real network.
"""

import json
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from database import get_conn, log_activity, now_iso
from leads import get_lead

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

BUSINESS_TIMEZONE = os.environ.get("BUSINESS_TIMEZONE", "UTC")
BUSINESS_START_HOUR = int(os.environ.get("BUSINESS_START_HOUR", "9"))
BUSINESS_END_HOUR = int(os.environ.get("BUSINESS_END_HOUR", "17"))
DEFAULT_MEETING_MINUTES = 30


def _get_credentials() -> Credentials:
    """
    Loads saved credentials, refreshing if needed.

    Two sources, checked in order:
    1. GOOGLE_TOKEN_JSON env var (a Streamlit Cloud secret) — the deployed
       server has no browser to do interactive login, so we reuse the
       token generated once locally. This works because a saved token
       already bundles refresh_token + client_id + client_secret together,
       so refreshing never needs the interactive flow again.
    2. token.json file — for local development, where the interactive
       browser flow (source #3 below) can actually run the first time.
    3. If neither exists locally, falls back to running the interactive
       browser consent flow using credentials.json (local dev only —
       this will fail on a headless server, by design).
    """
    creds = None

    token_json_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_json_env), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise RuntimeError(
                    f"{CREDENTIALS_FILE} not found. Download it from Google Cloud Console "
                    "(OAuth client ID, Desktop app type) and place it in this folder."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Only persist to the local file when we're NOT running from a
        # server secret (no point overwriting a file that isn't being read).
        if not token_json_env:
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

    return creds


def _get_service():
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def _get_busy_periods(service, time_min: datetime, time_max: datetime) -> list[tuple[datetime, datetime]]:
    """Queries Google's freebusy API for the primary calendar. Returns (start, end) datetime tuples."""
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "items": [{"id": "primary"}],
    }
    result = service.freebusy().query(body=body).execute()
    busy_raw = result["calendars"]["primary"]["busy"]
    return [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
        for b in busy_raw
    ]


def _compute_free_slots(
    busy_periods: list[tuple[datetime, datetime]],
    days_ahead: int,
    business_start_hour: int,
    business_end_hour: int,
    duration_minutes: int,
    tz: ZoneInfo,
    now: datetime = None,
) -> list[dict]:
    """
    Pure function (no network) — generates candidate meeting slots for the
    next `days_ahead` weekdays within business hours, excluding anything
    that overlaps a busy period. Returns [{"start": datetime, "end": datetime}, ...].
    """
    if now is None:
        now = datetime.now(tz)

    slots = []
    duration = timedelta(minutes=duration_minutes)

    for day_offset in range(days_ahead):
        day = (now + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:  # skip Saturday/Sunday
            continue

        day_start = datetime.combine(day, time(hour=business_start_hour), tzinfo=tz)
        day_end = datetime.combine(day, time(hour=business_end_hour), tzinfo=tz)

        slot_start = max(day_start, now) if day_offset == 0 else day_start

        while slot_start + duration <= day_end:
            slot_end = slot_start + duration
            overlaps = any(slot_start < busy_end and slot_end > busy_start for busy_start, busy_end in busy_periods)
            if not overlaps:
                slots.append({"start": slot_start, "end": slot_end})
            slot_start += duration

    return slots


def get_available_slots(days_ahead: int = 7, duration_minutes: int = DEFAULT_MEETING_MINUTES) -> list[dict]:
    """
    Full pipeline: authenticates, fetches busy periods from Google Calendar,
    computes open slots within business hours. Returns slots as ISO strings:
    [{"start": "2026-09-15T09:00:00-05:00", "end": "..."}, ...]
    """
    tz = ZoneInfo(BUSINESS_TIMEZONE)
    now = datetime.now(tz)
    time_max = now + timedelta(days=days_ahead)

    service = _get_service()
    busy_periods = _get_busy_periods(service, now, time_max)

    # Google's freebusy API treats all-day events (reminders, holidays, etc.)
    # as blocking the ENTIRE day, which isn't a real scheduling conflict —
    # filter out anything spanning close to a full day or more.
    busy_periods = [
        (start, end) for start, end in busy_periods
        if (end - start) < timedelta(hours=18)
    ]

    slots = _compute_free_slots(
        busy_periods, days_ahead, BUSINESS_START_HOUR, BUSINESS_END_HOUR, duration_minutes, tz, now=now
    )

    return [{"start": s["start"].isoformat(), "end": s["end"].isoformat()} for s in slots]


def book_meeting(lead_id: int, start_iso: str, end_iso: str, summary: str = None, description: str = "") -> dict:
    """
    Creates a real Google Calendar event and invites the lead by email.
    Adds a Google Meet link automatically. Saves a record in our meetings
    table. Returns {"meeting_id": int, "calendar_event_id": str, "meet_link": str}.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")
    if not lead.get("email"):
        raise ValueError(f"Lead {lead_id} has no email address — cannot send a calendar invite.")

    service = _get_service()

    event_body = {
        "summary": summary or f"Call with {lead['name']}" + (f" ({lead['company']})" if lead.get("company") else ""),
        "description": description,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "attendees": [{"email": lead["email"]}],
        "conferenceData": {
            "createRequest": {"requestId": f"lead-{lead_id}-{now_iso()}"}
        },
    }

    created_event = service.events().insert(
        calendarId="primary",
        body=event_body,
        sendUpdates="all",
        conferenceDataVersion=1,
    ).execute()

    meet_link = created_event.get("hangoutLink", "")
    timestamp = now_iso()

    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO meetings (lead_id, calendar_event_id, start_at, end_at, status, meet_link, created_at)
            VALUES (?, ?, ?, ?, 'scheduled', ?, ?)
            """,
            (lead_id, created_event["id"], start_iso, end_iso, meet_link, timestamp),
        )
        meeting_id = cursor.lastrowid

    log_activity(lead_id, "meeting_booked", f"Meeting {meeting_id} booked for {start_iso}, event_id={created_event['id']}")

    return {"meeting_id": meeting_id, "calendar_event_id": created_event["id"], "meet_link": meet_link}


def cancel_meeting(meeting_id: int) -> None:
    """Cancels a booked meeting on Google Calendar and marks it cancelled in our DB."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        raise ValueError(f"Meeting {meeting_id} not found.")
    meeting = dict(row)

    service = _get_service()
    try:
        service.events().delete(calendarId="primary", eventId=meeting["calendar_event_id"], sendUpdates="all").execute()
    except Exception as e:
        print(f"[calendar] Could not delete Google Calendar event (may already be gone): {e}")

    with get_conn() as conn:
        conn.execute("UPDATE meetings SET status = 'cancelled' WHERE id = ?", (meeting_id,))

    log_activity(meeting["lead_id"], "meeting_cancelled", f"Meeting {meeting_id} cancelled")


def get_meetings_for_lead(lead_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meetings WHERE lead_id = ? ORDER BY start_at DESC", (lead_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_meetings() -> list[dict]:
    """All meetings across all leads — used for aggregate analytics (funnel, meeting-booking rate)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM meetings ORDER BY start_at DESC").fetchall()
        return [dict(r) for r in rows]