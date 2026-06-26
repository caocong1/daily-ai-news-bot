"""Login helper: save session cookies for primary sources that need login.

Usage:
    # After logging into x.com/reddit via Chrome DevTools MCP, dump cookies:
    python3 login.py dump-cookies x.com/OpenAI  # prints cookies the user pastes into a JSON file

    # Or set cookies from a JSON file you have:
    python3 login.py set x.com/OpenAI /path/to/cookies.json

    # Or set cookies from a single header string ("name1=value1; name2=value2"):
    python3 login.py set-cookie x.com/OpenAI "auth_token=abc123; ct0=def456"

    # Show which sources need login:
    python3 login.py list

Cookie file format: JSON list of {name, value, domain} objects.
Same format that Chrome DevTools MCP network.getCookies returns.
"""
import sys
import os
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_news import db


COOKIE_DIR = Path.home() / ".hermes" / "ai_news" / "sessions"
COOKIE_DIR.mkdir(parents=True, exist_ok=True)


def cookie_path(domain):
    safe = domain.replace("/", "_").replace(":", "_")
    return COOKIE_DIR / f"{safe}.json"


def cmd_list():
    pending = db.list_pending_login_sources()
    if not pending:
        print("No sources need login. All primary sources either have sessions or don't need them.")
        return
    print(f"Sources needing login ({len(pending)}):")
    for s in pending:
        path = cookie_path(s["domain"])
        exists = "✓ saved" if path.exists() else "✗ no session"
        print(f"  {s['domain']:35s} {s['display_name']:30s} {exists}")
        print(f"    url: {s['url']}")
        print(f"    expected session path: {path}")
    print()
    print("To enable one of these, log in via Chrome browser, then:")
    print("  1. Open DevTools → Application → Cookies → copy values")
    print("  2. python3 login.py set <domain> /path/to/cookies.json")
    print()
    print("Or paste Cookie header directly:")
    print("  python3 login.py set-cookie <domain> \"name1=value1; name2=value2\"")


def cmd_set(domain, json_file):
    path = Path(json_file)
    if not path.exists():
        print(f"ERROR: {path} not found")
        return 1
    cookies = json.loads(path.read_text())
    if not isinstance(cookies, list):
        print("ERROR: cookies file must be a JSON list of {name, value, domain}")
        return 1
    target = cookie_path(domain)
    target.write_text(json.dumps(cookies, indent=2))
    db.set_primary_session_path(domain, str(target))
    print(f"✓ Saved {len(cookies)} cookies for {domain} → {target}")
    return 0


def cmd_set_cookie(domain, cookie_header):
    """Parse 'name1=value1; name2=value2' into a cookies list."""
    cookies = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({"name": name.strip(), "value": value.strip(), "domain": domain})
    target = cookie_path(domain)
    target.write_text(json.dumps(cookies, indent=2))
    db.set_primary_session_path(domain, str(target))
    print(f"✓ Saved {len(cookies)} cookies for {domain} → {target}")
    print()
    print("Hint: most X/Reddit logins need a few specific cookies to work:")
    print("  X (Twitter):     auth_token, ct0, twid, gt (and the secure ones)")
    print("  Reddit:          reddit_session, token_v2")
    return 0


def cmd_dump_prompt(domain):
    """Print instructions for dumping cookies from the user's browser session."""
    print(f"To save cookies for {domain}:")
    print()
    print("OPTION A — Chrome DevTools (recommended):")
    print("  1. Open Chrome, log into the site")
    print("  2. DevTools → Application → Storage → Cookies → click the domain")
    print("  3. For each cookie, copy name and value, OR use the network tab trick:")
    print("     - DevTools → Network → make any request → click it → Headers")
    print("     - Right-click the 'cookie:' request header → Copy value")
    print("  4. Save to a JSON file like this:")
    print()
    print('     [{"name": "auth_token", "value": "xxx", "domain": ".x.com"},')
    print('      {"name": "ct0", "value": "yyy", "domain": ".x.com"}]')
    print()
    print(f"  5. python3 login.py set {domain} /path/to/cookies.json")
    print()
    print("OPTION B — paste Cookie header value:")
    print(f"  python3 login.py set-cookie {domain} \"auth_token=xxx; ct0=yyy\"")
    print()
    print("OPTION C — Chrome DevTools MCP (we'll do it for you):")
    print("  Just tell me 'login to x.com and save cookies for OpenAI'")
    print("  I'll use the browser MCP to log in and save the session.")


def main():
    if len(sys.argv) < 2:
        cmd_list()
        return 0
    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "set" and len(sys.argv) >= 4:
        return cmd_set(sys.argv[2], sys.argv[3])
    elif cmd == "set-cookie" and len(sys.argv) >= 4:
        return cmd_set_cookie(sys.argv[2], sys.argv[3])
    elif cmd == "dump-prompt" and len(sys.argv) >= 3:
        cmd_dump_prompt(sys.argv[2])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
