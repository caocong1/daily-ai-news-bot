"""Hugging Face daily papers — fetched from the public HF papers endpoint.

Returns title + abstract for the day's curated AI papers.
"""
from ._common import fetch_json


def fetch():
    # HF API is sometimes slow; try alternate endpoint if first fails
    urls = [
        "https://huggingface.co/api/daily_papers",
        "https://huggingface.co/api/papers?limit=30",
    ]
    data = None
    for url in urls:
        try:
            data = fetch_json(url, timeout=25)
            if data:
                break
        except Exception:
            continue
    if not data:
        return [] 
    out = []
    for entry in data[:30]:
        paper = entry.get("paper") or entry
        pid = paper.get("id") or paper.get("paperId")
        if not pid:
            continue
        title = paper.get("title", "").strip()
        if not title:
            continue
        summary = (paper.get("summary") or paper.get("ai_summary") or "")[:600]
        url = f"https://huggingface.co/papers/{pid}"
        # publishedAt: HF API doesn't always include; fall back to now
        pub_at = paper.get("publishedAt")
        if pub_at:
            try:
                from datetime import datetime
                pub_at = int(datetime.fromisoformat(pub_at.replace("Z", "+00:00")).timestamp())
            except Exception:
                pub_at = None
        out.append({
            "url": url, "title": title, "body": summary,
            "source": "huggingface_papers", "published_at": pub_at,
        })
    return out
