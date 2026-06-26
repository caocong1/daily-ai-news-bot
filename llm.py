"""LLM client — wraps whatever provider Hermes is configured with.

Uses `hermes chat -q` (single-query mode) so we don't need TTY.
Quiet flag (-Q) keeps output clean; max-turns=1 disables tool loops.
"""
import json
import re
import subprocess
import sys


def call_llm(prompt: str, system: str = "", temperature: float = 0.2,
             max_tokens: int = 2000, json_mode: bool = False) -> str:
    """Call the active Hermes LLM via single-query subprocess."""
    full = prompt
    if system:
        full = f"<system>\n{system}\n</system>\n\n" + full
    if json_mode:
        full += "\n\nRespond with valid JSON only. No prose, no markdown fences, no commentary."
    try:
        result = subprocess.run(
            ["hermes", "chat", "-q", full, "-Q", "--max-turns", "1"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            sys.stderr.write(f"[llm] hermes exit={result.returncode} stderr={result.stderr[:300]}\n")
            return ""
        out = result.stdout.strip()
        lines = [l for l in out.split("\n") if not l.startswith("session_id:")]
        return "\n".join(lines).strip()
    except subprocess.TimeoutExpired:
        sys.stderr.write("[llm] hermes chat timeout\n")
        return ""
    except FileNotFoundError:
        sys.stderr.write("[llm] hermes CLI not on PATH\n")
        return ""


def call_llm_json(prompt: str, system: str = "", **kwargs):
    """Parse JSON from LLM. Tolerant: strip fences, find first balanced {} or []."""
    raw = call_llm(prompt, system=system, json_mode=True, **kwargs)
    if not raw:
        return None
    # Strip ```json fences (LLM keeps wrapping responses)
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    raw = re.sub(r"\n?```\s*$", "", raw)

    # Try whole-string parse first
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = None

    if result is not None:
        # If LLM returned a single dict where a list was expected, wrap it.
        # Caller can introspect.
        return result

    # Fallback: find outermost balanced {} or []
    for open_c, close_c in [("[", "]"), ("{", "}")]:
        first = raw.find(open_c)
        if first == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(first, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    candidate = raw[first:i+1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    sys.stderr.write(f"[llm] JSON parse failed: {raw[:400]}\n")
    return None
