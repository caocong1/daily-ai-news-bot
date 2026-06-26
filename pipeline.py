"""Processing pipeline: dedup + digest + freshness/AI-relevance filter.

Each step uses LLM (no regex) for the semantic judgments. Cheap structural
checks (URL exact match, simple normalization) happen first to short-circuit.
"""
import re
import time
from . import db
from . import llm


# ----------- LAYER 1: structural dedup -----------

def normalize_title(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[\W_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_structural_duplicate(new_item, recent_items):
    """Returns the existing item if duplicate by URL or same-source near-title."""
    if not recent_items:
        return None
    # Skip self-comparison (recent_items may include the item we're checking
    # if it was just inserted by the fetcher in this same run)
    pool = [r for r in recent_items if r.get("url") != new_item.get("url")]
    if not pool:
        return None
    # URL exact match (defense in depth — also filtered above, but cheap)
    for r in pool:
        if r["url"] == new_item["url"]:
            return r
    new_norm = normalize_title(new_item["title"])
    if len(new_norm) < 8:
        return None
    for r in pool:
        if r["source"] != new_item["source"]:
            continue
        if r["title"] and normalize_title(r["title"]) == new_norm:
            return r
    return None


# ----------- LAYER 2: BATCH LLM dedup -----------

def recent_window(hours=72, limit=400):
    """Items from the last N hours for cross-source dedup."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT id, url, title, body, source, published_at, status
               FROM items
               WHERE fetched_at >= ?
               ORDER BY fetched_at DESC
               LIMIT ?""",
            (int(time.time()) - hours * 3600, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def llm_dedup_batch(new_items, candidates):
    """For each new_item, ask LLM if any candidate matches.

    One LLM call returns JSON array of decisions, indexed parallel to new_items.
    Returns list aligned with new_items: matched candidate dict or None.
    """
    if not new_items:
        return []
    # Cap candidates to keep prompt manageable: prefer same-timeframe, dedup by source
    # We pull top candidates by recency; for each new item, we then ask in a batched call.
    # Even simpler: one batch call per new_item, listing up to 8 candidates.
    out = []
    others = [c for c in candidates if c["source"] not in {it["source"] for it in new_items}]
    # de-dup candidates by (source, normalized title)
    seen_keys = set()
    cand_unique = []
    for c in others:
        k = (c["source"], normalize_title(c["title"])[:60])
        if k in seen_keys:
            continue
        seen_keys.add(k)
        cand_unique.append(c)
    cand_unique = cand_unique[:30]

    # Production dedup always uses the batched template (matched to this
    # call signature). DB-stored dedup prompts are advisory — meta-eval
    # can write notes there for human review but they don't auto-activate.
    template = BATCH_DEDUP_TEMPLATE

    # Build a single shared cand_block for all items in this batch (it's
    # identical content, only the prompt per item differs in Item A).
    # Use `·` separators to avoid clashing with str.format() {key} placeholders.
    # Also escape { } in user-controlled fields so format() is safe.
    def esc(s):
        return (s or "").replace("{", "{{").replace("}", "}}")
    cand_block = "\n".join(
        f"[{i}] «{esc(c['source'])}» «{esc(c['title'][:150])}» «{esc((c.get('body') or '')[:150])}»"
        for i, c in enumerate(cand_unique)
    )
    tasks = []
    for ni in new_items:
        prompt = template.format(
            a_source=esc(ni["source"])[:80], a_title=esc(ni["title"])[:200],
            a_body=esc(ni.get("body") or "")[:250],
            candidates=cand_block,
        )
        tasks.append((ni, prompt))

    # Run all dedup calls in parallel — LLM calls are the bottleneck and
    # they're independent. Cap at 8 concurrent to avoid hammering the API.
    from concurrent.futures import ThreadPoolExecutor
    out = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_to_idx = {
            ex.submit(llm.call_llm_json, p, max_tokens=300): i
            for i, (_, p) in enumerate(tasks)
        }
        for fut in future_to_idx:
            i = future_to_idx[fut]
            ni, _ = tasks[i]
            try:
                verdict = fut.result()
            except Exception:
                verdict = None
            match = None
            if verdict:
                idx = verdict.get("match_index")
                conf = float(verdict.get("confidence", 0) or 0)
                if isinstance(idx, int) and 0 <= idx < len(cand_unique) and conf >= 0.85:
                    match = cand_unique[idx]
                else:
                    import sys as _s
                    _s.stderr.write(
                        f"[dedup] no-match for '{ni['title'][:50]}' verdict={verdict}\n")
            out[i] = match
    return out


# ----------- LAYER 3: per-item digest -----------

def llm_digest_batch(items):
    """Digest items via batched LLM calls (6 items per call) with parallel chunks.

    Uses BATCH_DIGEST_TEMPLATE (a hardcoded template matched to the batched
    call signature) rather than the DB-stored digest prompt. The DB digest
    prompt is kept for future per-item calls / human review, but production
    uses the batch shape.
    """
    out = [None] * len(items)
    template = DEFAULT_DIGEST_BATCH_TEMPLATE

    chunks = []
    for chunk_start in range(0, len(items), 6):
        chunk = items[chunk_start:chunk_start + 6]
        # IMPORTANT: use `·` separators and avoid "key=value" syntax in the
        # items block, because Python str.format() treats {key} as a placeholder
        # and would try to substitute every "source=..." literal in the block.
        # Also escape { } in user-controlled fields to keep format() safe.
        def esc(s):
            return (s or "").replace("{", "{{").replace("}", "}}")
        items_block = "\n".join(
            f"[{i}] «{esc(it['source'])}» «{esc(it['url'][:80])}» "
            f"«{esc(it['title'][:150])}» «{esc((it.get('body') or '')[:250])}»"
            for i, it in enumerate(chunk)
        )
        prompt = template.format(items=items_block)
        chunks.append((chunk_start, chunk, prompt))

    # Run chunks in parallel — typically 2-3 chunks, 2-3 concurrent LLM calls.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        future_to_start = {
            ex.submit(llm.call_llm_json, p, max_tokens=1500): start
            for start, _, p in chunks
        }
        for fut in future_to_start:
            start = future_to_start[fut]
            chunk = next(c for s, c, _ in chunks if s == start)
            try:
                verdicts = fut.result()
            except Exception:
                verdicts = None
            # Tolerate: LLM may return a single dict when an array was asked.
            # If the chunk had >1 items and we got a dict, that's a partial
            # answer — discard. If chunk was 1 item, accept the dict.
            if isinstance(verdicts, dict):
                if len(chunk) == 1:
                    verdicts = [verdicts]
                else:
                    verdicts = []
            if not isinstance(verdicts, list):
                verdicts = []
            for i, v in enumerate(verdicts):
                if not v:
                    continue
                v.setdefault("is_fresh", "stale")
                v.setdefault("ai_relevance", "none")
                v.setdefault("importance", 1)
                v.setdefault("category", "other")
                v.setdefault("headline", "")
                v.setdefault("summary", "")
                out[start + i] = v
    return out


# BATCH dedup template: one LLM call per new item, but it considers all
# candidates inlined. This is the production dedup prompt. Per-item dedup
# was an early prototype — we don't use it now.
BATCH_DEDUP_TEMPLATE = """Two news items — same underlying event?

You will compare Item A to a list of candidate items (indexed [0..N]).
If any candidate reports the same news event as Item A (same announcement,
same product launch, same paper, same funding, same incident), respond with
the index of the BEST matching candidate. If nothing matches, return null.

"Same event" means: same underlying story — different wording or different
detail level is fine, as long as it's the same fact.

IMPORTANT: most items will NOT have a match. Only return a match_index if
you are reasonably confident (>= 0.8). If unsure, return null.

Output JSON only:
{{"match_index": <int|null>, "confidence": 0.0-1.0, "reason": "one short sentence"}}

Item A: source={a_source}, title={a_title}, body={a_body}

Candidates:
{candidates}
"""


DEFAULT_DIGEST_BATCH_TEMPLATE = """You are triaging a batch of AI news items. For each,
output a JSON object (in order). Then output a JSON array of those objects.

Per item:
{{"idx": <int>, "is_fresh": "fresh"|"stale",
  "ai_relevance": "direct"|"tangential"|"none",
  "importance": 1-5,
  "category": "model_release"|"paper"|"funding"|"product"|"research"|"industry"|"open_source"|"other",
  "headline": "10-20 chars, 中文标题",
  "summary": "1-2 sentences in 中文, why it matters"}}

Rules:
- fresh = the topic/event is recent (last few days). When in doubt, mark fresh.
  Only mark stale if the title/body clearly says "in 2023" / "old paper" / "回顾".
- AI relevance: be permissive (tangential still passes); only mark "none"
  if the topic is clearly off-topic (sports scores, recipes, etc.).

- importance scoring — the user reads with discriminating eyes and likes
  DEEP content (engineering, science, methodology) the most, but also
  reads selective industry/business news when the angle is sharp. Use the
  full 1-5 range, don't bunch everything in 2-3:

    * 5  = major model release / SOTA benchmark / paradigm-shifting paper
    * 4  = engineering deep-dive: official whitepaper, system card, model
            card, post-mortem, novel architecture, or arXiv paper with a
            concrete method that practitioners can read
    * 4  = ALSO: industry/business news with a real angle — sharp analysis
            of competitive dynamics, market shifts with a thesis, or a
            high-signal executive interview. ("ZhiPu crosses ¥1T HKD" with
            what it means for AI infra; "AWS Trainium for external sale"
            with what it implies for Nvidia)
    * 3  = product feature with technical substance, open-source release
            with code, infrastructure post
    * 3  = ALSO: solid industry news — new funding round with strategic
            significance, new partnership that changes competitive position
    * 2  = routine industry news, single-product launch without technical
            novelty, partnerships with no strategic angle
    * 1  = empty hype, repetitive press release, vague "AI is changing X"
            commentary with no specific claim

  Signals that push an item UP (toward 4-5):
    - Specific technical content: parameters, benchmarks, code, architecture
    - Source is the originating lab/blog/author (not media rehash)
    - Whitepaper / PDF / system card / model card
    - Industry news that has a clear THESIS or SHARP angle (not "X raises
      money" but "X raises money BECAUSE Y, and the implication is Z")
    - Author is a known expert giving substantive analysis

  Signals that push an item DOWN (toward 1-2):
    - Vague or generic ("AI transforms X", "the future of...")
    - Media rehash of a story already covered by primary sources
    - Press release with no new information beyond the headline
    - No specific technical or strategic content

- category:
    "model_release"  = new model weights/API (GPT-N, Claude-N, Llama-N, etc.)
    "paper"          = arXiv or research-lab paper
    "research"       = broader research result / experiment
    "product"        = new product/feature/UX
    "open_source"    = open-source code release
    "funding"        = investment, valuation, acquisition
    "industry"       = business/market news with angle
    "other"          = anything else

- summary guidance:
    * Technical content: include WHAT was done and WHY it matters. Name
      the model / technique / benchmark. Avoid empty hype.
    * Industry/business news: include the SPECIFIC claim, the THESIS, and
      why it matters. Not "Company X raised money" but "Company X raised
      $Y at $Z valuation, signaling [specific thing about market]".
    * For all: avoid generic phrasing. Be specific or be quiet.

CRITICAL: respond with a JSON ARRAY only. No prose, no markdown fences.
Example response: [{{"idx":0,"is_fresh":"fresh",...}}, {{"idx":1,...}}]

The headline and summary MUST be in 中文 (Simplified Chinese), even if the source
item is in English. Use natural, concise Chinese — avoid literal translation,
paraphrase the meaning.

Items:
{items}
"""


# ----------- High-level entry point used by runner -----------

def process_pending(limit=120):
    """Process up to `limit` pending items. Three phases:
    1) structural dedup per item (cheap, no LLM)
    2) LLM cross-source dedup (batched — one call per new item, but each call
    3) LLM digest (batched — 6 items per call)
    """
    items = db.get_pending_items(limit=limit)
    if not items:
        return []
    results = []  # (item_id, decision, payload)

    # Phase 1: structural dedup
    cand_pool = recent_window(hours=72, limit=300)
    to_dedup = []  # items that survived structural dedup
    for it in items:
        dup = is_structural_duplicate(it, cand_pool)
        if dup:
            db.bump_source(it["source"], duplicate=True)
            db.mark_item(it["id"], "duplicate")
            results.append((it["id"], "duplicate", dup["id"]))
        else:
            to_dedup.append(it)

    if not to_dedup:
        return results

    # Phase 2: LLM dedup (one call per surviving item, but consolidated)
    matches = llm_dedup_batch(to_dedup, cand_pool)
    to_digest = []
    for it, match in zip(to_dedup, matches):
        if match:
            db.bump_source(it["source"], duplicate=True)
            db.mark_item(it["id"], "duplicate")
            results.append((it["id"], "duplicate", match["id"]))
        else:
            to_digest.append(it)

    if not to_digest:
        return results

    # Phase 3: digest
    digests = llm_digest_batch(to_digest)
    for it, d in zip(to_digest, digests):
        if not d:
            db.mark_item(it["id"], "error")
            results.append((it["id"], "error", None))
            continue
        if d["is_fresh"] != "fresh":
            db.mark_item(it["id"], "stale")
            results.append((it["id"], "stale", d))
            continue
        if d["ai_relevance"] == "none":
            db.mark_item(it["id"], "low_relevance")
            results.append((it["id"], "low_relevance", d))
            continue
        db.insert_digest(
            item_id=it["id"],
            headline=d["headline"] or it["title"][:60],
            summary=d["summary"],
            category=d["category"],
            importance=int(d["importance"]),
            ai_relevance=d["ai_relevance"],
            is_fresh=d["is_fresh"],
            run_id=it.get("first_seen_run", ""),
        )
        db.mark_item(it["id"], "digested")
        db.bump_source(it["source"], yielded=True)
        results.append((it["id"], "digested", d))

    return results
