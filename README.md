# Smart LLM Router

**Intelligent multi-backend LLM routing proxy with automatic tier-based model selection.**

Routes requests through the cheapest model capable of handling the task, with automatic failover.

```
Client (any OpenAI-compatible tool)
  |
  v
Smart Router (:4001)  ──  OpenAI-compatible API
  |
  ├── T0 FREE ──> Vertex AI (SA key)
  |                ├── Llama 4 Maverick/Scout (us-east5)
  |                ├── Qwen3-235B, DeepSeek V3/R1, Kimi K2, MiniMax M2 (global)
  |                ├── Gemini 2.5 Flash/Pro (us-central1)
  |                └── Embeddings, Imagen, Vision, Code Execution
  |
  ├── T0 FREE ──> Z.AI Direct
  |                └── GLM-5.1, GLM-5-Turbo, GLM-4.5
  |
  ├── T1 CHEAP ─> CCS CLIProxy (:8317)
  |                └── Claude Haiku 4.5, GPT-5.4-mini
  |
  ├── T2 MEDIUM -> CCS CLIProxy (:8317)
  |                └── Claude Sonnet 4.6, GPT-5.4
  |
  └── T3 PREMIUM> CCS CLIProxy (:8317)
                   └── Claude Opus 4.6
```

## Features

- **39 models** across 4 tiers (FREE / CHEAP / MEDIUM / PREMIUM)
- **Smart routing**: auto-detects task complexity via regex + word count heuristics
- **Explicit model selection**: pass `model=claude-opus-4-6` for direct dispatch
- **Round-robin T0**: distributes load across 5+ free models
- **Automatic failover**: T0 fail -> T1 -> T2 -> T3
- **Thinking model post-processing**: strips `<think>` tags, maps `reasoning_content` -> `content`
- **Streaming (SSE)**: full end-to-end streaming support with thinking model fixes
- **OpenAI-compatible**: drop-in replacement for any OpenAI SDK client
- **Multi-modal**: vision (image download + inlineData), audio (STT/TTS), embeddings, image generation
- **Tool endpoints**: `/v1/tools/search`, `/v1/tools/translate`, `/v1/tools/youtube`
- **Stats tracking**: persistent request/token/tier counters

## Quick Start

```bash
# Install dependencies
pip install google-auth google-cloud-aiplatform

# Optional
pip install youtube-transcript-api

# Configure
cp .env.example .env
# Edit .env with your keys

# Start
python smart-router.py

# Test
curl http://localhost:4001/health
curl http://localhost:4001/v1/models
curl -X POST http://localhost:4001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

## Configuration

| Env Var | Required | Description |
|---------|----------|-------------|
| `VERTEX_SA_KEY` | Yes | Path to GCP service account JSON key |
| `VERTEX_PROJECT` | Yes | GCP project ID |
| `ZAI_API_KEY` | For Z.AI | Z.AI API key for GLM models |
| `BRAVE_API_KEY` | Optional | Brave Search API key (2000 req/mo free) |
| `YOUTUBE_API_KEY` | Optional | YouTube Data API v3 key |

## Model Aliases

| Alias | Routes to |
|-------|-----------|
| `auto`, `smart` | Tier auto-detected from prompt |
| `free`, `vertex` | T0 round-robin |
| `cheap`, `haiku` | Claude Haiku 4.5 |
| `medium`, `sonnet` | Claude Sonnet 4.6 |
| `expensive`, `opus` | Claude Opus 4.6 |

## Routing Logic

```
1. Check T3 patterns (architect, design system, security audit)
2. Check T2 patterns (debug, refactor, implement, write test)
3. Check T0 patterns (hi, explain, translate) + word count < 15
4. Context heuristics: >5000 words or 8+ code blocks -> T3
5. Default: T1 (cheap Claude)
```

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/v1/chat/completions` | POST | Main chat endpoint |
| `/v1/embeddings` | POST | Text embeddings |
| `/v1/images/generations` | POST | Image generation (Imagen 3) |
| `/v1/audio/transcriptions` | POST | Speech-to-text |
| `/v1/audio/speech` | POST | Text-to-speech |
| `/v1/tools/search` | POST | Web search |
| `/v1/tools/translate` | POST | Translation |
| `/v1/tools/youtube` | POST | YouTube search/transcript/analyze |
| `/v1/models` | GET | List all models |
| `/health` | GET | Health check |
| `/stats` | GET | Usage statistics |

## Integration with Droid/Claude Code

Point your AI coding assistant to the router:

```json
{
  "baseUrl": "http://localhost:4001/v1",
  "apiKey": "router-managed",
  "provider": "openai"
}
```

Use `update-droid-settings.py` to auto-configure Droid CLI.

## License

MIT
