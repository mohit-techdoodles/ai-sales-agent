"""
app.py
V1 — Streamlit UI tying together the full pipeline:
receive_lead -> qualify_lead (may pause for follow-up info) -> score_lead ->
generate_draft -> human approval -> send (real email or mocked)

The "New Lead" tab is the public-facing intake form (this app IS the website
form). The other tabs (Needs Info, Approval Inbox, Dashboard) are internal
and gated behind STAFF_PASSWORD so a public visitor can't see other leads'
data or drafts.

Run with: streamlit run app.py
Deployable free on Streamlit Community Cloud.
"""

import os
from datetime import datetime
import streamlit as st
from dotenv import load_dotenv

from database import init_db
from leads import receive_lead, list_leads, get_lead, mark_opted_out
from qualify import qualify_lead, submit_followup_answer, MAX_FOLLOWUP_ROUNDS
from scoring import score_lead
from draft import generate_draft, get_pending_messages_for_lead, list_all_messages, get_lead_ids_with_pending_messages, generate_meeting_confirmation_draft
from approval import approve_message, edit_and_approve_message, reject_message
from research import research_lead, get_research, get_research_freshness
from enrichment import get_company_enrichment
from replies import check_for_replies, get_conversation
from followup import get_leads_due_for_followup, generate_nudge_draft, FOLLOWUP_DUE_DAYS, MAX_NUDGES
import opportunities as opp
from calendar_booking import get_available_slots, book_meeting, get_meetings_for_lead, list_all_meetings
from telegram_bot import get_telegram_link, check_for_telegram_updates
from briefing import generate_briefing
from policy import should_auto_approve, get_policy, set_policy, ACTION_TYPES
from next_best_action import get_all_next_actions
from settings import get_setting, set_setting
from analytics import (
    compute_funnel,
    compute_qualification_rate,
    compute_score_stats,
    compute_approval_rejection_rate,
    compute_followup_completion_rate,
    compute_extended_funnel,
    compute_response_rate,
    compute_meeting_rate,
)

load_dotenv()
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "")

st.set_page_config(page_title="AI Sales Agent — V1", layout="wide")
init_db()

st.title("AI Sales Agent — V1 Copilot")
import re

def is_valid_phone(phone: str) -> bool:
    """Permissive international validation — allows digits + common formatting characters."""
    phone = phone.strip()
    if not phone:
        return True  # phone is optional
    digits_only = re.sub(r"[\s\-\+\(\)\.]", "", phone)
    return digits_only.isdigit() and 7 <= len(digits_only) <= 15


def is_staff() -> bool:
    """
    Simple password gate for internal tabs. Returns True if this session
    is authenticated as staff (or if no password is configured at all).
    Does NOT stop script execution — callers should skip their own tab's
    content when this returns False, since all tabs render in one pass.
    """
    if st.session_state.get("staff_authenticated"):
        return True
    if not STAFF_PASSWORD:
        return True  # unprotected fallback if no password configured
    return False


def staff_login_prompt(key_prefix: str):
    """Renders a password prompt. Call this when is_staff() is False."""
    st.info("This is an internal tab. Enter the staff password to continue.")
    entered = st.text_input("Staff password", type="password", key=f"{key_prefix}_pw")
    if st.button("Unlock", key=f"{key_prefix}_unlock"):
        if entered == STAFF_PASSWORD:
            st.session_state["staff_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")


tab_new, tab_followup, tab_inbox, tab_conversations, tab_opportunities, tab_next_actions, tab_dashboard = st.tabs(
    ["📥 New Lead", "🗨️ Needs Info", "✅ Approval Inbox", "💬 Conversations", "🎯 Opportunities", "🧭 Next Actions", "📊 Dashboard"]
)

staff_ok = is_staff()  # computed once per run, used by the three internal tabs below


def maybe_auto_approve(message_id: int, lead_id: int, action_type: str, needs_escalation: bool = False) -> bool:
    """
    V3 — checks the autonomy policy and, if this action type is cleared for
    auto-approval (and any value cap is satisfied), approves and sends the
    draft immediately. Returns True if it was auto-approved, False if it's
    waiting in the Approval Inbox as usual.

    Hard safety override: a message flagged needs_escalation NEVER auto-sends,
    no matter what the policy says — it involves a question outside approved
    knowledge and genuinely needs a human's eyes.
    """
    if needs_escalation:
        return False

    if should_auto_approve(lead_id, action_type):
        try:
            approve_message(message_id)
            return True
        except Exception as e:
            print(f"[policy] Auto-approval failed, leaving as pending for manual review: {e}")
            return False
    return False


