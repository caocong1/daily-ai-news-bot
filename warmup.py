"""Quick per-source warmup — sequential, 30s cap each."""
import sys
import json
import signal
sys.path.insert(0, "/home/sorawatcher/.hermes/ai_news")
from sources import REGISTRY


def alarm(n):
    def _h(*a):
        raise TimeoutError(f"timed out after {n}s")
    return _h


results = {}
for name, fn in REGISTRY.items():
    signal.signal(signal.SIGALRM, alarm(30))
    signal.alarm(30)
    try:
        items = fn()
        results[name] = {"ok": True, "count": len(items), "sample": items[0]["title"][:80] if items else ""}
    except Exception as e:
        results[name] = {"ok": False, "error": str(e)[:200]}
    finally:
        signal.alarm(0)

print(json.dumps(results, indent=2, ensure_ascii=False))
