"""
intent.py
Lightweight reframing of the blueprint's intent_events. Original design:
website tracking pixels reporting page views/dwell time — needs a JS
snippet on a marketing site plus a persistent receiver, neither of which
this app has. Same underlying purpose (surface rising/falling buying
interest to prioritize outreach) derived instead from conversation data
already stored in the messages table — zero new infrastructure.

Four signals, each explainable in plain English:
  - reply latency:     how fast a lead responds after we message them
  - latency trend:     are they replying faster over time (warming up) or
                        slower (cooling off)?
  - engagement depth:  how many exchanges so far — more back-and-forth
                        usually means more serious evaluation
  - high-intent keywords: pricing/integration/timeline questions signal
                        real evaluation vs. a vague "tell me more"

This extends (doesn't replace) scoring.py's flat "+10 for any reply"
engagement bonus — see rescore_lead(), which keeps that baseline and adds
these weighted signals on top. Each meaningful signal is also logged to
intent_events, same audit-trail spirit as the original design, just
populated from conversation data instead of a tracking pixel.
"""
from datetime import datetime

from database import get_conn, now_iso

FAST_REPLY_SECONDS = 30 * 60  # under 30 minutes counts as a fast reply
FAST_REPLY_BONUS = 5
WARMING_TREND_BONUS = 5
DEPTH_BONUS_PER_EXCHANGE = 2
DEPTH_BONUS_CAP = 10
HIGH_INTENT_KEYWORD_BONUS = 5

