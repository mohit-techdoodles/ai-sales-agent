# Dashboard Configuration Guide

Recommended starting settings for the four Dashboard sections, in the order you should set them up.

## 1. Brand voice & messaging rules

This gets appended to every draft prompt across all channels, so it's worth getting right before you start relying on auto-approval. Starter template — replace the bracketed parts:

```
Tone: warm, direct, and conversational — not corporate or salesy. Write like a helpful
person, not a marketing department.

Always sign off as "[Your Name] at [Company]".
Never mention competitor names, even if the lead brings one up — redirect to our own strengths instead.
Keep emails under 150 words unless the lead has asked a detailed technical question.
Never use exclamation points more than once per email.
If a lead asks about a discount or custom pricing, do not negotiate — say a team member will follow up on that specifically.
```

The last two rules matter most early on — they're the kind of small tells that make AI-drafted email obvious. Watch the first 15-20 real drafts closely and add rules for anything that reads off-brand.

## 2. Approved knowledge base

This is the grounding mechanism — anything **not** in here, the AI escalates to a human instead of guessing. A thin knowledge base means more escalations (safe, but more manual work for you); a thorough one means fewer, so it's worth investing time here. Starter template:

```
PRICING: [tier names, prices, what's included in each]
INTEGRATIONS: [what it connects to]
SETUP TIME: [typical onboarding timeline]
SUPPORT: [hours, channels, SLA if any]
SECURITY/COMPLIANCE: [certifications, data handling — especially relevant since this app already masks PII before it reaches the LLM, so this section can safely describe your real data practices]
CANCELLATION/REFUND POLICY: [terms]
```

Update this whenever a lead asks something that gets escalated for lacking coverage — that's a live signal of what's missing.

## 3. FAQ cache

Front-load your top 5-10 anticipated questions here (pricing, "how does it work," integrations, timeline, security). These skip the LLM entirely — faster response and zero cost per hit. Good candidates are exactly the questions your knowledge base already answers well; duplicating them here just makes the common case instant.

## 4. Autonomy controls — the one requiring most care

**Start here: everything OFF.** Watch at least 15-20 real drafts per action type before automating anything. The point of V1's "human approval required" design wasn't a placeholder — it's protecting you while the brand voice and knowledge base are still being tuned.

**Once you trust the output, here's the order I'd loosen things in, from safest to riskiest:**

| Action type | Recommended setting | Why |
|---|---|---|
| `meeting_confirmation` | Auto-approve, no cap | Purely templated (time, date, link) — least room for the AI to say something wrong |
| `nudge` | Auto-approve, no cap | Low-stakes check-ins to unresponsive leads ("just following up") — high volume, low risk if imperfect |
| `initial` | Keep manual, or auto-approve only after review | First impression on a brand-new lead — worth a human glance longer than the two above |
| `followup` | Keep manual the longest | Directly answers what a real prospect asked, including objections/pricing pushback — highest-stakes category, and mistakes here are visible straight to the prospect |

**The important caveat on `max_value` caps:** they check against an `opportunities` record, which currently only gets created **manually by staff, usually after a meeting is booked**. For a brand-new lead going through `initial`/`followup` drafting, there's essentially never an opportunity yet — it's treated as $0, so any cap you set (even $50) will pass automatically regardless of the lead's real potential value. Setting a cap here gives a false sense of protection right now. Two honest options:

- Don't rely on `max_value` for `initial`/`followup` — just leave `auto_approve` off for those until you're fully confident, since the cap isn't actually gating anything yet.
- Or, if useful, I can extend `policy.py` to also support a **minimum score threshold** (e.g. "only auto-approve followups for leads scoring below 40" — i.e. automate the low-stakes ones, keep humans on the promising ones) — score exists from qualification onward, unlike opportunity value, so it'd actually be meaningful this early in the funnel. Let me know if you want that built.

## Suggested rollout timeline

- **Week 1:** Everything manual. Focus on tuning brand voice + knowledge base from what you observe.
- **Week 2, once ~20 drafts per type look right:** Auto-approve `meeting_confirmation` and `nudge`.
- **Week 3+:** Reconsider `initial`/`followup` only after deciding how to handle the `max_value` gap above — either accept it's unscoped for now, or ask for the score-threshold option.