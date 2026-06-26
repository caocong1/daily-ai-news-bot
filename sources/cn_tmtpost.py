"""TMTpost (钛媒体) — Chinese tech/business media with strong AI/AGI coverage.

Pulls the main RSS and filters by AI-relevant category or keyword.
"""
from ._common import fetch_url, strip_html
import re
from email.utils import parsedate_to_datetime


# Categories in TMTpost's RSS that mean "AI-related"
AI_CATEGORIES = {"AI", "人工智能", "大模型", "AGI", "AIGC", "智能驾驶", "机器人", "芯片"}

AI_KEYWORDS = [
    "AI", "大模型", "模型", "智能", "GPT", "Claude", "Llama", "Gemini",
    "DeepSeek", "深度求索", "通义", "文心", "盘古", "豆包", "Kimi", "智谱",
    "百川", "混元", "具身", "机器人", "自动驾驶", "AIGC", "AGI", "LLM",
    "OpenAI", "Anthropic", "NVIDIA", "英伟达", "芯片",
]


def is_ai_relevant(title, cats, body):
    for c in cats:
        if c in AI_CATEGORIES:
            return True
    text = (title or "") + " " + (body or "")
    for kw in AI_KEYWORDS:
        if kw in text:
            return True
    return False


def fetch():
    out = []
    try:
        xml = fetch_url("https://www.tmtpost.com/rss.xml", timeout=20,
                        headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    except Exception:
        return out
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", block)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", block)
        desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
        cats = [c.strip() for c in re.findall(r"<category>(.*?)</category>", block)]
        if not (title_m and link_m):
            continue
        title = strip_html(title_m.group(1))
        link = strip_html(link_m.group(1))
        body = strip_html(desc_m.group(1))[:400] if desc_m else ""
        if not is_ai_relevant(title, cats, body):
            continue
        pub_at = None
        if pub_m:
            try:
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pass
        out.append({
            "url": link, "title": title, "body": body,
            "source": "cn_tmtpost", "published_at": pub_at,
        })
    return out
