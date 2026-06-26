"""GitHub Trending — pulls trending repos, filters for AI-relevant topics.

GitHub removed the public Trending HTML feed's atom endpoint, but the HTML page
is still scrapable. We look for repo cards and parse title + description.
"""
from ._common import fetch_url, strip_html
import re
import time


AI_TOPICS = {
    "ai", "ml", "llm", "gpt", "claude", "openai", "anthropic", "langchain",
    "transformer", "diffusion", "stable-diffusion", "rag", "embedding",
    "huggingface", "ollama", "vllm", "agent", "agents", "rag", "mcp",
    "deepseek", "mistral", "gemini", "qwen", "llama", "machine-learning",
    "deep-learning", "neural", "tensor", "pytorch", "jax",
}


def fetch():
    out = []
    urls = [
        "https://github.com/trending?since=daily&spoken_language_code=en",
        "https://github.com/trending/python?since=daily&spoken_language_code=en",
        "https://github.com/trending/typescript?since=daily",
    ]
    seen = set()
    for url in urls:
        try:
            html = fetch_url(url)
        except Exception:
            continue
        # Each repo card is an <article class="Box-row"> ... </article>
        for m in re.finditer(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL):
            block = m.group(1)
            h2_m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"', block)
            if not h2_m:
                continue
            path = h2_m.group(1).strip()
            full_url = "https://github.com" + path
            if full_url in seen:
                continue
            seen.add(full_url)
            desc_m = re.search(r'<p class="col-9[^"]*">(.*?)</p>', block, re.DOTALL)
            desc = strip_html(desc_m.group(1)) if desc_m else ""
            # Filter: keep only AI-relevant
            blob = (path + " " + desc).lower()
            if not any(tok in blob for tok in AI_TOPICS):
                continue
            # Title is path "owner/repo"
            title = path.lstrip("/")
            out.append({
                "url": full_url,
                "title": f"🔥 GitHub Trending: {title}",
                "body": desc,
                "source": "github_trending",
                "published_at": int(time.time()),
            })
    return out
