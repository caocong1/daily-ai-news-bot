"""Primary source fetcher — runs before secondary aggregators.

Reads primary_sources table and fetches each. Uses:
- curl (via _common) for RSS-based primary sources
- Chrome DevTools MCP for social/X/Reddit if a login session exists

Items ingested this way have source=primary:<domain>, so they get pulled
into dedup against the secondary aggregator coverage. If we see the same
event from both primary and secondary, we dedupe and the primary wins
(it's earlier + canonical).
"""
import time
import os
from . import db
from .sources._common import fetch_url, fetch_json, strip_html
import re
from email.utils import parsedate_to_datetime


def _fetch_rss_primary(p):
    """Fetch an RSS-based primary source. Returns items."""
    try:
        xml = fetch_url(p["url"], timeout=20,
                        headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"})
    except Exception as e:
        return [], str(e)
    items = []
    for m in re.finditer(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
        link_m = re.search(r"<link[^>]*>(.*?)</link>", block, re.DOTALL)
        if not link_m:
            link_m = re.search(r"<link[^>]*href=[\"'](.*?)[\"']", block, re.DOTALL)
        pub_m = re.search(r"<(?:pubDate|published|updated)[^>]*>(.*?)</(?:pubDate|published|updated)>",
                          block, re.DOTALL)
        desc_m = re.search(r"<description[^>]*>(.*?)</description>", block, re.DOTALL)
        if not (title_m and link_m):
            continue
        title = strip_html(title_m.group(1))
        url = strip_html(link_m.group(1))
        if not title or not url.startswith("http"):
            continue
        pub_at = None
        if pub_m:
            try:
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pass
        body = strip_html(desc_m.group(1))[:400] if desc_m else ""
        items.append({
            "url": url, "title": title, "body": body,
            "source": f"primary:{p['domain']}",
            "published_at": pub_at,
        })
    return items, None


def _fetch_atom_primary(p):
    """arXiv API (Atom format, not RSS)."""
    try:
        xml = fetch_url(p["url"], timeout=20, headers={"Accept": "application/atom+xml"})
    except Exception as e:
        return [], str(e)
    items = []
    for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        id_m = re.search(r"<id>(.*?)</id>", block)
        pub_m = re.search(r"<published>(.*?)</published>", block)
        sum_m = re.search(r"<summary>(.*?)</summary>", block, re.DOTALL)
        if not (title_m and id_m):
            continue
        title = strip_html(title_m.group(1))
        link = strip_html(id_m.group(1))
        pub_at = None
        if pub_m:
            try:
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pass
        body = strip_html(sum_m.group(1))[:600] if sum_m else ""
        items.append({
            "url": link, "title": title, "body": body,
            "source": f"primary:{p['domain']}",
            "published_at": pub_at,
        })
    return items, None


def _fetch_social_primary(p):
    """For X / Reddit accounts that need a saved session.

    Strategy:
    - Reddit: curl with cookies (JSON API, works without JS).
    - X: browser-mode via Chrome DevTools MCP. x.com is a React SPA, so
      curl returns a JS shell with no tweet text. We use a hermes sub-agent
      that drives Chrome to load the profile page and extract tweets.
    - If login_session_path is empty, skip with a note.
    """
    if not p.get("login_session_path"):
        return [], "no login session; user needs to log in first"
    try:
        import json as _json
        cookies = _json.loads(open(p["login_session_path"]).read())
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        kind = p.get("kind")
        domain = p.get("domain", "")
        if kind == "social_account" and "reddit.com" in domain:
            # Reddit: hit old.reddit.com JSON — works without JS
            data = fetch_json(p["url"] + ("&" if "?" in p["url"] else "?") + "limit=20",
                              headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"})
            items = []
            for ch in (data.get("data") or {}).get("children") or []:
                d = ch.get("data") or {}
                url = "https://www.reddit.com" + (d.get("permalink") or "")
                title = d.get("title", "")
                created = d.get("created_utc")
                pub_at = int(created) if created else None
                if not title:
                    continue
                items.append({
                    "url": url, "title": title,
                    "body": (d.get("selftext") or "")[:400],
                    "source": f"primary:{domain}",
                    "published_at": pub_at,
                })
            return items, None

        if kind == "social_account" and ("x.com" in domain or "twitter.com" in domain):
            # X: SPA, must use browser. Spawn a hermes sub-agent with the
            # profile URL; it drives Chrome to load and extract tweets.
            return _fetch_x_via_browser(p, cookie_header)

        # Generic fallback
        return [], "no parser for this social source"
    except Exception as e:
        return [], str(e)


def _fetch_x_via_browser(p, cookie_header):
    """Use Chrome DevTools MCP to load an X profile and extract recent tweets.

    Strategy: spawn a hermes sub-agent that drives Chrome to load the profile,
    wait for tweets to render, then extract text + URLs. The sub-agent is told
    to return PLAIN TEXT (one tweet per line: "URL | text") — much more robust
    than JSON over a remote LLM.
    """
    import subprocess
    profile_url = p["url"]  # e.g. https://x.com/OpenAI
    # Note: we deliberately don't put the cookie_header in the prompt — the
    # sub-agent will use its own Chrome profile. If we want to inject cookies,
    # we'd need a different transport. For now this relies on whoever drives
    # Chrome being already logged in.
    prompt = (
        f"Use Chrome DevTools MCP to navigate to: {profile_url}\n"
        f"Wait 8 seconds for tweets to render. Then extract the most recent 10 "
        f"tweets you can see.\n"
        f"Output them as PLAIN TEXT (no JSON, no markdown fences), one tweet "
        f"per line in this exact format:\n"
        f"URL | text\n"
        f"where URL is the tweet permalink (https://x.com/<user>/status/<id>) "
        f"and text is the full tweet text.\n"
        f"Skip retweets, replies, and pinned tweets unless they're the most "
        f"recent. Do not include any commentary or explanation.\n"
        f"If you cannot load the page, output the single line: ERROR: <reason>"
    )
    try:
        r = subprocess.run(
            ["hermes", "chat", "-q", prompt, "-Q", "--max-turns", "12"],
            capture_output=True, text=True, timeout=240,
        )
        if r.returncode != 0:
            return [], f"browser subagent failed: {r.stderr[:200]}"
        out = r.stdout.strip()
        # Strip session_id line
        lines = [l for l in out.split("\n") if not l.startswith("session_id:")]
        text = "\n".join(lines).strip()
        # Parse line-by-line "URL | text"
        items = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("ERROR:"):
                continue
            if " | " not in line:
                continue
            url, _, tweet_text = line.partition(" | ")
            url = url.strip()
            tweet_text = tweet_text.strip()
            if not url.startswith("https://x.com/") or "/status/" not in url:
                continue
            if not tweet_text:
                continue
            items.append({
                "url": url,
                "title": tweet_text[:120],
                "body": tweet_text[:400],
                "source": f"primary:{p['domain']}",
                "published_at": None,
            })
        return items, None
    except subprocess.TimeoutExpired:
        return [], "browser subagent timeout"
    except Exception as e:
        return [], str(e)


def _fetch_x_account_primary(p):
    """For X accounts that DON'T have a saved login session.

    Uses a Node Playwright scraper (sources/x_scrape.js) that tries
    syndication.twitter.com first, then xcancel.com. Both are
    intermittent under rate limits / Cloudflare challenges, so we
    treat zero-result as a soft error and let other sources carry the
    tick.

    Pacing: to avoid being flagged as a bot by xcancel / syndication
    (which rate-limit per IP+account), we use a per-tick rotation. Each
    tick we only fetch a subset of the no-auth X accounts, and rotate
    through them. The run_id (passed in p) determines which slice is
    active this tick.

    Kind: 'x_account_noauth'. Domain example: 'x_noauth/OpenAI' -> screen_name='OpenAI'.
    """
    import subprocess
    import json as _json
    domain = p.get("domain", "")
    # domain is like "x_noauth/OpenAI" or "x_noauth/mingchikuo" — extract screen_name
    screen_name = domain.split("/", 1)[1] if "/" in domain else domain
    if not screen_name:
        return [], "no screen_name in domain"
    # Per-tick rotation: only fetch accounts whose hash mod N == tick_hash mod N
    # (so N consecutive ticks cover all N accounts once).
    # Pull sibling x_account_noauth rows from the DB.
    siblings = db.get_primary_sources(enabled_only=True)
    x_noauth_domains = sorted(
        s["domain"] for s in siblings
        if s.get("kind") == "x_account_noauth"
    )
    n = max(1, len(x_noauth_domains))
    if n > 1:
        # Use wall-clock minute bucket so different ticks rotate naturally.
        slot = int(time.time() // 1800) % n   # 1800s = 30 min, matches cron cadence
        my_index = x_noauth_domains.index(domain) if domain in x_noauth_domains else 0
        if my_index != slot:
            # Not my turn this tick — skip quietly.
            return [], "pacing_skip"
    script = os.path.join(os.path.dirname(__file__), "sources", "x_scrape.js")
    if not os.path.exists(script):
        return [], f"x_scrape.js not found at {script}"
    try:
        r = subprocess.run(
            ["node", script, screen_name, "--limit", "20"],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            err = r.stderr.strip().splitlines()[-1] if r.stderr else f"exit {r.returncode}"
            return [], f"x_scrape failed: {err[:200]}"
        data = _json.loads(r.stdout.strip())
        tweets = data.get("tweets") or []
        if data.get("rate_limited") or data.get("challenge"):
            return [], "rate_limited_or_challenge"
        items = []
        for t in tweets:
            url = t.get("url") or ""
            if not url.startswith("https://x.com/") or "/status/" not in url:
                continue
            text = (t.get("text") or "").strip()
            if not text:
                continue
            # dedup suffix like #m
            url = url.split("#")[0]
            items.append({
                "url": url,
                "title": text[:120],
                "body": text[:600],
                "source": f"primary:{domain}",
                "published_at": t.get("date_epoch"),
            })
        return items, None
    except subprocess.TimeoutExpired:
        return [], "x_scrape timeout"
    except Exception as e:
        return [], str(e)


def fetch_all():
    """Fetch all enabled primary sources. Yields (domain, items, err)."""
    from .sources import MAX_PER_SOURCE, DEFAULT_MAX_PER_SOURCE
    primaries = db.get_primary_sources(enabled_only=True)
    for p in primaries:
        kind = p.get("kind")
        items, err = [], None
        if kind == "arxiv":
            items, err = _fetch_atom_primary(p)
        elif kind == "social_account":
            items, err = _fetch_social_primary(p)
        elif kind == "x_account_noauth":
            items, err = _fetch_x_account_primary(p)
        else:
            # default: treat as RSS blog
            items, err = _fetch_rss_primary(p)
        # Cap items per source
        cap = MAX_PER_SOURCE.get(p["domain"], DEFAULT_MAX_PER_SOURCE)
        if len(items) > cap:
            items = items[:cap]
        if not err and items:
            db.mark_primary_success(p["domain"])
        yield p["domain"], items, err