"""Format digests into a chat-friendly block and emit to stdout.

Cron job captures stdout and sends it as the message. If nothing new,
stdout is empty -> user sees nothing (the "silent until there's news"
behavior the user asked for).

Output format (single block per tick):
  - Markdown text for the user (existing behavior)
  - Followed by a JSON-line footer:  [[DIGEST_FEEDBACK_BEGIN]] {...} [[DIGEST_FEEDBACK_END]]
    describing the per-digest keyboards, so the QQ send layer can attach
    inline buttons when it knows how. The footer is invisible to the
    user (the QQ send layer strips it; cron fallback without QQ knowledge
    shows it as raw text but it's a single line so the user can ignore).

Per-digest buttons (4 total, single row):
  👍 喜欢     → feedback:like:<digest_id>
  ⭐ 神作     → feedback:great:<digest_id>
  👎 不感兴趣 → feedback:dislike:<digest_id>
  🚫 拒收     → feedback:block:<digest_id>
"""
import time
import json
from . import db


CATEGORY_EMOJI = {
    "model_release": "🚀",
    "paper": "📄",
    "funding": "💰",
    "product": "🛠️",
    "research": "🔬",
    "industry": "📰",
    "open_source": "⭐",
    "other": "•",
}

# Visual labels for the 4 feedback buttons. The emoji + 4-字 label is
# compact enough to fit in a single QQ keyboard row.
FEEDBACK_BUTTONS = [
    ("like",    "👍 喜欢"),
    ("great",   "⭐ 神作"),
    ("dislike", "👎 不感兴趣"),
    ("block",   "🚫 拒收"),
]

# Markdown footer markers. The QQ send layer finds the JSON between these
# markers and uses it to attach per-digest keyboards; any other consumer
# (e.g. local file dump, fallback) just shows the markdown text.
FEEDBACK_FOOTER_BEGIN = "[[DIGEST_FEEDBACK_BEGIN]]"
FEEDBACK_FOOTER_END = "[[DIGEST_FEEDBACK_END]]"


def format_one(d):
    cat = d.get("category") or "other"
    emoji = CATEGORY_EMOJI.get(cat, "•")
    src = d.get("source", "")
    url = d.get("url", "")
    headline = d.get("headline", "").strip()
    summary = d.get("summary", "").strip()
    importance = int(d.get("importance", 1))
    stars = "★" * importance + "☆" * (5 - importance)
    lines = [f"{emoji} **{headline}**  {stars}"]
    if summary:
        lines.append(summary)
    if src:
        lines.append(f"`{src}`")
    if url:
        # Use a short, clickable label so QQ renders the URL as a hyperlink
        # rather than a long raw string. Fall back to the raw URL if it's
        # already short (≤ 32 chars).
        if len(url) <= 32:
            lines.append(url)
        else:
            # Strip protocol + www + trailing path bits to get a readable anchor
            anchor = _shorten_url_for_anchor(url)
            lines.append(f"[{anchor}]({url})")
    return "\n".join(lines)


