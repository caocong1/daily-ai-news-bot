"""Source fetchers. Each function returns a list of dicts:
   {url, title, body, source, published_at}

   Failed sources raise; the runner catches and records stats.
"""
from . import (
    hackernews_ai,
    arxiv_ai,
    github_trending,
    huggingface_papers,
    qbitai,
    jiqizhixin,
    the_decoder,
    producthunt_ai,
    openai_blog,
    cn_36kr,
    cn_leiphone,
    cn_ithome,
    cn_tmtpost,
    cn_baidu_news,
    _sample_browser_source,
)

REGISTRY = {
    "hackernews_ai":      hackernews_ai.fetch,
    "arxiv_ai":           arxiv_ai.fetch,
    "github_trending":    github_trending.fetch,
    "huggingface_papers": huggingface_papers.fetch,
    "qbitai":             qbitai.fetch,
    "jiqizhixin":         jiqizhixin.fetch,
    "the_decoder":        the_decoder.fetch,
    "producthunt_ai":     producthunt_ai.fetch,
    "openai_blog":        openai_blog.fetch,
    "cn_36kr":            cn_36kr.fetch,
    "cn_leiphone":        cn_leiphone.fetch,
    "cn_ithome":          cn_ithome.fetch,
    "cn_tmtpost":         cn_tmtpost.fetch,
    "cn_baidu_news":      cn_baidu_news.fetch,
    "zhihu_hot":          _sample_browser_source.fetch,
}

# Sources opted into browser mode by default. zhihu_hot is kept disabled
# (browser fallback hits the login wall). baidu_news + toutiao are real
# browser-mode sources that work in our tests.
BROWSER_SOURCES = {
    "zhihu_hot",   # disabled-by-default; login wall
}

# Cap how many items a single source can dump into the queue per run.
# High-volume sources (openai_blog RSS = full feed history) get trimmed.
MAX_PER_SOURCE = {
    "openai_blog": 30,
    "arxiv_ai": 60,
    "huggingface_papers": 30,
    "github_trending": 30,
    "hackernews_ai": 50,
    "qbitai": 20,
    "the_decoder": 20,
    "jiqizhixin": 20,
    "producthunt_ai": 20,
    "cn_36kr": 25,
    "cn_leiphone": 25,
    "cn_ithome": 30,
    "cn_tmtpost": 30,
    # Primary-source caps (also used by primaries.py). OpenAI blog gives a
    # giant RSS dump; we cap aggressively to keep per-tick digestion fast.
    "openai.com": 30,
    "anthropic.com": 30,
    "blog.google": 20,
    "deepmind.google": 20,
    "ai.meta.com": 20,
    "huggingface.co": 30,
    "mistral.ai": 20,
    "cohere.com": 20,
    "arxiv.org": 60,
    "qbitai.com": 25,
    "jiqizhixin.com": 25,
}
DEFAULT_MAX_PER_SOURCE = 30


def fetch_all(enabled_sources):
    """Yield (source_name, items, err) per source. Honors fetch_mode per source:
       - 'curl' (default): use REGISTRY fn, no fallback
       - 'curl_fallback_browser': try curl first; on failure, try browser
       - 'browser': use browser-only path (skip curl)
    """
    import db as _db
    stats = _db.get_source_stats()
    for name in enabled_sources:
        fn = REGISTRY.get(name)
        if not fn:
            continue
        mode = (stats.get(name) or {}).get("fetch_mode") or "curl"
        items = None
        err = None

        if mode in ("curl", "curl_fallback_browser"):
            try:
                items = fn()
            except Exception as e:
                err = str(e)
                if mode == "curl":
                    yield name, [], err
                    continue

        if items is None and mode in ("browser", "curl_fallback_browser"):
            # Browser fallback path — but only if the source provides a
            # browser-aware fn (sources that hard-depend on browser set
            # FETCH_VIA_BROWSER = True at module top and override fetch()).
            try:
                items = _fetch_via_browser(name)
                err = None
            except Exception as e:
                err = f"browser fallback failed: {e}"

        if items is None:
            yield name, [], err or "unknown error"
            continue

        cap = MAX_PER_SOURCE.get(name, DEFAULT_MAX_PER_SOURCE)
        if len(items) > cap:
            items = items[:cap]
        yield name, items, err


def _fetch_via_browser(name):
    """Browser-mode fetch for sources that opt in by defining fetch_via_browser().

    Convention: a source module named X.py can define both `fetch()` and
    `fetch_via_browser()`; the latter is invoked when mode='browser' or
    when mode='curl_fallback_browser' and curl fails.
    """
    import importlib
    try:
        mod = importlib.import_module(f".{name}", package=__name__)
    except Exception as e:
        raise RuntimeError(f"cannot load {name}: {e}")
    if not hasattr(mod, "fetch_via_browser"):
        raise RuntimeError(f"source {name} has no fetch_via_browser()")
    return mod.fetch_via_browser()
