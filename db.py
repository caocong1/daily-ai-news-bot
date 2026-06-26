"""SQLite data layer for AI news pipeline.

Schema:
- items: raw fetched entries (URL, title, body, source, fetched_at, status, semantic_sig)
- digests: LLM-digested news items (item_id, headline, summary, category, importance, published_at)
- seen_signatures: semantic fingerprint index for cross-run dedup
- source_stats: per-source performance counters (success/fail/duplicate/yielded)
- eval_runs: meta-eval snapshots for self-iteration
"""
import sqlite3
import time
import json
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path.home() / ".hermes" / "ai_news" / "ai_news.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    source TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    published_at INTEGER,           -- best-effort timestamp from source
    semantic_sig TEXT,              -- short normalized key for dedup
    status TEXT DEFAULT 'pending',  -- pending | filtered | digested | sent | expired | duplicate
    first_seen_run TEXT,            -- cron run id where first observed
    last_seen_run TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_fetched ON items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_sig ON items(semantic_sig);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT,        -- model_release | paper | funding | product | research | industry | other
    importance INTEGER,   -- 1-5
    ai_relevance TEXT,    -- direct | tangential | none
    is_fresh TEXT,        -- fresh | stale
    sent_at INTEGER,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_digests_sent ON digests(sent_at);
CREATE INDEX IF NOT EXISTS idx_digests_run ON digests(run_id);

CREATE TABLE IF NOT EXISTS digest_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    vote TEXT NOT NULL,             -- like | great | dislike | block
    operator_id TEXT,               -- QQ openid of the clicker
    recorded_at INTEGER NOT NULL,
    UNIQUE(digest_id, vote, operator_id)   -- one vote per user per digest per type
);
CREATE INDEX IF NOT EXISTS idx_fb_digest ON digest_feedback(digest_id);
CREATE INDEX IF NOT EXISTS idx_fb_vote ON digest_feedback(vote);

CREATE TABLE IF NOT EXISTS source_stats (
    source TEXT PRIMARY KEY,
    weight REAL DEFAULT 1.0,
    attempts INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0,
    duplicates INTEGER DEFAULT 0,
    yielded INTEGER DEFAULT 0,       -- items that survived all filters and got sent
    last_success_at INTEGER,
    last_failure_at INTEGER,
    last_failure_reason TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    fetch_mode TEXT DEFAULT 'curl'  -- 'curl' | 'browser' | 'curl_fallback_browser'
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    evaluated_at INTEGER NOT NULL,
    window_hours INTEGER,
    snapshot_json TEXT,
    actions_json TEXT
);

CREATE TABLE IF NOT EXISTS primary_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,      -- e.g. "openai.com", "anthropic.com"
    kind TEXT NOT NULL,               -- 'official_blog' | 'arxiv' | 'github_org' | 'social_account' | 'rss'
    url TEXT NOT NULL,                -- canonical feed URL
    display_name TEXT,
    discovered_from TEXT,             -- which digest/url led us to add this
    weight REAL DEFAULT 5.0,          -- primary sources get much higher weight
    enabled INTEGER DEFAULT 1,
    requires_login INTEGER DEFAULT 0,
    login_session_path TEXT,          -- for browser sources: where to load cookies from
    added_at INTEGER NOT NULL,
    last_success_at INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_primary_enabled ON primary_sources(enabled);


CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,             -- digest | dedup | meta_eval
    version INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    notes TEXT,
    created_at INTEGER NOT NULL,
    is_active INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prompt_task_active ON prompt_versions(task, is_active);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
    # Forward-compat migrations for existing DBs
    with conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(source_stats)").fetchall()}
        if "fetch_mode" not in cols:
            c.execute("ALTER TABLE source_stats ADD COLUMN fetch_mode TEXT DEFAULT 'curl'")
    # Seed source_stats rows on first run
    with conn() as c:
        existing = {r["source"] for r in c.execute("SELECT source FROM source_stats").fetchall()}
        for s in DEFAULT_SOURCES:
            if s not in existing:
                c.execute(
                    "INSERT INTO source_stats (source, weight) VALUES (?, 1.0)",
                    (s,),
                )
    # Seed primary_sources (first-party / upstream of news)
    seed_primary_sources()


