"""X (Twitter) — public RSS bridge for AI accounts via Nitter-style mirrors.

We use the public RSSHub-like endpoints (rss.app) which proxy public tweets
without auth. If these go down, the source gracefully returns [].
"""
from ._common import fetch_url, strip_html
import re
from email.utils import parsedate_to_datetime


# A small, high-signal list of AI researchers/labs. Self-iteration can grow this.
ACCOUNTS = [
    "OpenAI", "AnthropicAI", "sama", "karpathy", "ylecun",
    "AndrewYNg", "JeffDean", "demishassabis", "DarioAmodei",
    "JimFan", "drjimfan", "arankomatsuzaki", "hardmaru",
    "sundarpichai", "satyanadella",
]


def fetch():
    out = []
    for acct in ACCOUNTS:
        url = f"https://nitter.privacydev.net/{acct}/rss"
        try:
            xml = fetch_url(url, timeout=15)
        except Exception:
            continue
        if "<rss" not in xml and "<feed" not in xml:
            continue
        for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
            block = m.group(1)
            title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
            link_m = re.search(r"<link>(.*?)</link>", block)
            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", block)
            desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
            if not (title_m and link_m):
                continue
            title = strip_html(title_m.group(1))
            link = strip_html(link_m.group(1))
            pub_at = None
            if pub_m:
                try:
                    pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
                except Exception:
                    pass
            body = strip_html(desc_m.group(1))[:400] if desc_m else ""
            out.append({
                "url": link, "title": title, "body": body,
                "source": f"x_{acct.lower()}",
                "published_at": pub_at,
            })
    return out
