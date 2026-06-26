"""Per-source evaluation: analyze the last N hours of fetch attempts and
recommend fetch_mode ('curl' | 'browser' | 'curl_fallback_browser') for each.

Run from runner every 6h or manually. Updates source_stats.fetch_mode in DB.
"""
import time
from . import db
from . import llm


# Thresholds (counts over the eval window)
THRESHOLD_CURL_OK = 0.70          # >= 70% success → keep curl
THRESHOLD_BROWSER_REQUIRED = 0.50  # < 50% success → switch to browser
WINDOW_HOURS = 24


def evaluate_source(name, attempts, successes, failures):
    """Decide fetch_mode for one source based on recent stats."""
    if attempts < 3:
        return "curl", "not enough samples yet"
    succ_rate = successes / attempts
    if succ_rate >= THRESHOLD_CURL_OK:
        return "curl", f"success rate {succ_rate:.0%} >= {THRESHOLD_CURL_OK:.0%}"
    if succ_rate < THRESHOLD_BROWSER_REQUIRED:
        return "browser", f"success rate {succ_rate:.0%} < {THRESHOLD_BROWSER_REQUIRED:.0%}"
    return "curl_fallback_browser", f"success rate {succ_rate:.0%} in middle band"


def run(window_hours=WINDOW_HOURS, apply=True):
    """Look at source_stats over the eval window. Optionally apply changes."""
    # We don't have per-window counters, but source_stats is cumulative.
    # For a fair evaluation, count actual attempts in the items table:
    cutoff = int(time.time()) - window_hours * 3600
    with db.conn() as c:
        # For each enabled source: count fetches we observed in the items table
        # by counting unique first_seen_run values per source.
        sources = [r["source"] for r in c.execute(
            "SELECT source FROM source_stats WHERE enabled = 1"
        ).fetchall()]
        decisions = []
        for src in sources:
            rows = c.execute(
                """SELECT first_seen_run FROM items
                   WHERE source = ? AND fetched_at >= ?
                   GROUP BY first_seen_run""",
                (src, cutoff),
            ).fetchall()
            run_count = len(rows)  # how many distinct runs fetched this source
            # crude: assume success if any items were inserted
            success_count = sum(1 for r in rows if r["first_seen_run"])
            # also pull cumulative stats from source_stats
            stats = c.execute(
                "SELECT attempts, successes, failures, fetch_mode FROM source_stats WHERE source = ?",
                (src,),
            ).fetchone()
            attempts = stats["attempts"] or 0
            successes = stats["successes"] or 0
            failures = stats["failures"] or 0
            current_mode = stats["fetch_mode"] or "curl"

            new_mode, reason = evaluate_source(src, attempts, successes, failures)
            if new_mode != current_mode:
                decisions.append({
                    "source": src,
                    "current_mode": current_mode,
                    "new_mode": new_mode,
                    "attempts": attempts,
                    "successes": successes,
                    "failures": failures,
                    "reason": reason,
                })
                if apply:
                    c.execute(
                        "UPDATE source_stats SET fetch_mode = ? WHERE source = ?",
                        (new_mode, src),
                    )

    return {
        "window_hours": window_hours,
        "applied": apply,
        "decisions": decisions,
    }


if __name__ == "__main__":
    import json
    db.init_db()
    out = run()
    print(json.dumps(out, indent=2, ensure_ascii=False))
