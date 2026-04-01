"""Update Droid settings.json to route ALL models through Smart Router :4001.

Changes:
1. Claude models: anthropic → openai provider, baseUrl → router
2. GLM models: Z.AI direct → router
3. GPT/Gemini: CCS → router
4. Add new free models from router (Llama 4, DeepSeek, Qwen3, auto)

Creates backup at settings.json.bak before modifying.
"""
import json
import shutil
from pathlib import Path

SETTINGS = Path(r"C:\Users\WORK PC\.factory\settings.json")
ROUTER_BASE = "http://localhost:4001/v1"

def main():
    # Backup
    backup = SETTINGS.with_suffix(".json.bak")
    shutil.copy2(SETTINGS, backup)
    print(f"Backup: {backup}")

    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    models = data.get("customModels", [])

    # Update existing models to route through smart router
    for m in models:
        m["baseUrl"] = ROUTER_BASE
        m["apiKey"] = "router-managed"
        # Claude models: switch from anthropic to openai provider
        # (router speaks OpenAI format, CCS handles translation)
        if m.get("provider") == "anthropic":
            m["provider"] = "openai"

    # Add new free models from the router that Droid doesn't have yet
    existing_models = {m["model"] for m in models}
    new_models = [
        {
            "model": "auto",
            "displayName": "Smart Router (Auto)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "llama-4-maverick",
            "displayName": "Llama 4 Maverick (FREE)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "deepseek-v3",
            "displayName": "DeepSeek V3.2 (FREE)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "qwen3-235b",
            "displayName": "Qwen3 235B (FREE)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "gemini-2.5-flash",
            "displayName": "Gemini 2.5 Flash (FREE)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "deepseek-r1",
            "displayName": "DeepSeek R1 (FREE, thinking)",
            "noImageSupport": False,
            "provider": "openai",
        },
        {
            "model": "kimi-k2",
            "displayName": "Kimi K2 (FREE, thinking)",
            "noImageSupport": False,
            "provider": "openai",
        },
    ]

    idx = len(models)
    for nm in new_models:
        if nm["model"] not in existing_models:
            nm["baseUrl"] = ROUTER_BASE
            nm["apiKey"] = "router-managed"
            nm["id"] = f"custom:{nm['model']}-{idx}"
            nm["index"] = idx
            models.append(nm)
            idx += 1
            print(f"  Added: {nm['model']} ({nm['displayName']})")

    data["customModels"] = models
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nUpdated {len(models)} models → all routing through {ROUTER_BASE}")
    print("Models:")
    for m in models:
        print(f"  {m['model']:25s} provider={m['provider']} baseUrl={m['baseUrl']}")


if __name__ == "__main__":
    main()
