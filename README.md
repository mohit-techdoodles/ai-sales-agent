# AI Sales Agent — Project Summary & Handoff

Covers everything built against the original `AI_Sales_Agent_MVP_V1_V2_V3_Blueprint.pdf`, including the V4/V5/V6 phases added after the original blueprint. Written as a reference for future you (or anyone else who picks this up).

## What this is

A Streamlit app that takes inbound leads, qualifies them, scores them, drafts outreach for human approval, follows up automatically, books meetings, and — as of V6 — masks PII before it reaches any third-party LLM and reconciles sales calls against what was expected going in.

## Architecture at a glance

| Layer | What's actually used | Why (vs. the original blueprint) |
|---|---|---|
| Backend | Python + Streamlit (not FastAPI) | Streamlit was the actual constraint from day one — free-tier hosting, no separate backend service |
| Database | **Turso (libSQL)**, SQLite dialect | Blueprint said PostgreSQL; Turso keeps the SQLite dialect the app was already written in, has a generous free tier, and — critically — solves the actual problem that started this: Streamlit Community Cloud's filesystem is ephemeral, so a local `sales_agent.db` file gets wiped on every reboot/redeploy |
| LLM | Groq (`gpt-oss-120b` family) | Free/cheap tier, fast inference |
| Vector/semantic cache | Local `sentence-transformers` embeddings + plain SQLite, manual cosine similarity | Blueprint said Redis + pgvector; this avoids paying for Redis or Postgres just for a semantic cache |
| PII masking | Custom regex + Luhn check (`pii_guard.py`) | Blueprint said Presidio (spaCy + NLP models) — too heavy for a free-tier deployment already running an embeddings model |
| Company enrichment | Free website-scraping fallback (`enrichment.py`) | Blueprint said Apollo/Clearbit (paid APIs) |

**Running theme:** almost every deviation from the original blueprint exists because the actual deployment target is a *free-tier Streamlit app*, not an enterprise stack with Postgres/Redis/paid APIs available. Where the blueprint assumed infrastructure that costs money, we found a free equivalent that keeps the same behavior.

## Version-by-version summary

### V1 — Inbound Lead Qualification Copilot
Core pipeline: capture → qualify → score → draft → human approval → send → CRM log.
Key files: `leads.py`, `qualify.py`, `scoring.py`, `draft.py`, `approval.py`, `database.py`.

### V2 — Multi-Channel Sales Engagement
Email + Telegram, conversation memory, automated follow-up, calendar booking.
Key files: `followup.py`, `calendar_booking.py`, `telegram_bot.py`, `replies.py`, `opportunities.py`.

### V3 — AI Sales Employee
Dynamic qualification, objection handling via knowledge base, next-best-action routing, autonomy policy engine, sales briefings.
Key files: `next_best_action.py`, `policy.py`, `briefing.py`, plus the regression test suite (`tests/`) covering qualification, grounding, routing, and policy compliance.

### V4 — Data Intelligence & Cost Optimization
- **V4-A** — free website-scraping enrichment instead of Apollo/Clearbit → `enrichment.py`
- **V4-B** — hierarchical model routing (light model for simple tasks, reasoning model for objection handling) + local semantic FAQ cache → `model_tiers.py`, `semantic_cache.py`

### V5 — Multimodal & Omnichannel
- **V5-A** — document/RFP parsing on the New Lead form → `document_parser.py`
- **V5-B** — voice note transcription on the New Lead form → `voice.py`
- Extended to the **Approval Inbox**: staff can attach a document or record a voice reply mid-pipeline, which supersedes the pending draft (never rejects the lead), re-runs qualification/scoring, and generates a fresh draft → `attachments.py`
- That attachment now also **rides along on the outgoing email** when approved, not just informing the draft → `emailer.py`, `approval.py`