def run_ai_pipeline(lead_id: int):
    """
    Runs research -> qualify -> (score -> draft, unless paused for follow-up info).
    Research is P1/best-effort: it never blocks or fails the pipeline, even if
    the search provider is down or rate-limited.
    Returns {"status": "awaiting_info", "question": str} or
    {"status": "qualified", "score": int, "reasons": list[str]}
    """
    with st.spinner("Researching lead..."):
        try:
            research_lead(lead_id)
        except Exception as e:
            print(f"[pipeline] Research step failed (non-blocking): {e}")

    with st.spinner("Qualifying lead..."):
        qual_result = qualify_lead(lead_id)

    if qual_result["status"] == "awaiting_info":
        return {"status": "awaiting_info", "question": qual_result["question"]}

    with st.spinner("Scoring lead..."):
        score_result = score_lead(lead_id)
    with st.spinner("Drafting outreach message..."):
        draft_result = generate_draft(lead_id)

    auto_approved = maybe_auto_approve(draft_result["message_id"], lead_id, draft_result["mode"])

    return {"status": "qualified", "score": score_result["score"], "reasons": score_result["reasons"], "auto_approved": auto_approved}


# ----------------------------- TAB 1: NEW LEAD (public) -----------------------------
with tab_new:
    st.subheader("Tell us what you're looking for")

    with st.form("new_lead_form", clear_on_submit=True):
        name = st.text_input("Name*")
        email = st.text_input("Email")
        phone = st.text_input("Phone")
        company = st.text_input("Company")
        message = st.text_area(
            "What do you need? *",
            placeholder="e.g. We need a CRM for our 10-person sales team, budget around $300/month, want it within a month.",
            height=120,
        )
        submitted = st.form_submit_button("Submit")

    if submitted:
        if not name or not message:
            st.error("Name and message are required.")
        elif not is_valid_phone(phone):
            st.error("Phone number doesn't look valid — please check it (7-15 digits, formatting characters like +, -, () are fine).")
        else:
            lead = receive_lead(name=name, email=email, phone=phone, company=company, source="streamlit_form", message=message)

            if lead.get("is_duplicate"):
                st.info("Thanks — looks like we already have your inquiry on file. We'll be in touch!")
            else:
                try:
                    result = run_ai_pipeline(lead["id"])
                    st.success("Thanks for reaching out! We'll be in touch shortly.")
                except Exception:
                    st.success("Thanks for reaching out! We'll be in touch shortly.")


# ----------------------------- TAB 2: NEEDS INFO (internal) -----------------------------
with tab_followup:
    if not staff_ok:
        staff_login_prompt("followup")
    else:
        st.subheader("Leads waiting on a follow-up answer")

        awaiting_leads = list_leads(status="awaiting_info")

        if not awaiting_leads:
            st.info("No leads currently waiting on follow-up info.")

        for lead in awaiting_leads:
            round_num = lead.get("followup_rounds", 0) or 0
            with st.expander(f"{lead['name']} — {lead['company'] or 'no company'} (question {round_num} of {MAX_FOLLOWUP_ROUNDS})", expanded=True):
                st.markdown(f"**Question to ask the lead:** {lead['pending_question']}")
                st.caption(f"Original message: {lead['message']}")

                # IMPORTANT: the key includes followup_rounds, so each new
                # question round gets a genuinely fresh, empty text box —
                # without this, Streamlit reuses the previous round's typed
                # answer since the widget key would otherwise stay identical.
                answer = st.text_input("Lead's reply", key=f"followup_answer_{lead['id']}_{round_num}")

                if st.button("Submit answer & continue", key=f"followup_submit_{lead['id']}_{round_num}"):
                    if not answer.strip():
                        st.error("Enter the lead's answer first.")
                    else:
                        try:
                            qual_result = submit_followup_answer(lead["id"], answer)
                            if qual_result["status"] == "awaiting_info":
                                st.info(f"Next question: \"{qual_result['question']}\"")
                            else:
                                with st.spinner("Scoring and drafting..."):
                                    score_result = score_lead(lead["id"])
                                    draft_result = generate_draft(lead["id"])
                                if maybe_auto_approve(draft_result["message_id"], lead["id"], draft_result["mode"], draft_result.get("needs_escalation", False)):
                                    st.success(f"Qualified! Score: {score_result['score']}/100 — auto-approved and sent per autonomy settings.")
                                else:
                                    st.success(f"Qualified! Score: {score_result['score']}/100 — draft ready in Approval Inbox.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to process answer: {e}")


