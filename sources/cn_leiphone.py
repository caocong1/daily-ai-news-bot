"""Leiphone (雷锋网) — Chinese AI / robotics / smart-car focused media.

Their RSS is mostly AI-related already, so we don't filter as aggressively
as 36kr. We just take recent items.
"""
from ._common import fetch_url, strip_html
import re
import time
from email.utils import parsedate_to_datetime


def fetch():
    out = []
    try:
        xml = fetch_url("https://www.leiphone.com/feed", timeout=20,
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
            "source": "cn_leiphone", "published_at": pub_at,
        })
    # Keep only recent ones (last 7 days); leiphone's RSS is sometimes stale
    cutoff = time.time() - 7 * 86400
    out = [o for o in out if (o.get("published_at") or 0) >= cutoff]
    return out