def seed_primary_sources():
    """Insert DEFAULT_PRIMARY_SOURCES rows that aren't already there."""
    now = int(time.time())
    with conn() as c:
        existing = {r["domain"] for r in c.execute("SELECT domain FROM primary_sources").fetchall()}
        for domain, kind, url, name, weight, requires_login in DEFAULT_PRIMARY_SOURCES:
            if domain in existing:
                continue
            c.execute(
                """INSERT INTO primary_sources
                   (domain, kind, url, display_name, weight, requires_login, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (domain, kind, url, name, weight, requires_login, now),
            )


def get_primary_sources(enabled_only=True):
    with conn() as c:
        if enabled_only:
            rows = c.execute(
                "SELECT * FROM primary_sources WHERE enabled = 1 ORDER BY weight DESC"
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM primary_sources ORDER BY weight DESC").fetchall()
    return [dict(r) for r in rows]


def add_primary_source(domain, kind, url, display_name, weight=5.0,
                       requires_login=False, login_session_path=None,
                       discovered_from=None, notes=None):
    now = int(time.time())
    with conn() as c:
        try:
            c.execute(
                """INSERT INTO primary_sources
                   (domain, kind, url, display_name, weight, requires_login,
                    login_session_path, discovered_from, added_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (domain, kind, url, display_name, weight, int(requires_login),
                 login_session_path, discovered_from, now, notes),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # domain already exists


def set_primary_session_path(domain, path):
    """Save a Chrome DevTools MCP session/cookies file path for a primary source."""
    with conn() as c:
        c.execute(
            "UPDATE primary_sources SET login_session_path = ? WHERE domain = ?",
            (path, domain),
        )


def mark_primary_success(domain):
    with conn() as c:
        c.execute(
            "UPDATE primary_sources SET last_success_at = ? WHERE domain = ?",
            (int(time.time()), domain),
        )


def list_pending_login_sources():
    """Return primary sources that require login but have no session yet."""
    with conn() as c:
        rows = c.execute(
            """SELECT * FROM primary_sources
               WHERE requires_login = 1
                 AND enabled = 1
                 AND (login_session_path IS NULL OR login_session_path = '')"""
        ).fetchall()
    return [dict(r) for r in rows] 


# All sources we know about. Add new ones by appending; the loop in runner.py
# iterates this list and skips rows where enabled=0.
DEFAULT_SOURCES = [
    "hackernews_ai",
    "arxiv_ai",
    "github_trending",
    "huggingface_papers",
    "qbitai",
    "jiqizhixin",
    "the_decoder",
    "producthunt_ai",
    "openai_blog",
    "cn_36kr",
    "cn_leiphone",
    "cn_ithome",
    "cn_tmtpost",
    "cn_baidu_news",
]  # disabled-by-default sources we tried but couldn't reach from this host:
#   reddit_ml    — reddit.com hard-blocks, alt frontends all dead
#   twitter_x_ai — nitter mirrors all dead
#   zhihu_hot    — login wall (waiting for user login)
# To re-enable later, just import + register in sources/__init__.py.


# Seed list of known first-party / primary sources. These are read BEFORE
# secondary aggregators so we surface news before media covers it.
DEFAULT_PRIMARY_SOURCES = [
    # Official AI lab blogs
    ("openai.com",          "official_blog", "https://openai.com/blog/rss.xml", "OpenAI Blog", 5.0, 0),
    ("anthropic.com",       "official_blog", "https://www.anthropic.com/news/rss.xml", "Anthropic News", 5.0, 0),
    ("blog.google",         "official_blog", "https://blog.google/technology/ai/rss/", "Google AI Blog", 5.0, 0),
    ("deepmind.google",     "official_blog", "https://deepmind.google/blog/rss.xml", "DeepMind Blog", 5.0, 0),
    ("ai.meta.com",         "official_blog", "https://ai.meta.com/blog/rss/", "Meta AI Blog", 5.0, 0),
    ("huggingface.co",      "official_blog", "https://huggingface.co/blog/feed.xml", "HuggingFace Blog", 4.0, 0),
    ("mistral.ai",          "official_blog", "https://mistral.ai/feed.xml", "Mistral AI Blog", 4.0, 0),
    ("cohere.com",          "official_blog", "https://cohere.com/blog/rss.xml", "Cohere Blog", 4.0, 0),
    # arXiv (already covered by arxiv_ai but add as primary for completeness)
    ("arxiv.org",           "arxiv",         "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL+OR+cat:cs.LG&sortBy=submittedDate&sortOrder=descending&max_results=30", "arXiv AI/CL/LG", 5.0, 0),
    # Chinese primary sources
    ("qbitai.com",          "official_blog", "https://www.qbitai.com/feed", "量子位", 3.0, 0),
    ("jiqizhixin.com",      "official_blog", "https://www.jiqizhixin.com/rss", "机器之心", 3.0, 0),
    # Twitter/X primary accounts (require login)
    ("x.com/OpenAI",        "social_account", "https://x.com/OpenAI", "@OpenAI", 4.0, 1),
    ("x.com/AnthropicAI",   "social_account", "https://x.com/AnthropicAI", "@AnthropicAI", 4.0, 1),
    ("x.com/sama",          "social_account", "https://x.com/sama", "@sama (Sam Altman)", 3.5, 1),
    ("x.com/karpathy",      "social_account", "https://x.com/karpathy", "@karpathy", 3.5, 1),
    ("x.com/ylecun",        "social_account", "https://x.com/ylecun", "@ylecun", 3.0, 1),
    # X accounts via no-auth scrape (syndication.twitter.com + xcancel.com).
    # These work WITHOUT a login session but are subject to IP-based rate
    # limits / Cloudflare challenges — best-effort, intermittent. Pair with
    # the login-based social_account entries above as the canonical path.
    ("x_noauth/OpenAI",         "x_account_noauth", "https://x.com/OpenAI", "@OpenAI (no-auth)", 3.5, 0),
    ("x_noauth/AnthropicAI",    "x_account_noauth", "https://x.com/AnthropicAI", "@AnthropicAI (no-auth)", 3.5, 0),
    ("x_noauth/GoogleDeepMind", "x_account_noauth", "https://x.com/GoogleDeepMind", "@GoogleDeepMind (no-auth)", 3.0, 0),
    ("x_noauth/sama",           "x_account_noauth", "https://x.com/sama", "@sama (no-auth)", 3.0, 0),
    ("x_noauth/karpathy",       "x_account_noauth", "https://x.com/karpathy", "@karpathy (no-auth)", 3.0, 0),
    ("x_noauth/mingchikuo",     "x_account_noauth", "https://x.com/mingchikuo", "@mingchikuo (no-auth)", 3.5, 0),
    # Reddit primary subs (require login)
    ("reddit.com/r/MachineLearning", "social_account", "https://old.reddit.com/r/MachineLearning/new.json", "r/MachineLearning", 3.0, 1),
    ("reddit.com/r/LocalLLaMA",      "social_account", "https://old.reddit.com/r/LocalLLaMA/new.json", "r/LocalLLaMA", 3.0, 1),
]  # Note: login-required sources (requires_login=1) are dormant until user
# provides a session cookie file at the path stored in login_session_path.


def upsert_item(url, title, body, source, published_at=None, run_id=""):
    """Insert an item; return (item_id, was_inserted). On URL collision, refresh last_seen_run."""
    now = int(time.time())
    with conn() as c:
        row = c.execute("SELECT id, status FROM items WHERE url = ?", (url,)).fetchone()
        if row:
            c.execute(
                "UPDATE items SET last_seen_run = ? WHERE id = ?",
                (run_id, row["id"]),
            )
            return row["id"], False
        cur = c.execute(
            """INSERT INTO items (url, title, body, source, fetched_at, published_at,
                                  first_seen_run, last_seen_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, title, body, source, now, published_at, run_id, run_id),
        )
        return cur.lastrowid, True


def get_pending_items(limit=200):
    with conn() as c:
        return [
            dict(r)
            for r in c.execute(
                """SELECT id, url, title, body, source, fetched_at, published_at, status,
                          first_seen_run
                   FROM items
                   WHERE status = 'pending'
                   ORDER BY fetched_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        ]


def mark_item(item_id, status):
    with conn() as c:
        c.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))


