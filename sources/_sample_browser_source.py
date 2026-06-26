"""Zhihu Hot List (知乎热榜) — example of a browser-only source.

Zhihu's hot list (https://www.zhihu.com/billboard) is heavily anti-bot and
returns JS shell only via curl. Browser fetch extracts the ranked topics +
brief descriptions.
"""
from ._browser import fetch_via_browser
import re


URL = "https://www.zhihu.com/billboard"


def fetch():
    # curl path: try the obvious API
    try:
        from ._common import fetch_json
        data = fetch_json("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=30",
                          headers={"User-Agent": "Mozilla/5.0"})
        out = []
        for it in data.get("data", []):
            tgt = it.get("target", {})
            title = tgt.get("title", "")
            url = tgt.get("link", {}).get("url") or f"https://www.zhihu.com/question/{tgt.get('id','')}"
            excerpt = tgt.get("excerpt", "")
            if title:
                out.append({
                    "url": url, "title": title, "body": excerpt,
                    "source": "zhihu_hot", "published_at": None,
                })
        if out:
            return out
    except Exception:
        pass
    return []


def fetch_via_browser():
    text = fetch_via_browser(URL, wait_seconds=5, max_text_chars=20000)
    # Heuristic: zhihu billboard text is like "1 Title... brief desc"
    out = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cur = None
    for line in lines:
        # very rough: numbered or short lines are headlines
        if len(line) < 80 and re.match(r"^(\d+[\.\)、]?\s*)?[一-鿿A-Za-z0-9].*", line):
            if cur:
                out.append(cur)
            cur = {
                "url": "",  # zhihu blocks us from getting a real URL without parsing HTML
                "title": line[:120],
                "body": "",
                "source": "zhihu_hot",
                "published_at": None,
            }
        elif cur:
            cur["body"] += " " + line[:200]
            cur["body"] = cur["body"][:400]
    if cur:
        out.append(cur)
    return out[:20]
