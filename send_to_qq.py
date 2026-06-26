#!/usr/bin/env python3
"""
send_to_qq.py — Send AI news digests to QQ bot with inline keyboard buttons.

Reads the structured footer from stdin (emitted by formatter.emit_for_run),
splits into per-digest messages, and POSTs each with its keyboard to the
QQ Bot v2 REST API. Falls back gracefully if the QQ env vars are missing
or the API errors — the markdown is still dumped to the output file for
local-only delivery.

Usage:
    python3 -u runner.py | python3 send_to_qq.py

Env vars (read from ~/.hermes/.env via load_dotenv if available):
    QQ_APP_ID          — numeric app id from q.qq.com
    QQ_CLIENT_SECRET   — app secret
    QQ_BOT_HOME_OPENID — openid of the user/chat to send to (default Home channel)

Exit codes:
    0 = all messages sent (or zero to send, or no creds configured → local fallback)
    1 = one or more messages failed to send
"""
import os
import sys
import json
import time
import logging
import asyncio
from pathlib import Path

# Load env vars from ~/.hermes/.env if python-dotenv is available
try:
    from dotenv import load_dotenv
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [send_to_qq] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("send_to_qq")


FEEDBACK_FOOTER_BEGIN = "[[DIGEST_FEEDBACK_BEGIN]]"
FEEDBACK_FOOTER_END = "[[DIGEST_FEEDBACK_END]]"