def refresh_item_for_reprocessing(item_id, run_id):
    """When a source refetches an item already in DB, reset status to pending
    so the LLM pipeline will re-evaluate it. Only resets if currently in a
    terminal ERROR state (stale/duplicate/error/low_relevance) — leaves
    'pending' alone so we don't disturb in-flight processing, and
    critically leaves 'digested' alone so we don't re-digest & re-push the
    same item on every cron tick when the source keeps emitting it.

    Rationale: the original code reset 'digested' too, which caused each
    fetch to re-trigger the full pipeline for the same URL, producing
    duplicate digests (and duplicate QQ pushes) every 30 minutes for as
    long as the source kept the URL in its feed. Sources like OpenAI RSS
    keep recent posts visible for days — so we never want to re-digest a
    'digested' item just because the source still lists it. If the source
    genuinely needs to push an update, that should be a new URL / new item.

    Returns 1 if status was reset, 0 if it was already pending or digested.
    """
    with conn() as c:
        cur = c.execute(
            """UPDATE items SET status = 'pending', last_seen_run = ?
               WHERE id = ? AND status IN ('stale', 'duplicate', 'error', 'low_relevance')""",
            (run_id, item_id),
        )
        return cur.rowcount


def set_semantic_sig(item_id, sig):
    with conn() as c:
        c.execute("UPDATE items SET semantic_sig = ? WHERE id = ?", (sig, item_id))


