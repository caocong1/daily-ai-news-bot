"""Shared HTTP helpers for source fetchers. Uses curl (more reliable in WSL
than Python's urllib for some HTTPS hosts), with retries.
"""
import json
import subprocess
import re
import time
from html import unescape

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _curl(url, *, headers=None, timeout=25, retries=2):
    """Run curl, return (stdout, ok, err). ok=True on success."""
    h_args = []
    for k, v in {"User-Agent": UA, "Accept": "*/*",
                 "Accept-Language": "en-US,en;q=0.9",
                 **(headers or {})}.items():
        h_args += ["-H", f"{k}: {v}"]
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), *h_args, url]
    last_err = ""
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            continue
        if r.returncode == 0:
            return r.stdout, True, ""
        last_err = r.stderr.strip().splitlines()[-1] if r.stderr else f"exit {r.returncode}"
        if any(s in last_err for s in ("TLS", "Connection", "reset", "SSL", "EOF")):
            time.sleep(1 + attempt)
            continue
        break
    return "", False, last_err


def fetch_url(url, *, headers=None, timeout=25, as_text=True):
    out, ok, err = _curl(url, headers=headers, timeout=timeout)
    if not ok:
        raise RuntimeError(f"curl failed for {url}: {err}")
    return out


def fetch_json(url, *, headers=None, timeout=25):
    raw = fetch_url(url, headers=headers, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        i = raw.find("{")
        j = raw.rfind("}")
        if i != -1 and j > i:
            return json.loads(raw[i:j+1])
        raise


def strip_html(s: str) -> str:
    if not s:
        return ""
    # Strip CDATA wrappers but keep their content
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_rss(xml_text: str, source: str, link_field="link"):
    items = []
    for m in re.finditer(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml_text, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
        link_m = re.search(rf"<{link_field}[^>]*>(.*?)</{link_field}>", block, re.DOTALL)
        if not link_m:
            link_m = re.search(r"<link[^>]*href=[\"'](.*?)[\"']", block, re.DOTALL)
        pub_m = re.search(r"<(?:pubDate|published|updated)[^>]*>(.*?)</(?:pubDate|published|updated)>",
                          block, re.DOTALL)
        if not title_m or not link_m:
            continue
        title = strip_html(title_m.group(1))
        url = strip_html(link_m.group(1))
        if not url.startswith("http"):
            continue
        pub_at = None
        if pub_m:
            try:
                from email.utils import parsedate_to_datetime
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pub_at = None
        items.append({
            "url": url, "title": title, "body": "",
            "source": source, "published_at": pub_at,
        })
    return items
