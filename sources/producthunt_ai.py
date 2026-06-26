"""Product Hunt — AI-tagged products via the public homepage + AI topic page."""
from ._common import fetch_url, strip_html
import re
import time
from datetime import datetime


def fetch():
    """PH's GraphQL endpoint is private; we scrape the topic landing page instead.
    Less reliable but no auth needed."""
    out = []
    urls = [
        "https://www.producthunt.com/topics/artificial-intelligence",
        "https://www.producthunt.com/topics/ai",
        "https://www.producthunt.com/topics/machine-learning",
    ]
    seen = set()
    for url in urls:
        try:
            html = fetch_url(url, timeout=20,
                             headers={"Accept-Language": "en-US,en;q=0.9"})
        except Exception:
            continue
        # PH embeds posts as anchor tags with data-test="post-url" or /posts/<slug>
        for m in re.finditer(r'href="(/posts/[^"]+)"[^>]*>\s*([^<]{4,})', html):
            slug_path = m.group(1)
            anchor_text = m.group(2).strip()
            full_url = "https://www.producthunt.com" + slug_path
            if full_url in seen:
                continue
            seen.add(full_url)
            # Also try to grab a tagline from a sibling/nearby element
            tagline = ""
            # quick: look at next 200 chars after the link for a <p>
            tail = html[m.end():m.end()+400]
            tp = re.search(r"<p[^>]*>(.*?)</p>", tail, re.DOTALL)
            if tp:
                tagline = strip_html(tp.group(1))[:200]
            out.append({
                "url": full_url,
                "title": f"🚀 Product Hunt: {anchor_text}",
                "body": tagline,
                "source": "producthunt_ai",
                "published_at": int(time.time()),
            })
            if len(out) >= 20:
                break
        if len(out) >= 20:
            break
    return out
