"""Reddit — /r/MachineLearning, /r/LocalLLaMA, /r/singularity, /r/OpenAI.

Uses old.reddit.com JSON endpoints (.json suffix) which are stable and
require no auth for public subreddits.
"""
from ._common import fetch_json
import time


SUBREDDITS = ["MachineLearning", "LocalLLaMA", "singularity", "OpenAI", "ClaudeAI"]


def fetch():
    out = []
    seen = set()
    cutoff = int(time.time()) - 36 * 3600
    headers = {"User-Agent": "Mozilla/5.0 HermesAIBot/1.0 (research)"}
    for sub in SUBREDDITS:
        url = f"https://old.reddit.com/r/{sub}/new.json?limit=15"
        try:
            data = fetch_json(url, headers=headers)
        except Exception:
            continue
        children = (data.get("data") or {}).get("children") or []
        for ch in children:
            d = ch.get("data") or {}
            permalink = d.get("permalink")
            if not permalink:
                continue
            full_url = "https://www.reddit.com" + permalink
            if full_url in seen:
                continue
            seen.add(full_url)
            created = d.get("created_utc", 0)
            if created and created < cutoff:
                continue
            title = d.get("title", "")
            if not title:
                continue
            body = (d.get("selftext") or "")[:400]
            out.append({
                "url": full_url, "title": title, "body": body,
                "source": f"reddit_{sub.lower()}",
                "published_at": int(created) if created else None,
            })
    return out