def parse_footer_from_stdin():
    """Read stdin, extract the structured footer JSON, return (markdown, payload).

    The footer is delimited by FEEDBACK_FOOTER_BEGIN / FEEDBACK_FOOTER_END markers.
    The markdown is everything before the BEGIN marker.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return "", None
    begin = raw.find(FEEDBACK_FOOTER_BEGIN)
    if begin == -1:
        # No footer — just plain markdown (legacy behavior)
        return raw.strip(), None
    markdown = raw[:begin].rstrip()
    end = raw.find(FEEDBACK_FOOTER_END, begin)
    if end == -1:
        log.warning("BEGIN marker found but no END marker — sending as plain markdown")
        return raw.strip(), None
    json_line = raw[begin + len(FEEDBACK_FOOTER_BEGIN):end].strip()
    try:
        payload = json.loads(json_line)
    except json.JSONDecodeError as e:
        log.warning("Footer JSON parse failed: %s — sending as plain markdown", e)
        return raw.strip(), None
    return markdown, payload


async def refresh_access_token(session, app_id, client_secret):
    """Get a fresh access token from the QQ bot API."""
    url = "https://bots.qq.com/app/getAppAccessToken"
    body = {"appId": app_id, "clientSecret": client_secret}
    async with session.post(url, json=body) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"token HTTP {resp.status}: {text[:300]}")
        data = await resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"no access_token in response: {data}")
    return token


async def send_one_message(session, api_base, token, recipient, content, keyboard=None, msg_seq=None):
    """Send a single C2C message. Returns the QQ message id or raises.

    Note: QQ bot v2 API uses ``msg_seq`` (auto-incrementing integer per
    recipient) for de-dup, NOT ``msg_id`` (which is the response field).

    Also: QQ's inline keyboard is rendered **only on markdown messages**
    (per the official docs — ``在 markdown 消息的基础上，支持消息最底部挂载按钮``).
    Plain-text messages silently drop the keyboard. We always send as
    markdown so the keyboard actually shows up.
    """
    url = f"{api_base}/v2/users/{recipient}/messages"
    body = {
        "msg_type": 2,   # 2 = markdown
        "msg_seq": msg_seq if msg_seq is not None else 1,
        "markdown": {"content": content},
    }
    if keyboard is not None:
        body["keyboard"] = keyboard
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
    }
    async with session.post(url, json=body, headers=headers) as resp:
        text = await resp.text()
        if resp.status not in (200, 201):
            raise RuntimeError(f"send HTTP {resp.status}: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"non-JSON response: {text[:300]}")
    return data.get("id") or data.get("msg_id") or data.get("message_id")


async def send_all_digests(payload, recipient, app_id, client_secret):
    """Send a header message + per-digest messages with keyboards. Returns summary."""
    api_base = os.environ.get("QQ_API_BASE", "https://api.sgroup.qq.com")
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await refresh_access_token(session, app_id, client_secret)
        log.info("Got access token (len=%d)", len(token))

        sent = []
        # Use a per-recipient monotonic seq starting at int(time.time()) % 1000000
        # to avoid colliding with other senders; QQ bot requires msg_seq
        # to be a positive integer that strictly increases.
        seq = (int(time.time()) % 1_000_000)

        def next_seq():
            nonlocal seq
            seq += 1
            return seq

        # 1. Send the header
        if payload.get("header"):
            try:
                mid = await send_one_message(
                    session, api_base, token, recipient, payload["header"], msg_seq=next_seq()
                )
                log.info("Header sent: msg_id=%s", mid)
                sent.append(("header", mid))
            except Exception as e:
                log.error("Header send failed: %s", e)
                sent.append(("header", None))

        # 2. Send each digest as its own message with its keyboard
        for d in payload.get("digests", []):
            url = d.get("url", "")
            if url and len(url) > 32:
                from urllib.parse import urlparse as _up
                try:
                    p = _up(url)
                    host = p.netloc.replace("www.", "")
                    path = p.path.rstrip("/").split("/")[-1] if p.path else ""
                    if path and len(path) <= 24 and not path.startswith(("status", "papers")):
                        anchor = f"{host}/{path}"
                    else:
                        anchor = host
                except Exception:
                    anchor = url[:32]
                url_line = f"[{anchor}]({url})"
            else:
                url_line = url
            body = f"**{d['headline']}**\n\n{d['summary']}\n\n`{d['source']}`\n{url_line}"
            try:
                mid = await send_one_message(
                    session, api_base, token, recipient, body,
                    keyboard=d.get("keyboard"), msg_seq=next_seq(),
                )
                log.info("Digest #%d sent: msg_id=%s", d["id"], mid)
                sent.append((d["id"], mid))
            except Exception as e:
                log.error("Digest #%d send failed: %s", d["id"], e)
                sent.append((d["id"], None))

    return sent


def main():
    markdown, payload = parse_footer_from_stdin()

    # If no footer, treat the whole stdin as a single markdown message (legacy)
    if payload is None:
        if not markdown:
            log.info("Empty input, nothing to send")
            return 0
        payload = {
            "header": "",
            "digests": [
                {
                    "id": 0,
                    "headline": markdown.split("\n")[0] if markdown else "",
                    "summary": markdown,
                    "importance": 0,
                    "category": "other",
                    "url": "",
                    "source": "",
                    "keyboard": None,
                }
            ],
        }
        # In legacy mode, send as a single message
        payload["header"] = ""

    # Config
    app_id = os.environ.get("QQ_APP_ID")
    client_secret = os.environ.get("QQ_CLIENT_SECRET")
    recipient = os.environ.get("QQ_BOT_HOME_OPENID") or os.environ.get("QQBOT_HOME_CHANNEL")

    if not all([app_id, client_secret, recipient]):
        log.warning(
            "QQ creds missing (QQ_APP_ID, QQ_CLIENT_SECRET, QQ_BOT_HOME_OPENID) — "
            "falling back to local file dump at ~/.hermes/ai_news/last_qq_delivery.md"
        )
        fallback_path = Path.home() / ".hermes" / "ai_news" / "last_qq_delivery.md"
        fallback_path.write_text(markdown, encoding="utf-8")
        # also dump structured payload
        struct_path = fallback_path.with_suffix(".json")
        struct_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Wrote fallback to %s", fallback_path)
        # Also surface the markdown to stdout so the cron agent delivers
        # a visible (unformatted) message to the user, instead of a silent
        # no-op when QQ creds are missing.
        print(markdown, flush=True)
        return 0

    log.info("Sending to QQ recipient=%s with %d digests", recipient, len(payload.get("digests", [])))
    sent = asyncio.run(send_all_digests(payload, recipient, app_id, client_secret))

    # Summary to stderr (cron captures it for logs)
    ok = sum(1 for _, mid in sent if mid)
    fail = sum(1 for _, mid in sent if not mid)
    log.info("Sent: %d ok, %d failed", ok, fail)

    # On stdout, print nothing — the per-digest messages with keyboards
    # are already in the user's QQ via the direct API calls above, and
    # the cron agent (which reads our stdout as its own final response
    # via deliver: origin) would just duplicate the same content. Echoing
    # an empty stdout makes the cron agent return "silent" → no duplicate
    # message from the agent layer.
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