def _shorten_url_for_anchor(url: str) -> str:
    """Make a readable link label for a long URL. QQ supports markdown links
    in msg_type 2 messages, but very long anchors look ugly."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        host = p.netloc.replace("www.", "")
        path = p.path.rstrip("/").split("/")[-1] if p.path else ""
        if path and len(path) <= 24 and not path.startswith("status") and not path.startswith("papers"):
            return f"{host}/{path}"
        return host
    except Exception:
        return url[:32]


def build_keyboard_for_digest(digest_id):
    """Build the 4-button inline keyboard for one digest.

    Returns a dict ready to be wrapped in the QQ API's ``keyboard`` field,
    or None if the platform doesn't support keyboards. Each button shares
    ``group_id='feedback'`` so the user can change their mind — clicking
    a different button in the same group un-greys the previous choice and
    greys the new one.
    """
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": f"fb_{vote}",
                            "render_data": {
                                "label": label,
                                "visited_label": label,
                                "style": 0 if vote in ("dislike", "block") else 1,
                            },
                            "action": {
                                "type": 1,   # callback
                                "data": f"feedback:{vote}:{digest_id}",
                                "permission": {"type": 2},  # all users
                                "click_limit": 5,           # allow re-vote
                            },
                            "group_id": "feedback",
                        }
                        for vote, label in FEEDBACK_BUTTONS
                    ]
                }
            ]
        }
    }


def format_run(digests):
    """Return (markdown_text, keyboard_payload_dict).

    The markdown text is what the user reads. The keyboard_payload_dict
    maps digest_id → keyboard dict; the QQ send layer walks this map and
    sends each digest as its own message (with the keyboard attached)
    rather than one giant message — because QQ keyboards attach to a
    single message at a time, and per-digest buttons require per-digest
    messages.
    """
    if not digests:
        return "", {}
    digests = sorted(digests, key=lambda d: -int(d.get("importance", 1)))
    header = f"🤖 AI 快讯 · {time.strftime('%m-%d %H:%M')} · {len(digests)} 条"
    body = "\n\n".join(format_one(d) for d in digests)
    markdown = f"{header}\n\n{body}"

    # Per-digest keyboards: map digest_id → keyboard dict
    keyboards = {d["id"]: build_keyboard_for_digest(d["id"]) for d in digests}

    # Also build a "global" summary view: header text + footer explaining
    # the per-digest buttons. For now, the QQ send layer will send the
    # header once, then one message per digest (with its own keyboard).
    return markdown, keyboards


def emit_for_run(run_id):
    """Print formatted digest block for any unsent digests, mark sent.

    Originally this filtered by run_id, but with slow LLM digestion
    a tick's items may not finish processing until a later tick — so
    the original tick's run_id would be wrong. Now we just emit ALL
    unsent digests, ordered by importance. Safe to call repeatedly.

    IMPORTANT: any unsent digests at all means we have news — emit
    them. Only print nothing if zero unsent.

    Stdout format:
        <markdown text>

        [[DIGEST_FEEDBACK_BEGIN]]
        <single-line JSON: {header, digests: [{id, headline, importance, keyboard}, ...]}>
        [[DIGEST_FEEDBACK_END]]

    The footer is ignored by readers that don't understand it; the QQ
    send layer parses it to send per-digest messages with keyboards.
    """
    with db.conn() as c:
        rows = c.execute(
            """SELECT d.*, i.url, i.source
               FROM digests d JOIN items i ON i.id = d.item_id
               WHERE d.sent_at IS NULL
               ORDER BY d.importance DESC, d.id DESC""",
        ).fetchall()
    digests = [dict(r) for r in rows]
    if not digests:
        return 0

    markdown, keyboards = format_run(digests)

    # Build the structured footer (one JSON line so it stays greppable)
    footer_payload = {
        "header": markdown.split("\n\n", 1)[0],   # the "🤖 AI 快讯 · ..." line
        "tick_ts": int(time.time()),
        "digests": [
            {
                "id": d["id"],
                "headline": d.get("headline", "").strip(),
                "summary": d.get("summary", "").strip(),
                "importance": int(d.get("importance", 1)),
                "category": d.get("category") or "other",
                "url": d.get("url", ""),
                "source": d.get("source", ""),
                "keyboard": keyboards[d["id"]],
            }
            for d in sorted(digests, key=lambda x: -int(x.get("importance", 1)))
        ],
    }
    footer_json = json.dumps(footer_payload, ensure_ascii=False)

    if markdown:
        try:
            print(markdown, flush=True)
            # Blank line, then begin marker, JSON line, end marker.
            print(f"\n{FEEDBACK_FOOTER_BEGIN}", flush=True)
            print(footer_json, flush=True)
            print(f"{FEEDBACK_FOOTER_END}", flush=True)
        except (BrokenPipeError, ValueError) as e:
            # Downstream consumer (send_to_qq.py) already exited OR stdout
            # was closed by the parent process. Either way they read whatever
            # they needed before exiting. Suppress the traceback so the cron
            # run looks successful (items still marked sent below).
            # NOTE: ValueError ("I/O operation on closed file") is what Python
            # actually raises when stdout is fully closed; BrokenPipeError
            # only fires when the pipe writer (downstream) closes while we
            # still have buffered data.
            import sys as _sys
            print(f"[formatter] downstream pipe closed early ({type(e).__name__}: {e} ignored) — {len(digests)} digests still marked sent in DB",
                  file=_sys.stderr, flush=True)
    for d in digests:
        db.mark_digest_sent(d["id"])
    return len(digests)