HIGH_INTENT_KEYWORDS = [
    "price", "pricing", "cost", "how much", "quote",
    "integrate", "integration", "api", "setup", "implementation",
    "timeline", "when can", "how soon", "start date",
    "contract", "trial", "demo", "next steps", "move forward", "sign up",
]


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def compute_intent_signals(lead_id: int) -> dict:
    """
    Reads the lead's message thread and derives intent signals. Read-only —
    see record_intent_signals() for the version that also logs to
    intent_events.

    Returns:
        {"reply_count": int, "latencies_seconds": [float, ...],
         "avg_latency_seconds": float|None, "latest_latency_seconds": float|None,
         "trend": "warming"|"cooling"|"steady"|"insufficient_data",
         "high_intent_keywords_found": [str, ...],
         "bonus_points": int, "signal_reasons": [str, ...]}
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT direction, body, created_at FROM messages WHERE lead_id = ? ORDER BY created_at ASC",
            (lead_id,),
        ).fetchall()
    thread = [dict(r) for r in rows]

    # One latency per inbound reply: seconds since the most recent prior
    # outbound message. Each outbound message is "claimed" by its first
    # reply so a burst of replies to one message doesn't multiply-count.
    latencies = []
    last_outbound_at = None
    for msg in thread:
        if msg["direction"] == "outbound":
            last_outbound_at = msg["created_at"]
        elif msg["direction"] == "inbound" and last_outbound_at is not None:
            try:
                delta = (_parse_iso(msg["created_at"]) - _parse_iso(last_outbound_at)).total_seconds()
                if delta >= 0:
                    latencies.append(delta)
            except (ValueError, TypeError):
                pass
            last_outbound_at = None

    reply_count = sum(1 for m in thread if m["direction"] == "inbound")
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    latest_latency = latencies[-1] if latencies else None

    trend = "insufficient_data"
    if len(latencies) >= 2:
        earlier_avg = sum(latencies[:-1]) / len(latencies[:-1])
        if earlier_avg > 0 and latest_latency <= earlier_avg * 0.7:
            trend = "warming"
        elif earlier_avg > 0 and latest_latency >= earlier_avg * 1.3:
            trend = "cooling"
        else:
            trend = "steady"

    # High-intent keywords: only the MOST RECENT inbound message — this is
    # about current interest, not a lifetime tally of everything ever asked.
    most_recent_inbound = next((m for m in reversed(thread) if m["direction"] == "inbound"), None)
    keywords_found = []
    if most_recent_inbound:
        body_lower = (most_recent_inbound["body"] or "").lower()
        keywords_found = sorted({kw for kw in HIGH_INTENT_KEYWORDS if kw in body_lower})

    bonus_points = 0
    reasons = []

    if latest_latency is not None and latest_latency <= FAST_REPLY_SECONDS:
        bonus_points += FAST_REPLY_BONUS
        reasons.append(f"+{FAST_REPLY_BONUS} fast reply ({int(latest_latency // 60)} min)")

    if trend == "warming":
        bonus_points += WARMING_TREND_BONUS
        reasons.append(f"+{WARMING_TREND_BONUS} replying faster over time (warming up)")
    elif trend == "cooling":
        reasons.append("replying slower over time (cooling off) — no bonus, worth a nudge")

    if reply_count > 1:
        depth_bonus = min(DEPTH_BONUS_CAP, (reply_count - 1) * DEPTH_BONUS_PER_EXCHANGE)
        if depth_bonus > 0:
            bonus_points += depth_bonus
            reasons.append(f"+{depth_bonus} sustained engagement ({reply_count} replies)")

    if keywords_found:
        bonus_points += HIGH_INTENT_KEYWORD_BONUS
        reasons.append(f"+{HIGH_INTENT_KEYWORD_BONUS} asked about {', '.join(keywords_found[:3])} (high-intent question)")

    return {
        "reply_count": reply_count,
        "latencies_seconds": latencies,
        "avg_latency_seconds": avg_latency,
        "latest_latency_seconds": latest_latency,
        "trend": trend,
        "high_intent_keywords_found": keywords_found,
        "bonus_points": bonus_points,
        "signal_reasons": reasons,
    }


def log_intent_event(lead_id: int, event_type: str, weight: int, detail: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO intent_events (lead_id, event_type, weight, detail, captured_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, event_type, weight, detail, now_iso()),
        )


def record_intent_signals(lead_id: int) -> dict:
    """
    Computes intent signals and logs each meaningful one to intent_events
    for history/audit, then returns the same dict compute_intent_signals()
    does — so callers (scoring.rescore_lead) get bonus_points without a
    second DB round-trip.
    """
    signals = compute_intent_signals(lead_id)

    if signals["latest_latency_seconds"] is not None and signals["latest_latency_seconds"] <= FAST_REPLY_SECONDS:
        log_intent_event(lead_id, "fast_reply", FAST_REPLY_BONUS,
                          f"Replied in {int(signals['latest_latency_seconds'] // 60)} min")
    if signals["trend"] == "warming":
        log_intent_event(lead_id, "warming_trend", WARMING_TREND_BONUS, "Replying faster over time")
    elif signals["trend"] == "cooling":
        log_intent_event(lead_id, "cooling_trend", 0, "Replying slower over time")
    if signals["reply_count"] > 1:
        depth_bonus = min(DEPTH_BONUS_CAP, (signals["reply_count"] - 1) * DEPTH_BONUS_PER_EXCHANGE)
        if depth_bonus > 0:
            log_intent_event(lead_id, "engagement_depth", depth_bonus, f"{signals['reply_count']} replies so far")
    if signals["high_intent_keywords_found"]:
        log_intent_event(lead_id, "high_intent_question", HIGH_INTENT_KEYWORD_BONUS,
                          f"Asked about: {', '.join(signals['high_intent_keywords_found'])}")

    return signals


def get_intent_history(lead_id: int) -> list[dict]:
    """All logged intent events for a lead, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intent_events WHERE lead_id = ? ORDER BY captured_at DESC", (lead_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def intent_label(signals: dict) -> str:
    """Short human-facing label for dashboards/next-actions lists."""
    if signals["trend"] == "warming":
        return "🔥 Heating up"
    if signals["trend"] == "cooling":
        return "❄️ Cooling off"
    if signals["reply_count"] >= 1:
        return "➡️ Engaged"
    return "—"