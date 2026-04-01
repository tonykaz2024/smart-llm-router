"""
Real-World Routing Tests -- Bazate pe proiectele tale reale:
  - freight-bol-v2 (Python OCR/document processing)
  - telegram-claude (Node.js Telegram bot)
  - feishin-crazy (Electron + React + TypeScript music player)

Testeaza ca routerul trimite fiecare tip de task la modelul CORECT.
"""
import http.server
import json
import sys
import threading
import time
import urllib.request
import importlib.util
from pathlib import Path

# Load router module
spec = importlib.util.spec_from_file_location("router", str(Path(__file__).parent / "smart-router.py"))
router = importlib.util.module_from_spec(spec)
router.LISTEN_PORT = 4002
spec.loader.exec_module(router)

server = http.server.ThreadingHTTPServer(("0.0.0.0", 4002), router.RouterHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
time.sleep(1)

URL = "http://localhost:4002/v1/chat/completions"

def send(model, prompt, max_tokens=80):
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }).encode()
    req = urllib.request.Request(URL, data=payload, headers={"Content-Type": "application/json"})
    start = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        elapsed = time.time() - start
        model_used = data.get("model", "?")
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        safe = (content or "(empty)")[:60].encode("ascii", "replace").decode("ascii")
        usage = data.get("usage", {})
        return {
            "ok": True, "model": model_used, "time": elapsed,
            "content": safe, "in_tok": usage.get("prompt_tokens", 0),
            "out_tok": usage.get("completion_tokens", 0)
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


# === REAL TASK DEFINITIONS ===
# Format: (category, expected_tier_name, model_param, prompt)
REAL_TASKS = [
    # ─── TIER 0 (FREE - Gemini) -- Lucruri banale, zilnice ───
    ("freight-bol", "FREE", "auto",
     "explain what _hash function does in freightbol_v34_direct.py"),

    ("telegram-bot", "FREE", "auto",
     "what is CLAUDE_TIMEOUT used for in index.js"),

    ("general", "FREE", "auto",
     "rename variable res to apiResponse"),

    ("general", "FREE", "auto",
     "translate this error message to Romanian: 'Connection refused'"),

    ("feishin", "FREE", "auto",
     "what does .prettierrc.yaml do"),

    # ─── TIER 2 (MEDIUM - Sonnet) -- Coding tasks reale ───
    ("freight-bol", "MEDIUM", "auto",
     "debug this function that processes BOL images - the _fix_exif method "
     "crashes when EXIF orientation tag is missing from the PIL Image object"),

    ("telegram-bot", "MEDIUM", "auto",
     "refactor the callClaude function in telegram-claude/index.js to add "
     "retry logic with exponential backoff when the API returns 429 or 503"),

    ("freight-bol", "MEDIUM", "auto",
     "write unit tests for the _font function that loads TrueType fonts "
     "with fallback to default when the font file is missing"),

    ("feishin", "MEDIUM", "auto",
     "implement a new API endpoint handler for the music player that "
     "fetches album artwork from multiple sources with caching"),

    ("telegram-bot", "MEDIUM", "auto",
     "fix bug in the Telegram bot where messages longer than 4096 characters "
     "crash the bot instead of being split into multiple messages"),

    # ─── TIER 3 (PREMIUM - Opus) -- Arhitectura, design, audit ───
    ("freight-bol", "PREMIUM", "auto",
     "design system architecture for a scalable document processing pipeline "
     "that handles BOL OCR extraction across multiple formats - handwritten, "
     "printed, mixed - with quality validation and human review workflow"),

    ("telegram-bot", "PREMIUM", "auto",
     "comprehensive security audit of the Telegram bot authentication flow: "
     "analyze the ALLOWED_USERS whitelist approach, the API key storage in "
     ".env, the OAuth token handling, and identify all potential attack vectors"),

    ("feishin", "PREMIUM", "auto",
     "architect a plugin system for the Electron music player that allows "
     "third-party extensions to add new music sources, custom visualizers, "
     "and theme support - design the API, lifecycle, and sandboxing model"),

    ("freight-bol", "PREMIUM", "auto",
     "research the trade-off between using Tesseract OCR vs Google Document AI "
     "vs Gemini Vision for handwritten BOL extraction at scale - compare accuracy, "
     "cost per page, latency, and multi-language support for a production deployment"),
]


def tier_from_model(model_name):
    if "gemini" in model_name:
        return "FREE"
    if "haiku" in model_name or "mini" in model_name:
        return "CHEAP"
    if "sonnet" in model_name:
        return "MEDIUM"
    if "opus" in model_name:
        return "PREMIUM"
    return "UNKNOWN"


print("=" * 95)
print("  REAL-WORLD ROUTING TESTS -- Proiecte: freight-bol, telegram-claude, feishin-crazy")
print("=" * 95)
print(f"\n  Running {len(REAL_TASKS)} real coding tasks through the router...\n")

passed = 0
failed = 0
total_time = 0
tier_counts = {"FREE": 0, "CHEAP": 0, "MEDIUM": 0, "PREMIUM": 0}

for category, expected_tier, model, prompt in REAL_TASKS:
    result = send(model, prompt)
    if result["ok"]:
        actual_tier = tier_from_model(result["model"])
        ok = actual_tier == expected_tier
        total_time += result["time"]
        tier_counts[actual_tier] = tier_counts.get(actual_tier, 0) + 1
        status = "PASS" if ok else "WRONG"
        if ok:
            passed += 1
        else:
            failed += 1
        short_prompt = prompt[:50].replace("\n", " ")
        print(f"  [{status:5s}] {category:14s} expect={expected_tier:7s} got={actual_tier:7s} "
              f"| {result['time']:.1f}s | {result['model'][:25]:25s} | \"{short_prompt}...\"")
    else:
        failed += 1
        print(f"  [ERROR] {category:14s} | {result['error']}")

print(f"\n{'=' * 95}")
print(f"  RESULTS: {passed}/{passed + failed} correct routing ({failed} wrong)")
print(f"  Total time: {total_time:.1f}s | Avg: {total_time / max(passed + failed, 1):.1f}s per task")
print(f"{'=' * 95}")

print(f"\n  TIER DISTRIBUTION (how your requests would be routed):")
total = sum(tier_counts.values())
for tier, count in sorted(tier_counts.items(), key=lambda x: ["FREE", "CHEAP", "MEDIUM", "PREMIUM"].index(x[0])):
    pct = count * 100 // max(total, 1)
    bar = "#" * (pct // 2)
    cost = {"FREE": "$0 (Vertex)", "CHEAP": "$ (Haiku)", "MEDIUM": "$$ (Sonnet)", "PREMIUM": "$$$ (Opus)"}[tier]
    print(f"    {tier:8s}: {count:2d} ({pct:2d}%) {bar:30s} {cost}")

quota_saved = tier_counts.get("FREE", 0)
quota_total = sum(tier_counts.values())
print(f"\n  QUOTA IMPACT:")
print(f"    Requests on FREE (Gemini/Vertex): {quota_saved}/{quota_total} = {quota_saved * 100 // max(quota_total, 1)}% ZERO quota cost")
print(f"    Opus usage: {tier_counts.get('PREMIUM', 0)}/{quota_total} = {tier_counts.get('PREMIUM', 0) * 100 // max(quota_total, 1)}% (only when truly needed)")
print(f"    vs ALL on Opus: 100% -> now only {tier_counts.get('PREMIUM', 0) * 100 // max(quota_total, 1)}% = {100 - tier_counts.get('PREMIUM', 0) * 100 // max(quota_total, 1)}% less Opus usage")

# Get stats
try:
    resp = urllib.request.urlopen("http://localhost:4002/stats", timeout=5)
    stats = json.loads(resp.read())
    print(f"\n  CUMULATIVE STATS:")
    print(f"    Total tokens: in={stats['tokens']['input']} out={stats['tokens']['output']}")
except Exception:
    pass

server.shutdown()
print(f"\n  Done. Router stopped.")
