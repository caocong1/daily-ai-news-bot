"""Hacker News — AI-tagged stories via Algolia HN Search API.

We pull two queries: 'AI' (broad) and a curated list of AI-relevant tags.
Returns up to ~30 items per run.
"""
from ._common import fetch_json
import time

QUERY_BLOCK = """
query
hits {
  objectID
  title
  url
  story_text
  created_at_i
  author
  points
  num_comments
  _tags
}
nbHits
""".strip()


def fetch():
    out = []
    seen = set()
    queries = ["AI", "LLM", "GPT", "Claude", "OpenAI", "Anthropic", "Gemini", "Mistral"]
    # Algolia returns max 1000 hits; we cap to 10 per query
    for q in queries:
        params = (
            f"query={q}"
            "&tags=story"
            "&hitsPerPage=10"
            "&numericFilters=created_at_i>{cutoff}"
        )
        cutoff = int(time.time()) - 36 * 3600  # 36h window — fetcher is permissive,
        # freshness filter is done by LLM downstream
        url = "http://hn.algolia.com/api/v1/search?" + params.format(cutoff=cutoff)
        try:
            data = fetch_json(url)
        except Exception:
            continue
        for hit in data.get("hits", []):
            oid = hit.get("objectID")
            if oid in seen:
                continue
            seen.add(oid)
            url_h = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            title = hit.get("title") or hit.get("story_text", "")[:80]
            if not title:
                continue
            body = hit.get("story_text") or ""
            out.append({
                "url": url_h,
                "title": title,
                "body": body,
                "source": "hackernews_ai",
                "published_at": hit.get("created_at_i"),
            })
    return out
