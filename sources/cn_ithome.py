"""IT之家 (ithome.com) — Chinese tech/AI news aggregator.

IT之家 is one of the largest Chinese tech news sites, with a dedicated AI
RSS feed at /rss?rss=ai. We pull that directly.
"""
from ._common import fetch_url, strip_html
import re
from email.utils import parsedate_to_datetime


def fetch():
    out = []
    try:
        xml = fetch_url("https://www.ithome.com/rss?rss=ai", timeout=20,
                        headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    except Exception:
        return out
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
            "source": "cn_ithome", "published_at": pub_at,
        })
    return out