# ----------------------------- TAB 3: APPROVAL INBOX (internal) -----------------------------
with tab_inbox:
    if not staff_ok:
        staff_login_prompt("inbox")
    else:
        st.subheader("Pending drafts awaiting approval")

        pending_lead_ids = get_lead_ids_with_pending_messages()
        drafted_leads = [get_lead(lid) for lid in pending_lead_ids if get_lead(lid) is not None]

        if not drafted_leads:
            st.info("No drafts pending approval right now.")

        for lead in drafted_leads:
            pending = get_pending_messages_for_lead(lead["id"])
            if not pending:
                continue
            message = pending[0]

            escalation_marker = " 🚩 NEEDS REVIEW" if message.get("needs_escalation") else ""
            with st.expander(f"{lead['name']} — {lead['company'] or 'no company'} (score: {lead['score']}){escalation_marker}", expanded=True):
                if message.get("needs_escalation"):
                    st.warning(f"⚠️ Outside approved knowledge — {message.get('escalation_reason') or 'please verify before sending'}")

                col1, col2 = st.columns([2, 1])

                with col1:
                    new_subject = st.text_input("Subject", value=message["subject"] or "", key=f"subject_{message['id']}")
                    new_body = st.text_area("Body", value=message["body"] or "", height=180, key=f"body_{message['id']}")

                with col2:
                    st.markdown("**Lead info**")
                    st.write(f"Email: {lead['email'] or '—'}")
                    st.write(f"Phone: {lead['phone'] or '—'}")
                    st.write(f"Score: {lead['score']}/100")
                    if lead.get("score_reasons"):
                        st.caption(lead["score_reasons"])

                    findings = get_research(lead["id"])
                    freshness = get_research_freshness(lead["id"])
                    if findings:
                        staleness_note = f" ⚠️ {freshness['days_old']} days old — consider refreshing" if freshness["is_stale"] else f" (updated {freshness['days_old']}d ago)"
                        st.markdown(f"**Research findings**{staleness_note}")
                        st.caption("⚠️ Free search has no identity verification — a result may be about a different person/company with a similar name. Always check the source link before referencing this in outreach.")
                        for f in findings[:5]:  # keep it compact
                            st.caption(f"🔍 {f['finding']}")
                            st.caption(f"[source]({f['evidence_url']}) · {f['retrieved_at'][:10]}")
                        if freshness["is_stale"]:
                            if st.button("🔄 Refresh research", key=f"refresh_research_{lead['id']}"):
                                try:
                                    with st.spinner("Refreshing..."):
                                        research_lead(lead["id"])
                                    st.success("Refreshed.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Couldn't refresh: {e}")

                    enrichment_data = get_company_enrichment(lead["id"])
                    if enrichment_data:
                        st.markdown("**Website enrichment**")
                        if enrichment_data.get("page_title"):
                            st.caption(f"📄 {enrichment_data['page_title']}")
                        if enrichment_data.get("tech_stack"):
                            st.caption(f"🛠️ Tech: {', '.join(enrichment_data['tech_stack'])}")

                btn_col1, btn_col2, btn_col3 = st.columns(3)

                with btn_col1:
                    if st.button("✅ Approve", key=f"approve_{message['id']}"):
                        try:
                            edited = (new_subject != message["subject"]) or (new_body != message["body"])
                            if edited:
                                edit_and_approve_message(message["id"], new_subject, new_body)
                            else:
                                approve_message(message["id"])
                            st.success("Approved and sent!")
                            st.rerun()
                        except RuntimeError as e:
                            st.error(f"Approved, but sending failed: {e}")
                            st.rerun()

                with btn_col2:
                    if st.button("✏️ Save edits & send", key=f"edit_{message['id']}"):
                        try:
                            edit_and_approve_message(message["id"], new_subject, new_body)
                            st.success("Edited and sent!")
                            st.rerun()
                        except RuntimeError as e:
                            st.error(f"Edited, but sending failed: {e}")
                            st.rerun()

                with btn_col3:
                    if st.button("❌ Reject", key=f"reject_{message['id']}"):
                        reject_message(message["id"])
                        st.warning("Rejected. Nothing was sent.")
                        st.rerun()

        stuck_leads = [
            l for l in list_leads()
            if l["status"] in ("new", "qualifying", "qualified", "send_failed") and l.get("message")
        ]
        if stuck_leads:
            st.divider()
            st.subheader("Leads needing (re)processing")
            for lead in stuck_leads:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"{lead['name']} ({lead['company'] or 'no company'}) — status: {lead['status']}")
                with col2:
                    if st.button("Run AI pipeline", key=f"retry_{lead['id']}"):
                        try:
                            retry_result = run_ai_pipeline(lead["id"])
                            if retry_result.get("auto_approved"):
                                st.success("Processed and auto-approved/sent per autonomy settings.")
                            else:
                                st.success("Processed — check above for the new draft.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed again: {e}")

        # V2 Step 4: leads who were sent something but haven't replied in a while
        due_leads = get_leads_due_for_followup()
        if due_leads:
            st.divider()
            st.subheader("📅 Follow-ups due")
            st.caption(f"No reply after {FOLLOWUP_DUE_DAYS} day(s) — generate a brief check-in (still goes through approval).")
            for lead in due_leads:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(
                        f"{lead['name']} ({lead['company'] or 'no company'}) — "
                        f"{lead['days_since_sent']} day(s) since last contact, "
                        f"{lead.get('nudge_count', 0)}/{MAX_NUDGES} nudges used"
                    )
                with col2:
                    if st.button("Draft nudge", key=f"nudge_{lead['id']}"):
                        try:
                            nudge_result = generate_nudge_draft(lead["id"])
                            if maybe_auto_approve(nudge_result["message_id"], lead["id"], nudge_result["mode"]):
                                st.success("Nudge auto-approved and sent per autonomy settings.")
                            else:
                                st.success("Nudge draft ready — see above.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Couldn't draft nudge: {e}")


