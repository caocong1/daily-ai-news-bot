"""Browser-mode fetcher — uses Chrome DevTools MCP to fetch pages that
curl can't reach (Cloudflare, JS-rendered, anti-bot).

This module is invoked when source_stats.fetch_mode is 'browser' or
'curl_fallback_browser' and the curl attempt fails.

Strategy:
- Open a Chrome tab via MCP
- Navigate to the URL
- Wait for page load (configurable)
- Extract readable text + links via the DOM snapshot or text dump

The MCP calls are routed through a hermes subprocess (`hermes chat`) that
issues browser tool calls. We capture the text content from the model
response and return it like a normal HTTP fetch result.
"""
import subprocess
import json
import time


def fetch_via_browser(url, *, wait_seconds=4, max_text_chars=50000):
    """Use Chrome DevTools MCP (via Hermes sub-agent) to fetch URL.

    Returns a string (HTML or extracted text). Raises on failure.
    """
    prompt = (
        f"Use the chrome-devtools-mcp browser tools to navigate to: {url}\n"
        f"After the page loads, wait {wait_seconds}s for any JS to render, "
        f"then extract the main text content of the page.\n"
        f"Respond with ONLY the extracted text (no commentary, no markdown fences).\n"
        f"Cap the output at {max_text_chars} characters."
    )
    try:
        r = subprocess.run(
            ["hermes", "chat", "-q", prompt, "-Q", "--max-turns", "8"],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("browser fetch timeout")
    if r.returncode != 0:
        raise RuntimeError(f"hermes chat failed: {r.stderr[:200]}")
    out = r.stdout.strip()
    # Strip session_id line
    lines = [l for l in out.split("\n") if not l.startswith("session_id:")]
    text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("browser returned empty text")
    return text


def parse_rss_like_text(text, source_name, url_field_extractor=None):
    """Best-effort extract news items from a text dump when no RSS is available.

    Heuristic: split on double newlines, treat each block as a potential item,
    pick blocks that contain a date-ish substring or look like a headline.
    """
    # For now, return a single mega-item so the pipeline still digests something.
    # Subclasses or callers can override with smarter extraction.
    return [{
        "url": url_field_extractor or "",
        "title": text.split("\n", 1)[0][:200] if text else source_name,
        "body": text[:600],
        "source": source_name,
        "published_at": None,
    }]