"""
enrichment.py
V4-A — B2B enrichment (free alternative to Apollo/Clearbit).

Apollo and Clearbit are paid B2B data providers. Instead, this module
fetches the lead's actual company website directly (we already have their
domain from their email, via the same logic used in research.py) and
extracts REAL, verifiable facts: page title, meta description, and a
lightweight tech-stack fingerprint (checking the raw HTML for known
signatures — the same principle BuiltWith/Wappalyzer use, done manually
and for free).

This can't replicate everything a paid provider gives you (employee count,
industry classification, funding stage aren't reliably extractable from a
homepage) — but what it DOES return is grounded in the company's real page,
not an LLM guess, which is the actual point of "prevent research
hallucinations" from the original blueprint.
"""

import re

import requests

from database import get_conn, log_activity, now_iso
from leads import get_lead
from research import _extract_business_domain

REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; AISalesAgentBot/1.0; +https://example.com/bot)"

# Known signatures found in raw HTML source — same fingerprinting approach
# paid tools like BuiltWith/Wappalyzer use, done manually and for free.
TECH_SIGNATURES = {
    "cdn.shopify.com": "Shopify",
    "shopifycdn.net": "Shopify",
    "wp-content": "WordPress",
    "woocommerce": "WooCommerce",
    "wixstatic.com": "Wix",
    "squarespace.com": "Squarespace",
    "cdn.bigcommerce.com": "BigCommerce",
    "js.hs-scripts.com": "HubSpot",
    "hubspot.com": "HubSpot",
    "salesforce.com": "Salesforce",
    "marketo.net": "Marketo",
    "google-analytics.com": "Google Analytics",
    "googletagmanager.com": "Google Tag Manager",
    "js.stripe.com": "Stripe",
    "cloudflare.com": "Cloudflare",
    "magento": "Magento",
    "widget.intercom.io": "Intercom",
    "zdassets.com": "Zendesk",
    "list-manage.com": "Mailchimp",
}


def _fetch_html(domain: str) -> str | None:
    """Fetches the homepage HTML for a domain. Returns None on any failure (never crashes the pipeline)."""
    for scheme in ("https://", "http://"):
        try:
            response = requests.get(
                f"{scheme}{domain}",
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200 and response.text:
                return response.text
        except Exception as e:
            print(f"[enrichment] Fetch failed for {scheme}{domain}: {e}")
    return None


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_meta_description(html: str) -> str:
    match = re.search(
        r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    # Some pages put content before name — try the reverse order too
    match = re.search(
        r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
        html, re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _detect_tech_stack(html: str) -> list[str]:
    html_lower = html.lower()
    detected = []
    for signature, tech_name in TECH_SIGNATURES.items():
        if signature.lower() in html_lower and tech_name not in detected:
            detected.append(tech_name)
    return detected


def enrich_company(lead_id: int) -> dict | None:
    """
    Fetches and analyzes the lead's company website, if their email domain
    looks like a real business (not gmail/yahoo/etc). Saves the result and
    returns it. Returns None (soft-fail) if there's no usable domain or the
    fetch fails — this is a P1 enrichment step, never a pipeline blocker.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    domain = _extract_business_domain(lead.get("email") or "")
    if not domain:
        return None

    html = _fetch_html(domain)
    if html is None:
        log_activity(lead_id, "enrichment_failed", f"Could not fetch website for domain {domain}")
        return None

    title = _extract_title(html)
    description = _extract_meta_description(html)
    tech_stack = _detect_tech_stack(html)
    timestamp = now_iso()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO company_enrichment (lead_id, domain, page_title, meta_description, tech_stack, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lead_id, domain, title, description, ",".join(tech_stack), timestamp),
        )

    log_activity(lead_id, "enrichment_completed", f"Fetched {domain}: {len(tech_stack)} tech signature(s) detected")

    return {
        "domain": domain,
        "page_title": title,
        "meta_description": description,
        "tech_stack": tech_stack,
        "fetched_at": timestamp,
    }


def get_company_enrichment(lead_id: int) -> dict | None:
    """Fetch the most recent enrichment record for a lead, if any."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_enrichment WHERE lead_id = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["tech_stack"] = result["tech_stack"].split(",") if result["tech_stack"] else []
    return result