### V6 — Enterprise Security & Closed-Loop Feedback
- **V6-A** — PII masking (regex + Luhn, not Presidio) and prompt-injection screening on every LLM call, logged to a `security_audit` table → `pii_guard.py`, `security_log.py`
- **V6-B** — post-call reconciliation, **manual-entry version**: staff paste a transcript, it's compared against the persisted pre-call briefing, discrepancies are surfaced, and the score is recomputed by the same deterministic `scoring.py` logic used everywhere else (the LLM flags what changed; it never sets the score directly) → `call_reconciliation.py`, plus a new `briefings` table so briefings persist instead of vanishing from `st.session_state`

**Why V6-B is manual, not webhook-driven:** Recall.ai/Gong deliver transcripts via webhook, which needs a persistent, always-listening HTTP endpoint. Streamlit Community Cloud can't host one. Swapping in a real provider later only means changing how `add_and_reconcile_transcript()` gets called — the comparison/scoring logic underneath doesn't change.

### Deferred: `intent_events` (web visitor intent tracking)
The last table from the blueprint's "PostgreSQL Schema Additions" section. Tracks first-party website signals (page views, dwell time) to score buying intent before someone becomes a lead. Explicitly parked — bigger scope than V6, and hits the same "needs a persistent receiver" wall as the V6-B webhook, except there's no manual-entry workaround for a high-volume, real-time tracking pixel the way there was for a single call transcript.

## Bug fixed along the way

**Quoted-reply stripping (`replies.py`)** — bottom-posting/inline email replies (quote marker first, then the quoted block, then the lead's new text *below* it) were being reduced to just the `"On ... wrote:"` header line, silently dropping the lead's actual reply. Fixed to handle both top-posting and bottom-posting styles. `fix_broken_reply.py` is a one-off script to repair any leads whose data was corrupted by this before the fix — re-fetches the original email, re-parses it, and re-runs qualification/scoring/drafting on the corrected text.

## Environment variables

| Variable | Used for |
|---|---|
| `GROQ_API_KEY` | All LLM calls |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | Database (falls back to local SQLite if unset — dev only) |
| `SEND_REAL_EMAILS` | `true`/`false` — dry-run (prints) vs. actually sending |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_NAME` | Outbound email (Gmail: use an App Password) |
| `IMAP_HOST`, `IMAP_PORT` | Reading replies (reuses `SMTP_USERNAME`/`SMTP_PASSWORD`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | Telegram channel |
| `GOOGLE_TOKEN_JSON` | Calendar booking |
| `FOLLOWUP_DUE_DAYS`, `FOLLOWUP_MAX_ROUNDS` | Follow-up cadence tuning |
| `RESEARCH_STALE_DAYS` | Research freshness threshold |
| `SEMANTIC_CACHE_THRESHOLD` | FAQ cache similarity cutoff |
| `BUSINESS_START_HOUR`, `BUSINESS_END_HOUR`, `BUSINESS_TIMEZONE` | Meeting-booking hours |
| `STAFF_PASSWORD` | App access gate |

## Running it

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Testing

```bash
pip install -r requirements-dev.txt   # adds pytest
pytest tests/ -v
```

76 tests as of today, covering qualification, grounding/anti-hallucination, routing, policy compliance, document/voice attachments (including that they actually get sent, not just stored), quoted-reply stripping, PII masking + LLM wiring, and call reconciliation. Every test runs against a throwaway local SQLite file — none of them touch the real Turso database.

**Housekeeping:** `test_step2.py` through `test_step6.py` in the project root are pre-regression-suite smoke scripts and are stale (at least one no longer matches the current function signatures) — safe to delete now that `tests/` covers the same ground properly.

## One-off scripts (not part of the app)

- `debug_raw_reply.py` — prints the raw vs. cleaned body of the latest email from a sender, for diagnosing reply-parsing issues
- `fix_broken_reply.py` — repairs a lead whose reply was corrupted by the quote-stripping bug above
- `migrate_add_attachments.py` — one-time migration for databases created before attachment columns were added to `database.py`'s schema directly (new databases don't need it)