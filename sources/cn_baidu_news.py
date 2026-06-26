"""Baidu News Hot List — browser-mode source.

Baidu News (https://news.baidu.com/) is heavily JS-rendered and not
reachable via curl. Browser fetch extracts the leading stories.
"""
from ._browser import fetch_via_browser
import re


URL = "https://news.baidu.com/"


def fetch():
    """Curl path — best-effort fallback. May return []."""
    try:
        from ._common import fetch_url, strip_html
        html = fetch_url(URL, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Accept-Language": "zh-CN,zh;q=0.9"})
        # Baidu sometimes returns an HTML shell; we still try to extract links
        items = []
        for m in re.finditer(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
            url = m.group(1)
            title = strip_html(m.group(2))
            if not title or len(title) < 6 or "百度" in title:
                continue
            if any(bad in url for bad in ["baidu.com/s?", "map.baidu", "image.baidu"]):
                continue
            items.append({"url": url, "title": title, "body": "",
                          "source": "cn_baidu_news", "published_at": None})
            if len(items) >= 20:
                break
        return items
    except Exception:
        return []


def fetch_via_browser():
    """Browser path — extract top stories from the rendered page."""
    text = fetch_via_browser(URL, wait_seconds=5, max_text_chars=30000)
    out = []
    # Baidu news page text is roughly: "Title  source  time  Title  source  time  ..."
    # Heuristic: lines that look like headlines (no obvious menu text)
    BAD = {"首页", "登录", "注册", "网页", "新闻", "贴吧", "知道", "音乐", "图片",
           "视频", "地图", "文库", "百度首页", "更多", "设置", "hao123"}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        if len(line) < 8 or len(line) > 120:
            continue
        if any(b in line for b in BAD):
            continue
        if any(ch in line for ch in "【】「」…—"):
            # likely a real headline
            out.append({
                "url": URL,  # baidu blocks deep linking without cookies
                "title": line[:120],
                "body": "",
                "source": "cn_baidu_news",
                "published_at": None,
            })
        elif any(kw in line for kw in ["AI", "智能", "大模型", "GPT", "DeepSeek",
                                       "芯片", "机器人", "模型", "通义", "文心"]):
            out.append({
                "url": URL,
                "title": line[:120],
                "body": "",
                "source": "cn_baidu_news",
                "published_at": None,
            })
        if len(out) >= 20:
            break
    return out
