"""Main pipeline runner — invoked by cron every 10 minutes.

Steps:
1. Fetch from all enabled sources.
2. Upsert items to DB (URL uniqueness handles basic dedup).
3. Run pipeline (structural + LLM dedup, then per-item digest).
4. Emit formatted block to stdout for newly-digested items.
5. Periodically (every 6 hours) run meta-eval.

Cron captures stdout and sends it. Empty stdout = no message to user.
"""
import sys
import os
import time
import json

# allow running as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_news import db
from ai_news import sources
from ai_news import pipeline
from ai_news import formatter
from ai_news import meta_eval
from ai_news import primaries


META_EVAL_INTERVAL = 6 * 3600  # 6h


def run_once(run_id):
    print(f"[runner] start run={run_id} pid={os.getpid()}", file=sys.stderr)

    # 0. PRIMARY sources — read first so we see news before aggregators cover it
    primary_counts = {}
    primary_skipped_login = 0
    for domain, items, err in primaries.fetch_all():
        if err and "no login session" in err:
            primary_skipped_login += 1
            continue
        if err:
            print(f"[runner] primary {domain} FAILED: {err[:100]}", file=sys.stderr)
            continue
        primary_counts[domain] = len(items)
        new_inserts = 0
        refreshed = 0
        for it in items:
            item_id, inserted = db.upsert_item(
                url=it["url"], title=it["title"], body=it.get("body", ""),
                source=it["source"], published_at=it.get("published_at"),
                run_id=run_id,
            )
            if inserted:
                new_inserts += 1
            else:
                # Already in DB. If it was previously marked stale/duplicate/etc.,
                # we want to re-process it since the source is now back feeding.
                # Reset status to pending so the pipeline picks it up.
                refreshed += db.refresh_item_for_reprocessing(item_id, run_id)
        if items:
            print(f"[runner] primary {domain} fetched={len(items)} "
                  f"new={new_inserts} refreshed={refreshed}",
                  file=sys.stderr)
    if primary_skipped_login:
        print(f"[runner] primary: {primary_skipped_login} sources waiting on user login",
              file=sys.stderr)

    # 1. Fetch secondary aggregators
    enabled = [s for s, st in db.get_source_stats().items() if st.get("enabled")]
    if not enabled:
        enabled = db.DEFAULT_SOURCES
    fetched_counts = {}
    for name, items, err in sources.fetch_all(enabled):
        if err:
            db.bump_source(name, failure_reason=err[:200])
            print(f"[runner] source {name} FAILED: {err[:100]}", file=sys.stderr)
            continue
        db.bump_source(name, success=True)
        fetched_counts[name] = len(items)
        new_inserts = 0
        refreshed = 0
        for it in items:
            item_id, inserted = db.upsert_item(
                url=it["url"], title=it["title"], body=it.get("body", ""),
                source=it["source"], published_at=it.get("published_at"),
                run_id=run_id,
            )
            if inserted:
                new_inserts += 1
            else:
                refreshed += db.refresh_item_for_reprocessing(item_id, run_id)
        if items or new_inserts or refreshed:
            print(f"[runner] source {name} fetched={len(items)} "
                  f"new={new_inserts} refreshed={refreshed}",
                  file=sys.stderr)

    # 2. Process pending items (LLM dedup + digest)
    # Cap at PROCESS_BATCH_SIZE so a single tick doesn't try to chew through
    # the entire backlog when the queue has accumulated. Items left over will
    # be processed by the next tick.
    PROCESS_BATCH_SIZE = 20
    pending_items = db.get_pending_items(limit=PROCESS_BATCH_SIZE)
    print(f"[runner] processing {len(pending_items)} pending items "
          f"(batch size {PROCESS_BATCH_SIZE})", file=sys.stderr)
    results = pipeline.process_pending(limit=PROCESS_BATCH_SIZE)
    digested = sum(1 for _, d, _ in results if d == "digested")
    dups = sum(1 for _, d, _ in results if d == "duplicate")
    stale = sum(1 for _, d, _ in results if d == "stale")
    print(f"[runner] decisions: digested={digested} dup={dups} stale={stale}",
          file=sys.stderr)

    # 3. Emit any unsent digests from THIS or PREVIOUS runs (cross-run safe).
    # This fixes the silent-tick bug where slow LLM digestion pushes items
    # across run boundaries — emit_for_run ignores run_id now.
    sent = formatter.emit_for_run(run_id)
    print(f"[runner] emitted {sent} digests", file=sys.stderr)

    # 4. Meta-eval every 6h
    with db.conn() as c:
        last_eval = c.execute(
            "SELECT MAX(evaluated_at) FROM eval_runs"
        ).fetchone()[0]
    now = int(time.time())
    if not last_eval or (now - last_eval) > META_EVAL_INTERVAL:
        print(f"[runner] running meta-eval", file=sys.stderr)
        try:
            res = meta_eval.run(window_hours=24)
            print(f"[runner] meta-eval actions: {json.dumps(res['actions'], ensure_ascii=False)[:300]}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[runner] meta-eval failed: {e}", file=sys.stderr)

    print(f"[runner] done", file=sys.stderr)


def main():
    db.init_db()
    meta_eval.seed_initial_prompts()
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_once(run_id)


if __name__ == "__main__":
    main()
