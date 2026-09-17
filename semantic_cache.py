"""
semantic_cache.py
V4-B — Semantic caching (free alternative to Redis + paid embeddings API).

Original doc: "Query Redis vector storage using cosine similarity before
invoking an LLM. If similarity >= 0.95 on standard product FAQs, return
the cached answer immediately."

Free version: local embeddings via sentence-transformers (no API cost, one-
time model download, runs on CPU) instead of a paid embedding API, and the
existing SQLite/Turso database instead of a separate Redis instance.

Staff maintain a list of FAQ question/answer pairs (via the Dashboard).
When a lead's message closely matches a stored FAQ question (by meaning,
not exact wording), we can skip the reasoning-tier LLM call for objection
handling entirely and use the pre-approved answer directly — genuinely
reducing token overhead, same as the original design's intent.

Fails soft throughout: if the embedding model isn't available for any
reason (not installed, network issue downloading it, etc.), caching is
silently skipped and the normal LLM-drafting flow is used instead — this
is a cost-optimization layer, never a pipeline requirement.
"""

import json
import math
import os

from database import get_conn, now_iso

SEMANTIC_CACHE_THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.55"))

_model = None  # lazy-loaded singleton — loading is slow, do it once per process


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed(text: str) -> list[float] | None:
    """Returns the embedding vector for a piece of text, or None if the model isn't available."""
    try:
        model = _get_model()
        vector = model.encode(text)
        return vector.tolist()
    except Exception as e:
        print(f"[semantic_cache] Embedding failed (caching disabled for this call): {e}")
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def add_faq(question: str, answer: str) -> dict | None:
    """Adds a staff-approved FAQ pair, embedding the question for future matching."""
    vector = embed(question)
    if vector is None:
        return None  # embedding unavailable — don't store an unsearchable entry

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO faq_entries (question, answer, embedding, hit_count, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (question, answer, json.dumps(vector), timestamp, timestamp),
        )
        faq_id = cursor.lastrowid

    return get_faq(faq_id)


def update_faq(faq_id: int, question: str = None, answer: str = None) -> dict | None:
    """Updates a FAQ entry. Re-embeds if the question text changed."""
    existing = get_faq(faq_id)
    if existing is None:
        return None

    final_question = question if question is not None else existing["question"]
    final_answer = answer if answer is not None else existing["answer"]
    timestamp = now_iso()

    if question is not None and question != existing["question"]:
        vector = embed(final_question)
        embedding_json = json.dumps(vector) if vector else existing["embedding"]
    else:
        embedding_json = existing["embedding"]

    with get_conn() as conn:
        conn.execute(
            "UPDATE faq_entries SET question = ?, answer = ?, embedding = ?, updated_at = ? WHERE id = ?",
            (final_question, final_answer, embedding_json, timestamp, faq_id),
        )

    return get_faq(faq_id)


def delete_faq(faq_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM faq_entries WHERE id = ?", (faq_id,))


def get_faq(faq_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM faq_entries WHERE id = ?", (faq_id,)).fetchone()
        return dict(row) if row else None


def list_faqs() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM faq_entries ORDER BY hit_count DESC, created_at DESC").fetchall()
        return [dict(r) for r in rows]


def find_matching_faq(query_text: str, threshold: float = None) -> dict | None:
    """
    Embeds query_text and compares it against every stored FAQ question by
    cosine similarity. Returns the best match if it clears the threshold
    (default SEMANTIC_CACHE_THRESHOLD), incrementing its hit_count and
    updating last_accessed_at. Returns None if nothing matches well enough,
    there are no FAQs yet, or embedding fails for any reason (fails soft —
    the caller should fall back to normal LLM drafting in that case).
    """
    if threshold is None:
        threshold = SEMANTIC_CACHE_THRESHOLD

    query_vector = embed(query_text)
    if query_vector is None:
        return None

    faqs = list_faqs()
    if not faqs:
        return None

    best_match = None
    best_score = -1.0
    for faq in faqs:
        try:
            faq_vector = json.loads(faq["embedding"])
        except (json.JSONDecodeError, TypeError):
            continue
        score = cosine_similarity(query_vector, faq_vector)
        if score > best_score:
            best_score = score
            best_match = faq

    if best_match is None or best_score < threshold:
        return None

    timestamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE faq_entries SET hit_count = hit_count + 1, last_accessed_at = ? WHERE id = ?",
            (timestamp, best_match["id"]),
        )

    return {**best_match, "similarity": round(best_score, 3)}