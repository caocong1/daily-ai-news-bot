"""机器之心 (jiqizhixin.com) — Chinese AI news. Uses WordPress JSON API."""
from ._common import fetch_json, strip_html
import time


def fetch():
    out = []
    # WordPress REST endpoint; jiqizhixin uses a custom API but has a wp-json path
    candidates = [
        "https://www.jiqizhixin.com/api/v1/articles?page=1&limit=20",
        "https://api.jiqizhixin.com/api/v1/articles?page=1&limit=20",
        "https://www.jiqizhixin.com/wp-json/wp/v2/posts?per_page=20&orderby=date",
    ]
    data = None
    for url in candidates:
        try:
            data = fetch_json(url, headers={"Referer": "https://www.jiqizhixin.com/"})
            break
        except Exception:
            continue
    if data is None:
        return out
    # Normalize response shape — jiqizhixin custom API returns {data: [...]},
    # WordPress returns a list. Handle both.
    if isinstance(data, dict):
        records = data.get("data") or data.get("articles") or []
    else:
        records = data if isinstance(data, list) else []
    for r in records[:20]:
        # WP shape: {title:{rendered}, link, date, excerpt:{rendered}}
        # custom shape: {title, url, created_at, summary}
        title = ""
        if isinstance(r.get("title"), dict):
            title = strip_html(r["title"].get("rendered", ""))
        else:
            title = r.get("title", "")
        url = r.get("link") or r.get("url") or ""
        if not (title and url):
            continue
        excerpt = ""
        if isinstance(r.get("excerpt"), dict):
            excerpt = strip_html(r["excerpt"].get("rendered", ""))
        else:
            excerpt = r.get("summary") or r.get("excerpt") or ""
        pub_at = None
        date_str = r.get("date") or r.get("created_at") or r.get("published_at")
        if date_str:
            try:
                from datetime import datetime
                pub_at = int(datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
        out.append({
            "url": url, "title": strip_html(title), "body": excerpt[:400],
            "source": "jiqizhixin", "published_at": pub_at,
        })
    return out
