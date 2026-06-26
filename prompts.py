"""Initial prompts for the digest/dedup/meta_eval pipeline.

These are seeded once; meta-eval can rewrite any of them via prompt_versions
table. Keep these terse — they go into LLM context on every run.
"""


# Batch dedup is a different prompt shape (one LLM call, N candidates inline)
# than per-item dedup. It lives in pipeline.py as a hardcoded constant, not
# in the DB — its template is closely tied to the batched call signature.


# Per-item DEDUP/DIGEST were prototypes; batch versions in pipeline.py are
# the production templates. DIGEST_V1 is still seeded to DB for legacy compatibility.
DIGEST_V1 = """You are triaging an AI news item.

For the item below, output JSON only with these fields:
{{"is_fresh": "fresh" | "stale",   "ai_relevance": "direct" | "tangential" | "none",
  "importance": 1-5,   "category": "model_release" | "paper" | "funding" | "product" | "research" | "industry" | "open_source" | "other",
  "headline": "10-20 chars concise, in user's preferred language",
  "summary": "1-2 sentences, why it matters"}}

When in doubt: be permissive about AI relevance (tangential still passes),
but strict about freshness (anything >24h = stale).

Source: {source}
URL: {url}
Title: {title}
Body: {body}
"""


# ---------- META-EVAL ----------
# Weekly-ish self-evaluation: which sources are good, which to drop, prompt tweaks.
# Now also: discover new FIRST-PARTY sources from recent digests so we read
# the official blog/tweet before media covers it.
META_EVAL_V1 = """You are tuning an AI news aggregation pipeline that runs hourly.

Below is a snapshot of the last {window_hours}h:
- Per-source performance (attempts, successes, yields)
- Last N items that were marked duplicate (with reasons)
- Last N items that were marked stale
- Sample of recent digests that were actually sent (titles, source URLs, summaries)
- The currently registered PRIMARY sources (first-party / upstream)

Decide what to change. Output JSON only:
{{"source_actions": [{{"source": "...", "action": "keep" | "downweight" | "disable", "new_weight": 0.0-1.0, "reason": "..."}}],
  "primary_source_actions": [
    {{"action": "add" | "ignore",
      "domain": "e.g. mistral.ai or x.com/AnthropicAI",
      "kind": "official_blog" | "arxiv" | "github_org" | "social_account" | "rss",
      "url": "canonical feed URL",
      "display_name": "human-readable name",
      "weight": 5.0,
      "requires_login": false,
      "reason": "why this is upstream of recent news"}}
  ],
  "prompt_notes": "free-form notes on what's misfiring",
  "new_prompts": {{"digest": "full new prompt OR null",
                    "dedup": "full new prompt OR null"}},
  "add_sources": ["..."]}}

PRIMARY-SOURCE DISCOVERY (this is the most important new task):
- For each digest in the snapshot, ask: "what's the OFFICIAL first-party
  source of this news?"
- If the digest IS already a primary source (source starts with 'primary:'),
  don't re-add it.
- Examples of what to add:
    - If digests cover Anthropic news but no anthropic.com source → add
      anthropic.com official_blog
    - If a digest quotes @karpathy saying X → add x.com/karpathy as social_account
    - If a digest references a paper → that paper's arxiv URL is already covered
- Only add sources you have HIGH confidence are upstream and reachable.
- For sources behind login (X, Reddit, Weibo, Zhihu) set requires_login=true.
- Don't add sources the snapshot already shows failing repeatedly.

Conservative thresholds:
- Disable a source only after sustained failures (>=5 consecutive).
- Don't add sources you can't actually reach from this host.

SNAPSHOT:
{snapshot}
"""
