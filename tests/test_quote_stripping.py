"""
tests/test_quote_stripping.py

Regression tests for replies.py's _strip_quoted_reply(). Written after a
real bug: a bottom-posting/inline reply (quote marker first, quoted lines,
THEN the lead's new text below all of it) was returning just the "On ...
wrote:" header line — the lead's actual reply was silently dropped, so it
showed up blank in the app even though Gmail showed it fine.

Two compounding causes, both covered here:
  1. QUOTE_MARKERS required a literal "\n" before "On ... wrote:", which
     never matches when that line is the very first line of the body.
  2. Because the marker never matched, the code fell back to a stripper
     that assumes top-posting (new text, then quote) and breaks at the
     first ">" line — discarding everything after it, including
     bottom-posted new text.
"""
from replies import _strip_quoted_reply


def test_bottom_posting_reply_is_not_dropped():
    """The exact real-world case that triggered this fix: quote marker as
    the very first line, quoted block, then the lead's new text below it."""
    body = (
        "On 2026-09-21 08:55, Your Sales Team wrote:\r\n"
        "> Hi Mohit,\r\n"
        "> \r\n"
        "> Thanks for reaching out about building an AI sales agent.\r\n"
        "> \r\n"
        "> Best regards,\r\n"
        "> [Your Name]\r\n"
        "Sure, we can discuss it on a call before that. Give me an estimated \r\n"
        "price and timeline."
    )
    result = _strip_quoted_reply(body)
    assert "Sure, we can discuss it on a call" in result
    assert "price and timeline" in result
    assert "Hi Mohit" not in result
    assert "Your Sales Team wrote" not in result


def test_top_posting_reply_still_works():
    """The original, more common case: new text above the quote marker."""
    body = (
        "Sounds great, let's do Tuesday at 2pm.\n\n"
        "On Mon, Sep 21, 2026 at 9:00 AM Your Sales Team <sales@example.com> wrote:\n"
        "> Hi Mohit,\n"
        "> Would you have time for a call?\n"
        "> \n"
        "> Best,\n"
        "> Team"
    )
    result = _strip_quoted_reply(body)
    assert result == "Sounds great, let's do Tuesday at 2pm."


def test_plain_reply_with_no_quote_marker_is_untouched():
    assert _strip_quoted_reply("Sure, sounds good!") == "Sure, sounds good!"


def test_outlook_style_original_message_marker():
    body = (
        "Yes, next Tuesday works for me.\n\n"
        "-----Original Message-----\n"
        "From: Your Sales Team\n"
        "Sent: Monday, September 21, 2026\n"
        "To: Mohit\n"
        "Subject: Re: AI Sales Agent\n\n"
        "Would Tuesday work for a quick call?"
    )
    result = _strip_quoted_reply(body)
    assert result == "Yes, next Tuesday works for me."


def test_never_returns_empty_even_if_stripping_would_eat_everything():
    """If a reply is ONLY quoted content (e.g. the lead forwarded something
    with no new text), fall back to the raw body rather than returning
    nothing — a blank reply in the inbox is worse than an over-quoted one."""
    body = "> This is entirely quoted, nothing new was typed."
    result = _strip_quoted_reply(body)
    assert result  # never empty