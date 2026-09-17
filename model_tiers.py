"""
model_tiers.py
V4-B — Hierarchical model routing (free alternative to GPT-4o-mini/Claude
Sonnet routing): route between Groq's OWN free-tier models by task
complexity, instead of adding paid OpenAI/Anthropic API access.

IMPORTANT: llama-3.1-8b-instant (the obvious "light" choice) was
deprecated by Groq on Aug 16, 2026 and is now Enterprise-only. Their own
docs recommend openai/gpt-oss-20b as the replacement — check
https://console.groq.com/docs/models if this ever needs updating again.
"""

MODEL_LIGHT = "openai/gpt-oss-20b"
MODEL_REASONING = "openai/gpt-oss-120b"