def insert_digest(item_id, headline, summary, category, importance,
                  ai_relevance, is_fresh, run_id):
    """Insert a freshly-generated digest. sent_at stays NULL until emit actually
    pushes it to the user — the old code pre-stamped it with the creation time,
    which made emit_for_run's `sent_at IS NULL` filter never match anything."""
    with conn() as c:
        cur = c.execute(
            """INSERT INTO digests
               (item_id, headline, summary, category, importance,
                ai_relevance, is_fresh, sent_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (item_id, headline, summary, category, importance,
             ai_relevance, is_fresh, run_id),
        )
        return cur.lastrowid


def recent_digests(hours=24, limit=200):
    cutoff = int(time.time()) - hours * 3600
    with conn() as c:
        return [
            dict(r)
            for r in c.execute(
                """SELECT d.*, i.title AS raw_title, i.url, i.source
                   FROM digests d JOIN items i ON i.id = d.item_id
                   WHERE d.sent_at >= ?
                   ORDER BY d.sent_at DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        ]


def unsent_digests_for_run(run_id):
    with conn() as c:
        return [
            dict(r)
            for r in c.execute(
                """SELECT d.*, i.url, i.source
                   FROM digests d JOIN items i ON i.id = d.item_id
                   WHERE d.run_id = ? AND d.sent_at IS NULL
                   ORDER BY d.importance DESC""",
                (run_id,),
            ).fetchall()
        ]


def mark_digest_sent(digest_id):
    with conn() as c:
        c.execute("UPDATE digests SET sent_at = ? WHERE id = ?", (int(time.time()), digest_id))


# --- source stats ---
def get_source_stats():
    with conn() as c:
        return {r["source"]: dict(r) for r in c.execute("SELECT * FROM source_stats").fetchall()}


def bump_source(source, *, success=False, duplicate=False, yielded=False,
                failure_reason=None):
    now = int(time.time())
    with conn() as c:
        if success:
            c.execute(
                """UPDATE source_stats
                   SET attempts = attempts + 1, successes = successes + 1,
                       last_success_at = ?, consecutive_failures = 0
                   WHERE source = ?""",
                (now, source),
            )
        else:
            c.execute(
                """UPDATE source_stats
                   SET attempts = attempts + 1, failures = failures + 1,
                       last_failure_at = ?, last_failure_reason = ?,
                       consecutive_failures = consecutive_failures + 1
                   WHERE source = ?""",
                (now, failure_reason, source),
            )
        if duplicate:
            c.execute("UPDATE source_stats SET duplicates = duplicates + 1 WHERE source = ?", (source,))
        if yielded:
            c.execute("UPDATE source_stats SET yielded = yielded + 1 WHERE source = ?", (source,))


def set_source_weight(source, weight):
    with conn() as c:
        c.execute("UPDATE source_stats SET weight = ? WHERE source = ?", (weight, source))


def set_source_enabled(source, enabled):
    with conn() as c:
        c.execute("UPDATE source_stats SET enabled = ? WHERE source = ?", (1 if enabled else 0, source))


# --- prompt versions ---
def get_active_prompt(task):
    with conn() as c:
        row = c.execute(
            """SELECT id, version, prompt_text FROM prompt_versions
               WHERE task = ? AND is_active = 1 ORDER BY version DESC LIMIT 1""",
            (task,),
        ).fetchone()
        return dict(row) if row else None


