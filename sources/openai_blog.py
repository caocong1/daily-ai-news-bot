"""OpenAI blog — RSS feed at openai.com/blog/rss.xml."""
from ._common import fetch_url, strip_html
import re
from email.utils import parsedate_to_datetime


def fetch():
    out = []
    feeds = [
        ("https://openai.com/blog/rss.xml", "openai_blog"),
        ("https://anthropic.com/news/rss.xml", "anthropic_blog"),
        ("https://blog.google/technology/ai/rss/", "google_blog_ai"),
    ]
    for url, source in feeds:
        try:
            xml = fetch_url(url, timeout=20)
        except Exception:
            continue
        items = []
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
            items.append({
                "url": link, "title": title, "body": body,
                "source": source, "published_at": pub_at,
            })
        # Sort by published_at desc; keep only recent ones (LLM will further filter)
        items.sort(key=lambda x: x.get("published_at") or 0, reverse=True)
        out.extend(items[:30])  # per-feed cap; runner will cap by source total
    return out
