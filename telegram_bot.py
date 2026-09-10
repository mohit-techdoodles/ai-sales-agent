"""
telegram_bot.py
V2 — Telegram as a second messaging channel (per client request, in place
of WhatsApp — Telegram's Bot API has no template-approval requirements,
unlike WhatsApp Business API).

IMPORTANT CONSTRAINT: Telegram does not allow cold-messaging someone by
phone/email like SMTP does. The lead must click a personal deep link and
press "Start" on our bot first — that's how we obtain their chat_id. Until
then, we cannot send them anything on Telegram. The typical flow: staff
shares a lead's personal Telegram link (via email), the lead clicks it,
and from then on the conversation can continue on Telegram.

Like email replies, this is polling-based (call check_for_telegram_updates()
on demand) rather than a live webhook — consistent with how reply checking
already works for email on Streamlit's free tier.
"""

import os
import requests
from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from leads import get_lead
from inbound import store_inbound_message
from settings import get_setting, set_setting

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

LAST_UPDATE_ID_KEY = "telegram_last_update_id"


def get_telegram_link(lead_id: int) -> str:
    """
    Personal deep link for a lead. When they click it and press Start,
    Telegram sends our bot a '/start <lead_id>' message, which is how we
    link their chat_id to this lead (see check_for_telegram_updates).
    """
    if not BOT_USERNAME:
        raise RuntimeError("TELEGRAM_BOT_USERNAME not set in .env")
    return f"https://t.me/{BOT_USERNAME}?start={lead_id}"


def send_telegram_message(chat_id: str, text: str) -> dict:
    """Sends a message via the Telegram Bot API. Returns {"sent": bool, "error": str|None}."""
    if not BOT_TOKEN:
        return {"sent": False, "error": "TELEGRAM_BOT_TOKEN not set in .env"}
    if not chat_id:
        return {"sent": False, "error": "This lead hasn't started the Telegram bot yet — no chat_id on file."}

    try:
        response = requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        data = response.json()
        if data.get("ok"):
            return {"sent": True, "error": None}
        return {"sent": False, "error": data.get("description", "Unknown Telegram API error")}
    except Exception as e:
        return {"sent": False, "error": str(e)}


def _find_lead_by_chat_id(chat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE telegram_chat_id = ?", (str(chat_id),)).fetchone()
        return dict(row) if row else None


def _link_lead_to_chat(lead_id: int, chat_id: str) -> None:
    timestamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET telegram_chat_id = ?, updated_at = ? WHERE id = ?",
            (str(chat_id), timestamp, lead_id),
        )
    log_activity(lead_id, "telegram_linked", f"Lead started the Telegram bot, chat_id={chat_id}")


def check_for_telegram_updates() -> dict:
    """
    Polls Telegram for new messages since the last check. Handles two cases:
    1. '/start <lead_id>' — links this chat_id to that lead (deep link click).
    2. A regular message from an already-linked chat_id — stored as an
       inbound reply via the shared inbound.py pipeline (same opt-out
       detection + re-scoring as email replies get).
    Returns a summary dict.
    """
    summary = {"checked": 0, "linked": 0, "matched": 0, "unmatched": 0}

    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    last_update_id = int(get_setting(LAST_UPDATE_ID_KEY, "0"))

    response = requests.get(f"{API_BASE}/getUpdates", params={"offset": last_update_id + 1, "timeout": 5}, timeout=15)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")

    updates = data.get("result", [])
    summary["checked"] = len(updates)
    highest_update_id = last_update_id

    for update in updates:
        highest_update_id = max(highest_update_id, update["update_id"])
        message = update.get("message")
        if not message or "text" not in message:
            continue

        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip().isdigit():
                lead_id = int(parts[1].strip())
                lead = get_lead(lead_id)
                if lead is not None:
                    _link_lead_to_chat(lead_id, chat_id)
                    send_telegram_message(chat_id, f"Hi {lead['name']}, you're now connected with us here on Telegram!")
                    summary["linked"] += 1
                    continue

        lead = _find_lead_by_chat_id(chat_id)
        if lead is None:
            summary["unmatched"] += 1
            continue

        store_inbound_message(lead["id"], channel="telegram", subject="", body=text, external_id=f"tg-{update['update_id']}")
        summary["matched"] += 1

    if highest_update_id > last_update_id:
        set_setting(LAST_UPDATE_ID_KEY, str(highest_update_id))

    return summary