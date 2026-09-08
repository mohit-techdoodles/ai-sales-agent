"""
emailer.py
V1 Step 7 — Real email sending via SMTP.

Works with Gmail (via App Password), Outlook, or any standard SMTP provider —
no paid email service required. Controlled by SEND_REAL_EMAILS in .env so you
can safely test the approval flow without actually emailing anyone until ready.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

load_dotenv()


def send_email(to_email: str, subject: str, body: str) -> dict:
    """
    Sends a real email via SMTP if SEND_REAL_EMAILS=true and to_email is present.
    Otherwise, prints the email instead (safe dry-run mode).
    Returns {"sent": bool, "method": "smtp" | "mocked", "error": str | None}
    """
    send_real_emails = os.environ.get("SEND_REAL_EMAILS", "false").strip().lower() == "true"

    if not to_email:
        print("\n--- SEND SKIPPED (no email address on file) ---\n")
        return {"sent": False, "method": "mocked", "error": "No email address"}

    if not send_real_emails:
        print("\n--- SENDING (mocked — set SEND_REAL_EMAILS=true in .env for real delivery) ---")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
        print(f"Body:\n{body}")
        print("--- END SEND ---\n")
        return {"sent": True, "method": "mocked", "error": None}

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    from_name = os.environ.get("SMTP_FROM_NAME", "")

    if not all([host, username, password]):
        error = "SMTP credentials missing in .env — cannot send real email."
        print(f"--- SEND FAILED: {error} ---")
        return {"sent": False, "method": "smtp", "error": error}

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{from_name} <{username}>" if from_name else username
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, to_email, msg.as_string())

        print(f"--- EMAIL SENT to {to_email} ---")
        return {"sent": True, "method": "smtp", "error": None}

    except Exception as e:
        print(f"--- SEND FAILED: {e} ---")
        return {"sent": False, "method": "smtp", "error": str(e)}