def save_prompt_version(task, version, prompt_text, notes="", activate=True):
    now = int(time.time())
    with conn() as c:
        if activate:
            c.execute("UPDATE prompt_versions SET is_active = 0 WHERE task = ?", (task,))
        c.execute(
            """INSERT INTO prompt_versions (task, version, prompt_text, notes, created_at, is_active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task, version, prompt_text, notes, now, 1 if activate else 0),
        )


# --- meta-eval ---
def save_eval(run_id, window_hours, snapshot, actions):
    with conn() as c:
        c.execute(
            """INSERT INTO eval_runs (run_id, evaluated_at, window_hours,
                                      snapshot_json, actions_json)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, int(time.time()), window_hours,
             json.dumps(snapshot, ensure_ascii=False),
             json.dumps(actions, ensure_ascii=False)),
        )


def recent_eval_runs(limit=5):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM eval_runs ORDER BY evaluated_at DESC LIMIT ?", (limit,)
        ).fetchall()]


# -------- digest_feedback ----------

FEEDBACK_VOTES = ("like", "great", "dislike", "block")

# Weight deltas per vote. Applied to source_stats.weight (and used as
# importance nudges) so user feedback actually shifts the pipeline.
FEEDBACK_WEIGHT_DELTA = {
    "like":    +0.10,   # small positive signal
    "great":   +0.50,   # strong positive — "more like this please"
    "dislike": -0.20,   # mild negative
    "block":   -1.00,   # strong negative — downweight heavily
}


def record_feedback(digest_id, vote, operator_id=""):
    """Record a feedback click from a user. Returns True if new, False if duplicate.

    Idempotent per (digest_id, vote, operator_id) — repeated clicks are no-ops.
    Side effect: nudges the digest's source weight by FEEDBACK_WEIGHT_DELTA[vote]
    so future ticks learn from accumulated feedback.
    """
    if vote not in FEEDBACK_VOTES:
        return False
    now = int(time.time())
    with conn() as c:
        # INSERT OR IGNORE so duplicate clicks are no-ops
        cur = c.execute(
            """INSERT OR IGNORE INTO digest_feedback
               (digest_id, vote, operator_id, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (digest_id, vote, operator_id or "", now),
        )
        inserted = cur.rowcount > 0
        if inserted:
            delta = FEEDBACK_WEIGHT_DELTA[vote]
            # join digests → items to find the source
            row = c.execute(
                """SELECT i.source FROM digests d
                   JOIN items i ON i.id = d.item_id
                   WHERE d.id = ?""",
                (digest_id,),
            ).fetchone()
            if row:
                src = row["source"]
                # clamp weight to a sane range so a single "block" can't drive it negative
                c.execute(
                    """UPDATE source_stats
                       SET weight = MAX(0.05, MIN(10.0, weight + ?))
                       WHERE source = ?""",
                    (delta, src),
                )
        return inserted


def get_feedback_for_digest(digest_id):
    """Return vote counts for a digest: {"like": N, "great": N, ...}."""
    with conn() as c:
        rows = c.execute(
            """SELECT vote, COUNT(*) AS n FROM digest_feedback
               WHERE digest_id = ? GROUP BY vote""",
            (digest_id,),
        ).fetchall()
    out = {v: 0 for v in FEEDBACK_VOTES}
    for r in rows:
        out[r["vote"]] = r["n"]
    return out


def get_feedback_summary(days=30):
    """Aggregate feedback over the last N days, for meta-eval."""
    cutoff = int(time.time()) - days * 86400
    with conn() as c:
        by_source = c.execute(
            """SELECT i.source, f.vote, COUNT(*) AS n
               FROM digest_feedback f
               JOIN digests d ON d.id = f.digest_id
               JOIN items i ON i.id = d.item_id
               WHERE f.recorded_at >= ?
               GROUP BY i.source, f.vote
               ORDER BY i.source""",
            (cutoff,),
        ).fetchall()
    out = {}
    for r in by_source:
        out.setdefault(r["source"], {v: 0 for v in FEEDBACK_VOTES})
        out[r["source"]][r["vote"]] = r["n"]
    return out


if __name__ == "__main__":
    init_db()
    print(f"DB ready at {DB_PATH}")
