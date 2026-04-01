"""
UNIVERSAL SYSTEM AUDIT -- Verifica TOATE punctele de consum LLM pe sistem.

Inventarizeaza:
  1. Ce tool-uri/apps consuma LLM
  2. Ce model folosesc (hardcoded vs configurat)
  3. Prin ce backend merg (CCS direct, Vertex, altceva)
  4. Ce trece prin router si ce NU
  5. Ce se poate redirecta si cum

Proiecte scanate: tot F:\ + user config dirs
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

ISSUES = []
OK_ITEMS = []
WARNINGS = []

def check(name, ok, detail=""):
    if ok:
        OK_ITEMS.append((name, detail))
        print(f"  [OK]   {name}: {detail}")
    else:
        ISSUES.append((name, detail))
        print(f"  [FAIL] {name}: {detail}")

def warn(name, detail):
    WARNINGS.append((name, detail))
    print(f"  [WARN] {name}: {detail}")

def http_get(url, headers=None, timeout=5):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return None

def http_post(url, payload, headers=None, timeout=15):
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", **(headers or {})
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


print("=" * 95)
print("  UNIVERSAL SYSTEM AUDIT -- LLM Usage & Router Compliance")
print("=" * 95)

# ══════════════════════════════════════════════════════════════════
# 1. BACKEND AVAILABILITY
# ══════════════════════════════════════════════════════════════════
print("\n--- 1. BACKEND AVAILABILITY ---\n")

# CCS Proxy
ccs = http_get("http://localhost:8317/v1/models", {"Authorization": "Bearer ccs-internal-managed"})
check("CCS CLIProxy (:8317)", ccs is not None and "data" in ccs,
      f"{len(ccs['data'])} models available" if ccs and "data" in ccs else "NOT RUNNING or auth failed")

# Vertex/DOT PC
vertex = http_get("http://192.168.10.38:4000/v1/models", {"Authorization": "Bearer sk-vertex"})
check("Vertex DOT PC (:4000)", vertex is not None and "data" in vertex,
      f"{len(vertex['data'])} models available" if vertex and "data" in vertex else "NOT REACHABLE")

# Router
router_health = http_get("http://localhost:4001/health")
check("Smart Router (:4001)", router_health is not None,
      "Running" if router_health else "NOT RUNNING -- start with: python F:\\llm-router\\smart-router.py --port 4001")

# ══════════════════════════════════════════════════════════════════
# 2. ENVIRONMENT VARIABLES
# ══════════════════════════════════════════════════════════════════
print("\n--- 2. ENVIRONMENT VARIABLES ---\n")

openai_base = os.environ.get("OPENAI_API_BASE", "")
check("OPENAI_API_BASE", openai_base == "http://localhost:8317/v1",
      f"Points to: {openai_base}" if openai_base else "NOT SET")

if openai_base and "8317" in openai_base and "4001" not in openai_base:
    warn("OPENAI_API_BASE", "Points to CCS (:8317) directly, NOT through router (:4001). "
         "Tools using this env var bypass routing!")

openai_key = os.environ.get("OPENAI_API_KEY", "")
check("OPENAI_API_KEY", bool(openai_key), "Set" if openai_key else "NOT SET")

google_key = os.environ.get("GOOGLE_API_KEY", "")
check("GOOGLE_API_KEY", bool(google_key), "Set (used by some Gemini tools)" if google_key else "NOT SET")

anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
if anthropic_key:
    warn("ANTHROPIC_API_KEY", "Set -- tools using this go DIRECTLY to Anthropic API (not through CCS/router)")
else:
    check("ANTHROPIC_API_KEY", True, "Not set (correct -- everything through CCS proxy)")

# ══════════════════════════════════════════════════════════════════
# 3. CLAUDE CODE / FACTORY DROID
# ══════════════════════════════════════════════════════════════════
print("\n--- 3. CLAUDE CODE / FACTORY DROID ---\n")

claude_started = os.environ.get("CLAUDE_STARTED", "")
check("Claude Code session", claude_started == "1", "Active (CLAUDE_STARTED=1)")

# Check bash function aliases
droid_fn = os.environ.get("BASH_FUNC_droid%%", "")
droid_opus_fn = os.environ.get("BASH_FUNC_droid-opus%%", "")
droid_haiku_fn = os.environ.get("BASH_FUNC_droid-haiku%%", "")

if "sonnet" in droid_fn:
    check("droid alias", True, "Default: claude-sonnet-4-6 (GOOD - not Opus)")
elif "opus" in droid_fn:
    warn("droid alias", "Default droid uses OPUS -- wasteful for most tasks! Change to sonnet")
else:
    check("droid alias", True, f"Custom: {droid_fn[:60]}")

if droid_opus_fn:
    check("droid-opus alias", True, "Available for explicit Opus usage")
if droid_haiku_fn:
    check("droid-haiku alias", True, "Available for cheap tasks")

# ══════════════════════════════════════════════════════════════════
# 4. CCS OAUTH ACCOUNTS
# ══════════════════════════════════════════════════════════════════
print("\n--- 4. CCS OAUTH ACCOUNTS ---\n")

auth_dir = Path(os.path.expanduser("~")) / ".ccs" / "cliproxy" / "auth"
if auth_dir.exists():
    accounts = [f.stem for f in auth_dir.glob("*.json") if not f.stem.endswith(".bak")]
    claude_accounts = [a for a in accounts if a.startswith("claude-")]
    gemini_accounts = [a for a in accounts if a.startswith("gemini-")]
    codex_accounts = [a for a in accounts if a.startswith("codex-")]

    check("Claude OAuth accounts", len(claude_accounts) > 0,
          f"{len(claude_accounts)} accounts: {', '.join(a.replace('claude-','') for a in claude_accounts)}")
    if gemini_accounts:
        check("Gemini OAuth accounts", True,
              f"{len(gemini_accounts)}: {', '.join(a.replace('gemini-','') for a in gemini_accounts)}")
    else:
        warn("Gemini OAuth", "No Gemini OAuth -- Gemini goes through Vertex (DOT PC) only")
    if codex_accounts:
        check("Codex/GPT accounts", True, f"{len(codex_accounts)}")
else:
    warn("CCS auth dir", f"Not found at {auth_dir}")

# ══════════════════════════════════════════════════════════════════
# 5. CONSUMER APPS -- Hardcoded models
# ══════════════════════════════════════════════════════════════════
print("\n--- 5. CONSUMER APPS -- Model Usage ---\n")

# telegram-claude
tg_index = Path("F:/telegram-claude/index.js")
if tg_index.exists():
    content = tg_index.read_text(encoding="utf-8", errors="replace")
    models_found = re.findall(r"model:\s*['\"]([^'\"]+)['\"]", content)
    endpoints = re.findall(r"fetch\(['\"]([^'\"]+)['\"]", content)
    for m in models_found:
        if "opus" in m.lower():
            warn("telegram-claude/index.js", f"Hardcoded model: {m} (OPUS! wasteful for chat)")
        elif "sonnet" in m.lower():
            check("telegram-claude model", True, f"Uses {m} (reasonable for chat)")
        else:
            check("telegram-claude model", True, f"Uses {m}")
    for ep in endpoints:
        if "8317" in ep:
            check("telegram-claude endpoint", True, f"Goes through CCS: {ep}")
        elif "4001" in ep:
            check("telegram-claude endpoint", True, f"Goes through ROUTER: {ep}")
        else:
            warn("telegram-claude endpoint", f"Direct endpoint: {ep} -- bypasses router")

# freight-bol-v2
bol_env = Path("F:/freight-bol-v2/.env")
if bol_env.exists():
    env_content = bol_env.read_text(encoding="utf-8", errors="replace")
    if "localhost:8317" in env_content:
        check("freight-bol-v2 .env", True, "OPENAI_API_BASE -> CCS (:8317)")
    if "OPENAI_BASE_URL" in env_content and "openai.com" in env_content:
        warn("freight-bol-v2 .env", "Also has OPENAI_BASE_URL pointing to openai.com directly!")

# Scan all Python files in freight-bol for model references
bol_dir = Path("F:/freight-bol-v2")
if bol_dir.exists():
    py_files = list(bol_dir.glob("*.py"))
    opus_files = []
    for pf in py_files:
        try:
            content = pf.read_text(encoding="utf-8", errors="replace")
            if "opus" in content.lower() and "model" in content.lower():
                opus_files.append(pf.name)
        except Exception:
            pass
    if opus_files:
        warn("freight-bol Python scripts", f"{len(opus_files)} files reference Opus: {', '.join(opus_files[:5])}")
    else:
        check("freight-bol Python scripts", True, "No Opus references found in Python scripts")

# ══════════════════════════════════════════════════════════════════
# 6. CCS PROXY CONFIG -- Model mapping / routing
# ══════════════════════════════════════════════════════════════════
print("\n--- 6. CCS PROXY CONFIG ---\n")

ccs_config = Path(os.path.expanduser("~")) / ".ccs" / "cliproxy" / "config.yaml"
if ccs_config.exists():
    cfg = ccs_config.read_text(encoding="utf-8", errors="replace")
    if "quota-exceeded" in cfg and "switch-project: true" in cfg:
        check("CCS quota failover", True, "Auto-switch accounts on 429 (quota exceeded)")
    else:
        warn("CCS quota failover", "switch-project not enabled -- manual account switching needed")

    if "disable-cooling: true" in cfg:
        check("CCS cooling", True, "Cooling disabled (no unnecessary delays)")

    if "usage-statistics-enabled: true" in cfg:
        check("CCS usage stats", True, "Tracking enabled")
    else:
        warn("CCS usage stats", "Disabled -- can't monitor consumption")
else:
    warn("CCS config", "Not found")

# ══════════════════════════════════════════════════════════════════
# 7. LIVE ROUTING TESTS (if router is running)
# ══════════════════════════════════════════════════════════════════
print("\n--- 7. LIVE ROUTING VERIFICATION ---\n")

if router_health:
    live_tests = [
        ("Simple task", "auto", "explain what this variable does", "gemini"),
        ("Medium task", "auto", "debug this function that crashes on null input", "sonnet"),
        ("Complex task", "auto", "architect a distributed system for document processing", "opus"),
    ]
    for label, model, prompt, expected_model_substr in live_tests:
        result = http_post("http://localhost:4001/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 20
        })
        if "error" not in result or "choices" in result:
            used_model = result.get("model", "unknown")
            ok = expected_model_substr in used_model.lower()
            check(f"Route: {label}", ok,
                  f"Routed to {used_model}" + ("" if ok else f" (expected {expected_model_substr})"))
        else:
            check(f"Route: {label}", False, f"Error: {str(result.get('error',''))[:60]}")
else:
    warn("Live routing", "Router not running -- skipping live tests")


# ══════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════
print(f"\n{'=' * 95}")
print(f"  AUDIT SUMMARY")
print(f"{'=' * 95}")
print(f"\n  OK:       {len(OK_ITEMS)}")
print(f"  WARNINGS: {len(WARNINGS)}")
print(f"  FAILURES: {len(ISSUES)}")

if WARNINGS:
    print(f"\n  --- WARNINGS (optimization opportunities) ---")
    for name, detail in WARNINGS:
        print(f"    [{name}] {detail}")

if ISSUES:
    print(f"\n  --- FAILURES (must fix) ---")
    for name, detail in ISSUES:
        print(f"    [{name}] {detail}")

# Recommendations
print(f"\n  --- RECOMMENDATIONS ---")
if any("4001" not in d for _, d in WARNINGS + ISSUES if "OPENAI_API_BASE" in _):
    print(f"    1. REDIRECT OPENAI_API_BASE to router: export OPENAI_API_BASE=http://localhost:4001/v1")
    print(f"       This makes ALL tools using OpenAI-compat API go through smart routing")

if any("opus" in d.lower() for _, d in WARNINGS):
    print(f"    2. REDUCE OPUS usage: change hardcoded 'opus' to 'auto' or 'sonnet' where possible")

if not router_health:
    print(f"    3. START ROUTER: python F:\\llm-router\\smart-router.py --port 4001")

print()