# ----------------------------- TAB 4: CONVERSATIONS (internal) -----------------------------
with tab_conversations:
    if not staff_ok:
        staff_login_prompt("conversations")
    else:
        st.subheader("Conversations")
        st.caption(
            "Reply checking is on-demand (polling), not instant — click the button below "
            "to check the inbox for new replies since last time."
        )

        check_col1, check_col2 = st.columns(2)
        with check_col1:
            if st.button("🔄 Check for new email replies"):
                try:
                    with st.spinner("Checking inbox..."):
                        summary = check_for_replies()
                    st.success(
                        f"Checked {summary['checked']} email(s) — "
                        f"{summary['matched']} matched to leads "
                        f"({summary['opted_out']} opted out), "
                        f"{summary['unmatched']} from unknown senders, "
                        f"{summary['duplicates_skipped']} already seen."
                    )
                except Exception as e:
                    st.error(f"Couldn't check for replies: {e}")
        with check_col2:
            if st.button("🔄 Check Telegram"):
                try:
                    with st.spinner("Checking Telegram..."):
                        tg_summary = check_for_telegram_updates()
                    st.success(
                        f"Checked {tg_summary['checked']} update(s) — "
                        f"{tg_summary['linked']} new link(s), "
                        f"{tg_summary['matched']} message(s) matched, "
                        f"{tg_summary['unmatched']} unmatched."
                    )
                except Exception as e:
                    st.error(f"Couldn't check Telegram: {e}")

        st.divider()

        # Show leads that have any conversation activity (replied, opted out, or previously sent to)
        conversation_leads = [
            l for l in list_leads()
            if l["status"] in ("replied", "opted_out", "sent")
        ]

        if not conversation_leads:
            st.info("No conversations yet.")

        for lead in conversation_leads:
            thread = get_conversation(lead["id"])
            if not thread:
                continue

            status_badge = {"replied": "💬 Replied", "opted_out": "🚫 Opted out", "sent": "📤 Sent, no reply yet"}.get(lead["status"], lead["status"])

            with st.expander(f"{lead['name']} — {lead['company'] or 'no company'} · {status_badge}"):
                for msg in thread:
                    channel_icon = "✈️" if msg.get("channel") == "telegram" else "📧"
                    if msg["direction"] == "inbound":
                        st.markdown(f"**← {lead['name']}** {channel_icon} _{msg['created_at'][:19]}_")
                    else:
                        st.markdown(f"**→ Us** {channel_icon} _{msg['created_at'][:19]}_")
                    if msg.get("subject"):
                        st.caption(f"Subject: {msg['subject']}")
                    st.write(msg["body"])
                    st.divider()

                if lead["status"] == "replied":
                    action_col1, action_col2, action_col3 = st.columns(3)
                    with action_col1:
                        if st.button("✍️ Generate reply draft", key=f"gen_reply_{lead['id']}"):
                            try:
                                with st.spinner("Drafting a reply based on the conversation..."):
                                    reply_result = generate_draft(lead["id"])
                                if reply_result.get("needs_escalation"):
                                    st.warning("⚠️ This reply involves something outside approved knowledge — please review carefully before sending.")
                                elif maybe_auto_approve(reply_result["message_id"], lead["id"], reply_result["mode"], reply_result.get("needs_escalation", False)):
                                    st.success("Reply auto-approved and sent per autonomy settings.")
                                else:
                                    st.success("Draft ready — check the Approval Inbox to review and send.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Couldn't generate a reply: {e}")
                    with action_col2:
                        if st.button("🚫 Mark do-not-contact", key=f"optout_{lead['id']}"):
                            mark_opted_out(lead["id"], reason="Marked manually by staff")
                            st.warning("Marked as do-not-contact. No further outreach will be sent.")
                            st.rerun()
                    with action_col3:
                        if not lead.get("telegram_chat_id"):
                            if st.button("✈️ Get Telegram link", key=f"tg_link_{lead['id']}"):
                                try:
                                    link = get_telegram_link(lead["id"])
                                    st.info(f"Share this with the lead: {link}")
                                except Exception as e:
                                    st.error(f"Couldn't generate link: {e}")
                        else:
                            st.caption("✈️ Telegram linked")

                    st.divider()
                    st.markdown("**📋 Sales briefing**")
                    if st.button("Generate briefing", key=f"briefing_{lead['id']}"):
                        try:
                            with st.spinner("Compiling briefing..."):
                                briefing_text = generate_briefing(lead["id"])
                            st.session_state[f"briefing_text_{lead['id']}"] = briefing_text
                        except Exception as e:
                            st.error(f"Couldn't generate briefing: {e}")

                    briefing_key = f"briefing_text_{lead['id']}"
                    if briefing_key in st.session_state:
                        st.text_area("Briefing", value=st.session_state[briefing_key], height=300, key=f"briefing_display_{lead['id']}")

                    st.divider()
                    st.markdown("**📅 Meetings**")

                    existing_meetings = get_meetings_for_lead(lead["id"])
                    for m in existing_meetings:
                        status_icon = "✅" if m["status"] == "scheduled" else "❌"
                        st.write(f"{status_icon} {m['start_at'][:16]} — {m['status']}")
                        if m.get("meet_link"):
                            st.caption(f"[Google Meet link]({m['meet_link']})")

                    if st.button("Find available times", key=f"find_slots_{lead['id']}"):
                        try:
                            with st.spinner("Checking your calendar..."):
                                st.session_state[f"slots_{lead['id']}"] = get_available_slots(days_ahead=5)
                        except Exception as e:
                            st.error(f"Couldn't fetch availability: {e}")

                    slots_key = f"slots_{lead['id']}"
                    if slots_key in st.session_state and st.session_state[slots_key]:
                        slot_labels = {
                            f"{datetime.fromisoformat(s['start']).strftime('%a %b %d, %I:%M %p')}": s
                            for s in st.session_state[slots_key][:15]  # keep the list manageable
                        }
                        chosen_label = st.selectbox("Pick a time", list(slot_labels.keys()), key=f"slot_pick_{lead['id']}")
                        if st.button("📅 Book this meeting", key=f"book_{lead['id']}"):
                            chosen_slot = slot_labels[chosen_label]
                            try:
                                with st.spinner("Booking and sending calendar invite..."):
                                    booking_result = book_meeting(lead["id"], chosen_slot["start"], chosen_slot["end"])
                                try:
                                    conf_result = generate_meeting_confirmation_draft(
                                        lead["id"], chosen_label, booking_result.get("meet_link", "")
                                    )
                                    if maybe_auto_approve(conf_result["message_id"], lead["id"], "meeting_confirmation"):
                                        st.success("Meeting booked, invite sent, and confirmation auto-sent per autonomy settings!")
                                    else:
                                        st.success("Meeting booked, invite sent, and a confirmation message is ready in the Approval Inbox!")
                                except Exception as draft_error:
                                    st.success("Meeting booked and invite sent!")
                                    st.warning(f"(Couldn't generate a confirmation draft: {draft_error})")
                                del st.session_state[slots_key]
                                st.rerun()
                            except Exception as e:
                                st.error(f"Couldn't book meeting: {e}")

                elif lead["status"] == "opted_out":
                    st.warning("This lead has opted out. No further outreach will be sent to them.")
                elif lead["status"] == "sent":
                    if st.button("🚫 Mark do-not-contact", key=f"optout_{lead['id']}"):
                        mark_opted_out(lead["id"], reason="Marked manually by staff")
                        st.warning("Marked as do-not-contact. No further outreach will be sent.")
                        st.rerun()


