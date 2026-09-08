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
import streamlit as st
from dotenv import load_dotenv

from database import init_db
from leads import receive_lead, list_leads, get_lead
from qualify import qualify_lead, submit_followup_answer
from scoring import score_lead
from draft import generate_draft, get_pending_messages_for_lead, list_all_messages
from approval import approve_message, edit_and_approve_message, reject_message
from research import research_lead, get_research
from analytics import (
    compute_funnel,
    compute_qualification_rate,
    compute_score_stats,
    compute_approval_rejection_rate,
    compute_followup_completion_rate,
)

load_dotenv()
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "")

st.set_page_config(page_title="AI Sales Agent — V1", layout="wide")
init_db()

st.title("AI Sales Agent — V1 Copilot")


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


tab_new, tab_followup, tab_inbox, tab_dashboard = st.tabs(
    ["📥 New Lead", "🗨️ Needs Info", "✅ Approval Inbox", "📊 Dashboard"]
)

staff_ok = is_staff()  # computed once per run, used by the three internal tabs below


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
        generate_draft(lead_id)

    return {"status": "qualified", "score": score_result["score"], "reasons": score_result["reasons"]}


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
        else:
            lead = receive_lead(name=name, email=email, phone=phone, company=company, source="streamlit_form", message=message)

            if lead.get("is_duplicate"):
                st.info("Thanks — looks like we already have your inquiry on file. We'll be in touch!")
            else:
                try:
                    result = run_ai_pipeline(lead["id"])
                    st.success("Thanks for reaching out! We'll be in touch shortly.")
                    # Internal details (score/question) intentionally NOT shown to the public visitor.
                except Exception:
                    st.success("Thanks for reaching out! We'll be in touch shortly.")
                    # Lead is saved regardless; staff can retry processing from the Dashboard tab.


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
            with st.expander(f"{lead['name']} — {lead['company'] or 'no company'}", expanded=True):
                st.markdown(f"**Question to ask the lead:** {lead['pending_question']}")
                st.caption(f"Original message: {lead['message']}")

                answer = st.text_input("Lead's reply", key=f"followup_answer_{lead['id']}")

                if st.button("Submit answer & continue", key=f"followup_submit_{lead['id']}"):
                    if not answer.strip():
                        st.error("Enter the lead's answer first.")
                    else:
                        try:
                            qual_result = submit_followup_answer(lead["id"], answer)
                            if qual_result["status"] == "awaiting_info":
                                st.info(f"Still need more info: \"{qual_result['question']}\"")
                            else:
                                with st.spinner("Scoring and drafting..."):
                                    score_result = score_lead(lead["id"])
                                    generate_draft(lead["id"])
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

        drafted_leads = list_leads(status="drafted")

        if not drafted_leads:
            st.info("No drafts pending approval right now.")

        for lead in drafted_leads:
            pending = get_pending_messages_for_lead(lead["id"])
            if not pending:
                continue
            message = pending[0]

            with st.expander(f"{lead['name']} — {lead['company'] or 'no company'} (score: {lead['score']})", expanded=True):
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
                    if findings:
                        st.markdown("**Research findings**")
                        for f in findings[:5]:  # keep it compact
                            st.caption(f"🔍 {f['finding']}")
                            st.caption(f"[source]({f['evidence_url']}) · {f['retrieved_at'][:10]}")

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
                            run_ai_pipeline(lead["id"])
                            st.success("Processed — check above for the new draft.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed again: {e}")


# ----------------------------- TAB 4: DASHBOARD (internal) -----------------------------
with tab_dashboard:
    if not staff_ok:
        staff_login_prompt("dashboard")
    else:
        st.subheader("Analytics")

        all_leads = list_leads()
        all_messages = list_all_messages()

        if not all_leads:
            st.info("No leads yet.")
        else:
            funnel = compute_funnel(all_leads)
            qual_rate = compute_qualification_rate(all_leads)
            score_stats = compute_score_stats(all_leads)
            approval_stats = compute_approval_rejection_rate(all_messages)
            followup_stats = compute_followup_completion_rate(all_leads)

            # --- Top-line KPIs ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total leads", funnel["total"])
            col2.metric("Qualification rate", f"{qual_rate}%" if qual_rate is not None else "—")
            col3.metric("Avg. score", score_stats["average"] if score_stats["average"] is not None else "—")
            col4.metric("Sent", funnel["sent"])

            st.divider()

            # --- Funnel ---
            st.markdown("#### Funnel")
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

            # --- Honest notes on what's not measurable yet ---
            with st.expander("ℹ️ Metrics not yet available (require V2 features)"):
                st.markdown(
                    "- **Response rate** — needs a channel that captures the lead's replies "
                    "(V2's conversation memory / multi-channel engagement).\n"
                    "- **Meeting-booking rate** — needs calendar integration (explicitly a V2 feature).\n"
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