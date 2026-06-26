# daily-ai-news-bot

> **AI 行业新闻自动聚合 → LLM 去重 + 摘要 → QQ bot 推送（带反馈键盘）**
>
> A cron-driven pipeline that fetches AI/tech news daily, deduplicates via LLM, and pushes digests to a QQ bot with inline feedback buttons.

## 这是什么

一个**自托管的每日 AI 新闻 bot**。每 35 分钟拉一次源（HN/arxiv/8 个中文站/x.com），用 LLM 去重 + 生成摘要，把当天新内容整理成 markdown 消息推送到你的 QQ，每条带可点击的 👍/👎/评分按钮。

## 架构

```
   cron (every 35min)
       │
       ▼
  runner.py ──┬── sources/*.py ─→ fetch (HN / arxiv / x.com / cn_36kr / cn_baidu / cn_ithome / ...)
              │            │
              │            └─→ upsert to SQLite (URL unique key for dedup)
              │
              ├── pipeline.py ─→ LLM dedup + per-item digest (importance / category)
              │
              ├── formatter.py ─→ markdown + JSON footer (keyboard spec)
              │
              └── send_to_qq.py ─→ QQ Bot v2 REST API (with inline buttons)
                              │
                              └─→ 用户点 👍/👎 ─→ webhook feedback ─→ 自动评估
```

## 核心特性

- **多源聚合**：8+ 数据源（HackerNews AI / arxiv cs.AI+CL+LG+CV / x.com / 36kr / 百度新闻 / IT之家 / 量子位 / HuggingFace Papers / GitHub Trending）
- **登录态支持**：x.com / HN 需要 cookie session（用 `login.py` + `sessions/` 目录管理）
- **LLM 智能去重**：同一新闻多源覆盖时，pipeline 自动识别 + 合并（不用精确文本匹配）
- **评分反馈**：每条 digest 末尾带按钮，用户点 👍/👎 反馈到 bot，cron 自动评估 meta-eval
- **断点保护**：`runner.py | send_to_qq.py` 管道中 send_to_qq.py 提前退出时不会崩（已修复 BrokenPipeError + ValueError）
- **Cron-driven**：完全无状态，崩溃后下次 tick 自动恢复

## 依赖

- Python 3.11+
- `httpx` / `aiohttp` / `requests`
- LLM provider（OpenAI / Anthropic / 自定义 openai-compat）
- QQ Bot v2 app credentials (`QQ_APP_ID` + `QQ_CLIENT_SECRET`)

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/caocong1/daily-ai-news-bot.git
cd daily-ai-news-bot

# 2. 配置环境变量
cp .env.example .env  # 编辑填入 LLM_API_KEY + QQ_APP_ID + QQ_CLIENT_SECRET

# 3. 登录需要 session 的源
python3 login.py set-cookie x.com "<x.com cookie string>"
python3 login.py set-cookie hackernews "<hn cookie>"

# 4. 初始化数据库
python3 -c "from db import init_db; init_db()"

# 5. 单次跑测试
python3 -u runner.py | python3 send_to_qq.py

# 6. 用 cron 跑（推荐 hermes cron 或系统 crontab）
hermes cron create \
  --name "daily-ai-news-bot" \
  --script "cd /path/to/daily-ai-news-bot && python3 -u runner.py | python3 send_to_qq.py" \
  --no-agent --deliver local "every 35m"
```

## 文件结构

```
daily-ai-news-bot/
├── runner.py              # 主入口（cron 调用）
├── formatter.py           # 输出 markdown + JSON footer
├── pipeline.py            # LLM 去重 + 摘要生成
├── send_to_qq.py          # QQ Bot v2 推送
├── db.py                  # SQLite schema + helpers
├── eval.py                # 单条评分
├── meta_eval.py           # 周期性综合评估
├── prompts.py             # LLM prompt 模板
├── login.py               # 手动管理 cookie session
├── llm.py                 # LLM provider 抽象
├── warmup.py              # 启动预热
├── sources/               # 数据源 fetcher
│   ├── arxiv_ai.py
│   ├── hackernews_ai.py
│   ├── cn_36kr.py
│   ├── cn_baidu_news.py
│   ├── cn_ithome.py
│   ├── cn_leiphone.py
│   ├── cn_tmtpost.py
│   ├── github_trending.py
│   ├── huggingface_papers.py
│   ├── jiqizhixin.py
│   ├── openai_blog.py
│   ├── producthunt_ai.py
│   ├── qbitai.py
│   ├── reddit_ml.py
│   ├── the_decoder.py
│   ├── twitter_x_ai.py
│   ├── _browser.py        # Playwright-based fetcher (for JS-rendered sites)
│   ├── _common.py
│   └── x_scrape.js        # Node.js scraper for x.com
└── primaries/             # 登录态主源 fetcher
```

## License

MIT

## 致谢

源于 Hermes Agent ecosystem 的 cron 实战，参考了 anthropic/claude-for-legal 的反馈驱动优化思路。