# ----------------------------- TAB 5: OPPORTUNITIES (internal) -----------------------------
with tab_opportunities:
    if not staff_ok:
        staff_login_prompt("opportunities")
    else:
        st.subheader("Pipeline")

        all_opps = opp.list_opportunities()
        pipeline = opp.pipeline_value_by_stage()

        # --- Pipeline summary ---
        summary_cols = st.columns(len(opp.STAGES))
        for i, stage in enumerate(opp.STAGES):
            with summary_cols[i]:
                st.metric(stage.capitalize(), f"${pipeline['totals'][stage]:,.0f}", f"{pipeline['counts'][stage]} deal(s)")

        st.divider()

        # --- Existing opportunities: edit stage/value/probability/next action ---
        if all_opps:
            st.markdown("#### Active opportunities")
            for o in all_opps:
                with st.expander(f"{o['lead_name']} — {o['lead_company'] or 'no company'} · {o['stage']} · ${o['value'] or 0:,.0f}"):
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        new_stage = st.selectbox("Stage", opp.STAGES, index=opp.STAGES.index(o["stage"]), key=f"stage_{o['id']}")
                    with col2:
                        new_value = st.number_input("Value ($)", value=float(o["value"] or 0), step=100.0, key=f"value_{o['id']}")
                    with col3:
                        new_probability = st.slider("Probability (%)", 0, 100, o["probability"] or 0, key=f"prob_{o['id']}")

                    new_next_action = st.text_input("Next action", value=o["next_action"] or "", key=f"next_{o['id']}")

                    if st.button("Save changes", key=f"save_opp_{o['id']}"):
                        opp.update_opportunity(o["id"], stage=new_stage, value=new_value, probability=new_probability, next_action=new_next_action)
                        st.success("Updated.")
                        st.rerun()
        else:
            st.info("No opportunities yet — create one below from an active lead.")

        st.divider()

        # --- Create a new opportunity from a lead that doesn't have one yet ---
        st.markdown("#### Create opportunity from a lead")
        leads_with_opps = {o["lead_id"] for o in all_opps}
        eligible_leads = [
            l for l in list_leads()
            if l["id"] not in leads_with_opps and l["status"] not in ("rejected", "opted_out", "new")
        ]

        if not eligible_leads:
            st.caption("No eligible leads right now (needs to be past initial qualification, and not already tracked).")
        else:
            lead_options = {f"{l['name']} — {l['company'] or 'no company'} (status: {l['status']})": l["id"] for l in eligible_leads}

            with st.form("new_opportunity_form", clear_on_submit=True):
                selected_label = st.selectbox("Lead", list(lead_options.keys()))
                c1, c2, c3 = st.columns(3)
                with c1:
                    init_stage = st.selectbox("Initial stage", opp.STAGES, index=0)
                with c2:
                    init_value = st.number_input("Estimated value ($)", min_value=0.0, step=100.0)
                with c3:
                    init_probability = st.slider("Probability (%)", 0, 100, 25)
                init_next_action = st.text_input("Next action")
                create_submitted = st.form_submit_button("Create opportunity")

            if create_submitted:
                selected_lead_id = lead_options[selected_label]
                with st.spinner("Creating opportunity..."):
                    opp.create_opportunity(selected_lead_id, stage=init_stage, value=init_value, probability=init_probability, next_action=init_next_action)
                st.success("Opportunity created.")
                st.rerun()


