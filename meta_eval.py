"""Meta-evaluation: review recent pipeline performance, let LLM propose
adjustments to source weights, prompt versions, and source additions.

Run this on a slower cadence (every few hours) than the main pipeline.
"""
import json
import time
from . import db
from . import llm
from . import prompts


def build_snapshot(window_hours=24):
    stats = db.get_source_stats()
    # Trim to useful fields
    sources = []
    for name, s in stats.items():
        if not s.get("enabled"):
            continue
        a = s.get("attempts", 0) or 0
        succ = s.get("successes", 0) or 0
        sources.append({
            "source": name,
            "weight": s.get("weight", 1.0),
            "attempts": a,
            "successes": succ,
            "failures": s.get("failures", 0) or 0,
            "duplicates": s.get("duplicates", 0) or 0,
            "yielded": s.get("yielded", 0) or 0,
            "consecutive_failures": s.get("consecutive_failures", 0) or 0,
            "last_failure_reason": s.get("last_failure_reason"),
        })

    # Recent duplicate & stale & sent samples
    cutoff = int(time.time()) - window_hours * 3600
    with db.conn() as c:
        dups = [dict(r) for r in c.execute(
            "SELECT i.title, i.source, i.url FROM items i "
            "WHERE i.status='duplicate' AND i.fetched_at>=? ORDER BY i.fetched_at DESC LIMIT 30",
            (cutoff,)).fetchall()]
        stales = [dict(r) for r in c.execute(
            "SELECT i.title, i.source, i.url FROM items i "
            "WHERE i.status='stale' AND i.fetched_at>=? ORDER BY i.fetched_at DESC LIMIT 20",
            (cutoff,)).fetchall()]
        sent = [dict(r) for r in c.execute(
            "SELECT d.headline, d.summary, d.importance, d.category, i.source, i.url "
            "FROM digests d JOIN items i ON i.id=d.item_id "
            "WHERE d.sent_at>=? ORDER BY d.sent_at DESC LIMIT 25",
            (cutoff,)).fetchall()]

    # Currently registered primary sources — for the LLM to know what's already covered
    primaries = db.get_primary_sources(enabled_only=False)

    return {
        "window_hours": window_hours,
        "sources": sources,
        "recent_duplicates": dups,
        "recent_stale": stales,
        "recent_sent": sent,
        "primary_sources_registered": [
            {
                "domain": p["domain"],
                "kind": p["kind"],
                "url": p["url"],
                "display_name": p["display_name"],
                "requires_login": bool(p["requires_login"]),
            } for p in primaries
        ],
    }


def run(window_hours=24):
    snap = build_snapshot(window_hours)
    p = db.get_active_prompt("meta_eval")
    template = p["prompt_text"] if p else prompts.META_EVAL_V1
    prompt = template.format(
        window_hours=window_hours,
        snapshot=json.dumps(snap, ensure_ascii=False, indent=1)[:4000],
    )
    decision = llm.call_llm_json(prompt, max_tokens=4000)
    if not decision:
        return {"snapshot": snap, "actions": {}, "error": "llm returned nothing"}

    actions = {"source_actions": [], "new_prompts": {}, "add_sources": [],
               "primary_source_actions": []}
    # Apply source actions
    for sa in decision.get("source_actions", []) or []:
        name = sa.get("source")
        action = sa.get("action", "keep")
        if action == "disable":
            db.set_source_enabled(name, False)
        elif action in ("downweight", "weight_down", "lower_weight"):
            try:
                new_w = float(sa.get("new_weight", sa.get("weight", 0.5)))
            except (TypeError, ValueError):
                new_w = 0.5
            db.set_source_weight(name, max(0.0, min(1.0, new_w)))
        # "keep" / "upweight" / unknown — leave as is
        actions["source_actions"].append(sa)

    # Apply primary-source discoveries
    for psa in decision.get("primary_source_actions", []) or []:
        if psa.get("action") != "add":
            continue
        domain = psa.get("domain")
        if not domain:
            continue
        # Don't add X accounts that we don't have a session for
        # (those go in primary_sources as requires_login=1, which is fine —
        # we record them so meta-eval can keep recommending them and once the
        # user logs in we just point login_session_path at the file).
        ok = db.add_primary_source(
            domain=domain,
            kind=psa.get("kind", "rss"),
            url=psa.get("url", ""),
            display_name=psa.get("display_name", domain),
            weight=float(psa.get("weight", 5.0)),
            requires_login=bool(psa.get("requires_login", False)),
            discovered_from=psa.get("reason", "meta-eval auto-discovered"),
        )
        if ok:
            actions["primary_source_actions"].append({"added": domain})

    # Apply prompt updates — only accept relatively short rewrites to avoid
    # the LLM accidentally trashing a working prompt. New prompt must differ
    # from current and be a meaningful size, but not a wall of text.
    cur_digest = db.get_active_prompt("digest")
    cur_dedup = db.get_active_prompt("dedup")
    for task, cur in [("digest", cur_digest), ("dedup", cur_dedup)]:
        new_txt = (decision.get("new_prompts", {}) or {}).get(task)
        if not new_txt or not isinstance(new_txt, str):
            continue
        if not cur:
            continue
        if new_txt == cur["prompt_text"]:
            continue
        if len(new_txt) < 50 or len(new_txt) > 3000:
            continue  # too short = likely garbage, too long = likely truncated
        # Save but keep current active until we have evidence new one is better
        db.save_prompt_version(
            task, cur["version"] + 1, new_txt,
            notes="auto-updated by meta-eval (kept inactive until validated)",
            activate=False,
        )
        actions["new_prompts"][task] = f"v{cur['version'] + 1} staged (inactive)"

    actions["add_sources"] = decision.get("add_sources", []) or []
    actions["prompt_notes"] = decision.get("prompt_notes", "")

    db.save_eval(
        run_id=f"meta-{int(time.time())}",
        window_hours=window_hours,
        snapshot=snap,
        actions=actions,
    )
    return {"snapshot": snap, "actions": actions}


def seed_initial_prompts():
    """Insert v1 prompts if the table is empty.

    The active prompts are stored in DB and *can* be rewritten by meta-eval.
    We seed digest + meta_eval here. Batch dedup/digest templates live in
    pipeline.py as constants (closely tied to the batched call signatures)
    and are not in the DB — meta-eval can suggest rewrites but they go
    through manual review.
    """
    if db.get_active_prompt("digest") is None:
        db.save_prompt_version("digest", 1, prompts.DIGEST_V1, notes="initial seed", activate=True)
    if db.get_active_prompt("meta_eval") is None:
        db.save_prompt_version("meta_eval", 1, prompts.META_EVAL_V1, notes="initial seed", activate=True)
    # Mark "dedup" prompt slot as occupied too so meta-eval knows it exists,
    # but use the per-item digest prompt as a placeholder (it's never actually
    # used — production code uses BATCH_DEDUP_TEMPLATE in pipeline.py).
    if db.get_active_prompt("dedup") is None:
        db.save_prompt_version(
            "dedup", 1, prompts.DIGEST_V1,
            notes="placeholder — actual dedup uses BATCH_DEDUP_TEMPLATE in pipeline.py",
            activate=True,
        )
