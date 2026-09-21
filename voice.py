"""
voice.py
V5-B (lite) — Turn-based voice message transcription.

The original blueprint's V5-B calls for a real-time phone voice agent
(WebRTC/SIP, <800ms latency, Twilio/LiveKit/Vapi) — that requires paid
telephony and a separate always-on service; Streamlit's request/response
model fundamentally cannot host a live call. That version is genuinely
out of scope for a free build.

This is the realistic free alternative: a lead (or staff) records a voice
message, we transcribe it with Groq's free Whisper API, and the transcript
feeds into the EXISTING qualification/drafting pipeline — no new AI logic
needed, no ongoing telephony cost, works within Streamlit's normal
request/response model. Not a live call — a "voice note" exchange.

Groq's free tier for Whisper: 2,000 requests/day, 25MB file size limit,
confirmed via https://console.groq.com/docs/speech-to-text.
"""

import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL = "whisper-large-v3-turbo"  # fast + free; use whisper-large-v3 instead if accuracy matters more than speed
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # Groq's free-tier limit


def transcribe_audio(audio_bytes: bytes, filename: str = "recording.wav") -> dict:
    """
    Transcribes a voice recording via Groq's free Whisper API.
    Returns {"text": str, "error": str|None}. Never raises — fails soft,
    consistent with every other enrichment step in this app.
    """
    if not audio_bytes:
        return {"text": "", "error": "No audio data provided."}

    if len(audio_bytes) > MAX_FILE_SIZE_BYTES:
        return {"text": "", "error": f"Audio file too large ({len(audio_bytes) / 1_000_000:.1f}MB — max 25MB on the free tier)."}

    try:
        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        response = client.audio.transcriptions.create(
            model=MODEL,
            file=(filename, audio_bytes),
            response_format="json",
        )
        return {"text": response.text.strip(), "error": None}
    except Exception as e:
        print(f"[voice] Transcription failed: {e}")
        return {"text": "", "error": str(e)}