# ----------------------------- TAB 6: NEXT ACTIONS (internal) -----------------------------
with tab_next_actions:
    if not staff_ok:
        staff_login_prompt("next actions")
    else:
        st.subheader("What needs attention right now")
        st.caption("Recommended next step per lead, based on their current state and conversation.")

        actions = get_all_next_actions()

        if not actions:
            st.info("Nothing needs action right now — everything is either waiting on a lead or done.")

        ACTION_ICONS = {
            "run_pipeline": "⚙️",
            "review_draft": "✅",
            "escalate": "🚩",
            "propose_meeting": "📅",
            "generate_reply": "✍️",
            "send_nudge": "👋",
            "retry_send": "🔁",
        }

        for lead in actions:
            action = lead["next_action"]
            icon = ACTION_ICONS.get(action, "•")
            with st.container():
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"{icon} **{lead['name']}** ({lead['company'] or 'no company'})")
                    st.caption(lead["next_action_reason"])
                with col2:
                    if action == "run_pipeline":
                        if st.button("Run pipeline", key=f"nba_run_{lead['id']}"):
                            try:
                                result = run_ai_pipeline(lead["id"])
                                st.success("Done.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
                    elif action == "generate_reply":
                        if st.button("Generate reply", key=f"nba_reply_{lead['id']}"):
                            try:
                                reply_result = generate_draft(lead["id"])
                                if not reply_result.get("needs_escalation"):
                                    maybe_auto_approve(reply_result["message_id"], lead["id"], reply_result["mode"], reply_result.get("needs_escalation", False))
                                st.success("Done — check Approval Inbox if not auto-sent.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
                    elif action == "send_nudge":
                        if st.button("Draft nudge", key=f"nba_nudge_{lead['id']}"):
                            try:
                                nudge_result = generate_nudge_draft(lead["id"])
                                maybe_auto_approve(nudge_result["message_id"], lead["id"], nudge_result["mode"])
                                st.success("Done — check Approval Inbox if not auto-sent.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
                    elif action == "retry_send":
                        st.caption("→ See 'Leads needing (re)processing' in Approval Inbox")
                    elif action == "review_draft":
                        st.caption("→ See Approval Inbox")
                    elif action == "escalate":
                        st.caption("→ See Approval Inbox (flagged 🚩)")
                    elif action == "propose_meeting":
                        st.caption("→ See Conversations tab to pick a time")
                st.divider()


# ----------------------------- TAB 7: DASHBOARD (internal) -----------------------------
with tab_dashboard:
    if not staff_ok:
        staff_login_prompt("dashboard")
    else:
        with st.expander("⚙️ Brand voice & messaging rules"):
            st.caption("Applied automatically to every AI-drafted message across all channels.")
            current_guidelines = get_setting("brand_guidelines", "")
            new_guidelines = st.text_area(
                "Guidelines",
                value=current_guidelines,
                height=100,
                placeholder="e.g. Never mention competitor names. Always sign off as 'The Gaucha Team'. Keep a warm, casual tone.",
            )
            if st.button("Save guidelines"):
                set_setting("brand_guidelines", new_guidelines)
                st.success("Saved — applies to new drafts going forward.")

        with st.expander("📚 Approved knowledge base"):
            st.caption(
                "Real facts (pricing, features, policies) the AI can confidently reference when "
                "answering a lead's questions/objections. If a question isn't covered here, the AI "
                "will flag it for your review instead of guessing."
            )
            current_knowledge = get_setting("knowledge_base", "")
            new_knowledge = st.text_area(
                "Knowledge base",
                value=current_knowledge,
                height=150,
                placeholder="e.g. Pricing: $99/month for up to 10 users, $199/month unlimited.\nIntegrates with Shopify, WooCommerce, and Stripe.\nSetup typically takes 3-5 business days.",
            )
            if st.button("Save knowledge base"):
                set_setting("knowledge_base", new_knowledge)
                st.success("Saved — applies to new drafts going forward.")

        with st.expander("⚡ FAQ cache (instant answers, no AI call)"):
            st.caption(
                "For your most common questions, add an exact pre-written answer here. "
                "When a lead's message closely matches (by meaning, not exact wording), that answer "
                "is sent directly — skipping the AI drafting call entirely, for speed and lower cost."
            )
            try:
                from semantic_cache import list_faqs, add_faq, delete_faq
                faqs = list_faqs()

                if faqs:
                    for faq in faqs:
                        with st.container():
                            col1, col2 = st.columns([4, 1])
                            with col1:
                                st.write(f"**Q:** {faq['question']}")
                                st.caption(f"A: {faq['answer']}")
                                st.caption(f"Used {faq['hit_count']} time(s)")
                            with col2:
                                if st.button("Delete", key=f"del_faq_{faq['id']}"):
                                    delete_faq(faq["id"])
                                    st.rerun()
                            st.divider()
                else:
                    st.caption("No FAQ entries yet.")

                st.markdown("**Add a new FAQ**")
                with st.form("new_faq_form", clear_on_submit=True):
                    new_q = st.text_input("Question", placeholder="What's your pricing?")
                    new_a = st.text_area("Answer", placeholder="Our pricing is $99/month for up to 10 users.")
                    faq_submitted = st.form_submit_button("Add FAQ")

                if faq_submitted:
                    if new_q.strip() and new_a.strip():
                        with st.spinner("Adding FAQ..."):
                            result = add_faq(new_q, new_a)
                        if result:
                            st.success("Added.")
                            st.rerun()
                        else:
                            st.error("Couldn't add FAQ — embedding model unavailable right now.")
                    else:
                        st.error("Both question and answer are required.")
            except ImportError:
                st.warning("FAQ caching requires the `sentence-transformers` package. Run: pip install sentence-transformers")

        with st.expander("🤖 Autonomy controls"):
            st.caption(
                "By default, EVERY message needs your approval before sending. "
                "You can allow specific action types to send automatically — optionally only "
                "below a certain deal value, so high-value opportunities always get a human look."
            )
            current_policy = get_policy()
            action_labels = {
                "initial": "Initial outreach (first contact with a new lead)",
                "followup": "Reply to a lead's message",
                "nudge": "Follow-up nudge (lead gone quiet)",
                "meeting_confirmation": "Meeting confirmation",
            }
            updated_policy = {}
            for action_type in ACTION_TYPES:
                rule = current_policy.get(action_type, {"auto_approve": False, "max_value": None})
                col1, col2 = st.columns([2, 1])
                with col1:
                    auto = st.checkbox(
                        f"Auto-send: {action_labels.get(action_type, action_type)}",
                        value=rule.get("auto_approve", False),
                        key=f"policy_auto_{action_type}",
                    )
                with col2:
                    max_val = st.number_input(
                        "Max deal value ($, blank = no limit)",
                        min_value=0.0,
                        value=float(rule["max_value"]) if rule.get("max_value") is not None else 0.0,
                        step=100.0,
                        key=f"policy_max_{action_type}",
                        disabled=not auto,
                    )
                updated_policy[action_type] = {
                    "auto_approve": auto,
                    "max_value": max_val if (auto and max_val > 0) else None,
                }

            if st.button("Save autonomy settings"):
                set_policy(updated_policy)
                st.success("Saved.")

        st.subheader("Analytics")

        all_leads = list_leads()
        all_messages = list_all_messages()
        all_meetings = list_all_meetings()
        all_opportunities = opp.list_opportunities()

        if not all_leads:
            st.info("No leads yet.")
        else:
            funnel = compute_funnel(all_leads)
            qual_rate = compute_qualification_rate(all_leads)
            score_stats = compute_score_stats(all_leads)
            approval_stats = compute_approval_rejection_rate(all_messages)
            followup_stats = compute_followup_completion_rate(all_leads)
            extended_funnel = compute_extended_funnel(all_leads, all_messages, all_meetings, all_opportunities)
            response_stats = compute_response_rate(all_messages)
            meeting_stats = compute_meeting_rate(all_messages, all_meetings)

            # --- Top-line KPIs ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total leads", funnel["total"])
            col2.metric("Qualification rate", f"{qual_rate}%" if qual_rate is not None else "—")
            col3.metric("Avg. score", score_stats["average"] if score_stats["average"] is not None else "—")
            col4.metric("Sent", funnel["sent"])

            st.divider()

            # --- V2: the exact blueprint funnel shape (Lead -> qualified -> contacted -> replied -> meeting -> opportunity) ---
            st.markdown("#### Pipeline funnel (Lead → Qualified → Contacted → Replied → Meeting → Opportunity)")
            st.bar_chart({
                "Lead": extended_funnel["lead"],
                "Qualified": extended_funnel["qualified"],
                "Contacted": extended_funnel["contacted"],
                "Replied": extended_funnel["replied"],
                "Meeting": extended_funnel["meeting"],
                "Opportunity": extended_funnel["opportunity"],
            })

            rate_col1, rate_col2 = st.columns(2)
            with rate_col1:
                st.metric(
                    "Response rate",
                    f"{response_stats['response_rate']}%" if response_stats["response_rate"] is not None else "—",
                    help=f"Of {response_stats['contacted_count']} contacted lead(s), how many replied",
                )
            with rate_col2:
                st.metric(
                    "Meeting-booking rate",
                    f"{meeting_stats['meeting_rate']}%" if meeting_stats["meeting_rate"] is not None else "—",
                    help=f"Of {meeting_stats['contacted_count']} contacted lead(s), how many got a meeting booked",
                )

            st.divider()

            # --- Status-snapshot funnel (current state, not lifetime events) ---
            st.markdown("#### Current status breakdown")
            funnel_col1, funnel_col2 = st.columns([2, 1])
            with funnel_col1:
                funnel_chart_data = {
                    "New": funnel["new"],
                    "Awaiting info": funnel["awaiting_info"],
                    "Qualifying": funnel["qualifying"],
                    "Qualified": funnel["qualified"],
                    "Drafted": funnel["drafted"],
                    "Sent": funnel["sent"],
                }
                st.bar_chart(funnel_chart_data)
            with funnel_col2:
                st.caption(f"Rejected: {funnel['rejected']}")
                st.caption(f"Send failed: {funnel['send_failed']}")

            st.divider()

            # --- Score distribution + Approval/Rejection + Follow-up completion ---
            met_col1, met_col2, met_col3 = st.columns(3)

            with met_col1:
                st.markdown("#### Score distribution")
                if score_stats["count_scored"] > 0:
                    st.bar_chart(score_stats["distribution"])
                else:
                    st.caption("No scored leads yet.")

            with met_col2:
                st.markdown("#### Approval vs rejection")
                if approval_stats["decided_count"] > 0:
                    st.write(f"Approved: **{approval_stats['approval_rate']}%**")
                    st.write(f"Rejected: **{approval_stats['rejection_rate']}%**")
                    st.caption(f"Based on {approval_stats['decided_count']} decided draft(s)")
                else:
                    st.caption("No drafts decided yet.")

            with met_col3:
                st.markdown("#### Follow-up completion")
                if followup_stats["asked_count"] > 0:
                    st.write(f"Completed: **{followup_stats['completion_rate']}%**")
                    st.caption(f"{followup_stats['asked_count']} lead(s) asked, {followup_stats['still_waiting']} still waiting")
                else:
                    st.caption("No follow-up questions asked yet.")

            st.divider()

            # --- Honest note on the one metric still genuinely blocked ---
            with st.expander("ℹ️ Metric not yet available"):
                st.markdown(
                    "- **Cost per qualified opportunity** — currently $0 since Groq's free tier is in use; "
                    "meaningful once running on a paid model where per-call cost applies."
                )

            st.divider()

            # --- Raw leads table ---
            st.markdown("#### All leads")
            display_rows = [
                {
                    "ID": l["id"],
                    "Name": l["name"],
                    "Company": l["company"],
                    "Status": l["status"],
                    "Score": l["score"],
                    "Source": l["source"],
                    "Created": l["created_at"][:19] if l["created_at"] else "",
                }
                for l in all_leads
            ]
            st.dataframe(display_rows, use_container_width=True, hide_index=True)