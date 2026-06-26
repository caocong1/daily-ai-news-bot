"""36kr — Chinese tech/business news with AI section.

36kr is a major Chinese startup/tech media outlet. Their main RSS includes
some AI stories but most are general business/finance. We filter for
AI-relevant keywords to keep the signal tight.
"""
from ._common import fetch_url, strip_html
import re
import time
from email.utils import parsedate_to_datetime


# Keywords that suggest AI relevance (Chinese + English)
AI_KEYWORDS = [
    "ai", "Ai", "AI",
    "大模型", "模型", "算法", "智能", "智驾", "智算",
    "GPT", "Claude", "Llama", "Gemini", "通义", "文心", "盘古", "混元",
    "豆包", "Kimi", "智谱", "百川", "深度求索", "DeepSeek", "MiniMax",
    "具身", "机器人", "自动驾驶", "无人车",
    "AIGC", "AGI", "LLM", "ML", "机器学习", "深度学习", "神经网络",
    "Transformer", "Diffusion", "RAG", "Agent", "智能体",
    "OpenAI", "Anthropic", "NVIDIA", "英伟达", "Hugging",
]


def is_ai_relevant(title, body):
    text = (title or "") + " " + (body or "")
    for kw in AI_KEYWORDS:
        if kw in text:
            return True
    return False


def fetch():
    out = []
    try:
        xml = fetch_url("https://www.36kr.com/feed", timeout=20,
                        headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    except Exception:
        return out
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", block)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", block)
        desc_m = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
        if not (title_m and link_m):
            continue
        title = strip_html(title_m.group(1))
        link = strip_html(link_m.group(1))
        if not is_ai_relevant(title, ""):
            continue
        pub_at = None
        if pub_m:
            try:
                pub_at = int(parsedate_to_datetime(pub_m.group(1).strip()).timestamp())
            except Exception:
                pass
        body = strip_html(desc_m.group(1))[:400] if desc_m else ""
        out.append({
            "url": link, "title": title, "body": body,
            "source": "cn_36kr", "published_at": pub_at,
        })
    return out
