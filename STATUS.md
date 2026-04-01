# Smart LLM Router v2.4 — STATUS

**Data verificare**: 2026-04-01
**Stare**: FUNCTIONAL — pornit pe port 4001

---

## Ce funcționează

### Core routing (COMPLET)
- `/v1/chat/completions` — endpoint principal cu fallback T0→T3
- `/v1/embeddings` — text-embedding-004/005 + multilingv
- `/v1/images/generations` — Imagen 3 (OpenAI compat)
- `/v1/tools/youtube` — YouTube: search (cu YOUTUBE_API_KEY), transcript (youtube-transcript-api), analyze (Gemini)
- `/v1/tools/search` — Web search: Brave (cu BRAVE_API_KEY) sau Gemini grounding fallback
- `/v1/tools/translate` — Traducere via Gemini (zero IAM extra)
- `/v1/audio/transcriptions` — STT via Gemini audio inlineData (max 20MB)
- `/v1/audio/speech` — TTS via Cloud TTS Chirp 3 HD (necesită IAM: roles/texttospeech.client)
- `/v1/models` — listează toate 36 modelele + aliasurile
- `/stats` — statistici persistente (router-stats.json)
- `/health` — health check simplu

### Tier routing logic

| Tier | Model | Backend | Trigger |
|------|-------|---------|---------|
| T0 FREE | Llama 4 Maverick (us-east5) | Vertex SA key | hi, explain, < 15 cuvinte |
| T0 FREE | Qwen3-235B / DeepSeek-V3 / Kimi-K2 (global MaaS) | Vertex SA key | round-robin T0 |
| T0 FREE | Gemini 2.5 Flash (us-central1) | Vertex SA key | fallback T0 |
| T1 CHEAP | claude-haiku-4-5-20251001 | CCS CLIProxy :8317 | cuvinte 15-50 |
| T2 MEDIUM | claude-sonnet-4-6 | CCS CLIProxy :8317 | debug, refactor, implement |
| T3 PREMIUM | claude-opus-4-6 | CCS CLIProxy :8317 | architect, security audit |

### Fallback chain
- T0 fail → T1 → T2 → T3 automat (pe HTTPError)

---

## Arhitectura (v2.4)

```
Client
  └─> Smart Router :4001
        ├─ T0 FREE ──> Vertex AI SA key (F:\vertex-ai-lab\vertex-ai-key.json)
        │               ├─ us-east5 MaaS:  Llama 4 Maverick/Scout
        │               ├─ global MaaS:    Qwen3-235B, DeepSeek-V3, GLM-4.7/5, Kimi-K2, MiniMax-M2
        │               ├─ us-central1 MaaS: DeepSeek R1-0528
        │               ├─ us-central1 Gemini API: Gemini 2.5 Flash/Pro, gemini-vision, gemini-grounded, gemini-code-exec
        │               ├─ us-central1 Embeddings: text-embedding-004/005, multilingv
        │               └─ us-central1 Imagen: Imagen 3 / Imagen 3 Fast
        ├─ T1 CHEAP ─> CCS CLIProxy :8317 ──> Claude Haiku 4.5
        ├─ T2 MEDIUM -> CCS CLIProxy :8317 ──> Claude Sonnet 4.6
        ├─ T3 PREMIUM> CCS CLIProxy :8317 ──> Claude Opus 4.6
        │
        ├─ /v1/tools/search    ──> Brave API (BRAVE_API_KEY) || Gemini grounding fallback
        ├─ /v1/tools/translate ──> Gemini Flash (zero IAM extra, zero cost)
        ├─ /v1/tools/youtube   ──> youtube-transcript-api (local) + YouTube Data API (search)
        ├─ /v1/audio/transcriptions ──> Gemini audio inlineData STT (max 20MB)
        └─ /v1/audio/speech    ──> Cloud TTS Chirp 3 HD (needs roles/texttospeech.client)
```

**Vertex project**: `vertex-lab-1774842110`
**SA key**: `F:\vertex-ai-lab\vertex-ai-key.json`
**Token cache**: 55 min, thread-safe

---

## Modele disponibile (36 total, model= parameter)

| Alias | Model real | Endpoint | Tier |
|-------|-----------|---------|------|
| `auto`, `smart` | detectare automată | - | - |
| `free`, `gemini` | llama-4-maverick | T0 round-robin | T0 |
| `llama-4-maverick` | meta/llama-4-maverick-...-maas | us-east5 | T0 |
| `llama-4-scout` | meta/llama-4-scout-...-maas | us-east5 | T0 |
| `qwen3-235b` | qwen/qwen3-235b-...-maas | global | T0 |
| `qwen3-coder` | qwen/qwen3-coder-480b-...-maas | global | T0 |
| `qwen3-80b` | qwen/qwen3-next-80b-...-maas | global | T0 |
| `qwen3-80b-think` | qwen/qwen3-next-80b-...-thinking-maas | global | T0 |
| `deepseek-v3` | deepseek-ai/deepseek-v3.2-maas | global | T0 |
| `deepseek-r1` | deepseek-ai/deepseek-r1-0528-maas | us-central1 | T0 |
| `glm-4.7` | zai-org/glm-4.7-maas | global | T0 |
| `glm-5` | zai-org/glm-5-maas | global | T0 |
| `kimi-k2` | moonshotai/kimi-k2-thinking-maas | global | T0 |
| `minimax-m2` | minimaxai/minimax-m2-maas | global | T0 |
| `gpt-oss` | openai/gpt-oss-120b-maas | global | T0 |
| `gemini-2.5-flash` | gemini-2.5-flash | us-central1 | T0 |
| `gemini-2.5-pro` | gemini-2.5-pro | us-central1 | T0 |
| `gemini-grounded` | gemini-2.5-flash + googleSearch | us-central1 | T0 |
| `gemini-code-exec` | gemini-2.5-flash + code_execution | us-central1 | T0 |
| `gemini-vision` | gemini-2.5-flash (multimodal, downloads images locally) | us-central1 | T0 |
| `text-embedding-004` | text-embedding-004 | us-central1 | T0 |
| `text-embedding-005` | text-embedding-005 | us-central1 | T0 |
| `embedding` | text-embedding-005 (default) | us-central1 | T0 |
| `text-multilingual-embedding-002` | text-multilingual-embedding-002 | us-central1 | T0 |
| `imagen-3` | imagen-3.0-generate-002 | us-central1 | IMAGEN |
| `imagen-3-fast` | imagen-3.0-fast-generate-001 | us-central1 | IMAGEN |
| `youtube-search` | YouTube Data API v3 | API key | TOOL |
| `cheap`, `haiku` | claude-haiku-4-5-20251001 | CCS :8317 | T1 |
| `gpt-5.4-mini` | gpt-5.4-mini | CCS :8317 | T1 |
| `medium`, `sonnet` | claude-sonnet-4-6 | CCS :8317 | T2 |
| `gpt-5.4` | gpt-5.4 | CCS :8317 | T2 |
| `expensive`, `opus` | claude-opus-4-6 | CCS :8317 | T3 |

---

## Thinking models — post-processing aplicat automat

| Model | Comportament | Fix aplicat |
|-------|-------------|------------|
| GLM-4.7/5 | `content=null`, `reasoning_content=<text>` | fallback la reasoning_content |
| DeepSeek-R1 | `content` conține `<think>...</think>` prefix | strip tag-uri, returnează răspunsul curat |
| MiniMax-M2 | `content` conține `<think>...</think>` prefix | idem |
| Qwen3-thinking | similar DeepSeek-R1 | idem |

Dacă `max_tokens` e prea mic și `</think>` e trunchiat → returnează mesaj util în loc de garbage.

---

## Capabilități speciale (v2.4)

### Vision — gemini-vision sau orice model Gemini
```json
{"model":"gemini-vision","messages":[{"role":"user","content":[
  {"type":"text","text":"Describe"},
  {"type":"image_url","image_url":{"url":"https://..."}}
]}]}
```
- URL extern: descărcat automat ca inlineData (max 10MB, cu User-Agent)
- data URI: `data:image/jpeg;base64,...` → inlineData direct
- Fallback la fileData dacă download eșuează (funcționează pentru GCS: gs://)

### Web Search — /v1/tools/search
```json
{"query":"...", "max_results":5, "freshness":"pw"}
```
- Cu BRAVE_API_KEY: Brave Search (2000 req/mo gratis)
- Fără cheie: Gemini grounding cu Google Search (1 result cu context)

### Traducere — /v1/tools/translate
```json
{"text":"...", "target_lang":"ro", "source_lang":"auto"}
```
- Zero IAM extra, zero cost (Gemini Flash)

### YouTube — /v1/tools/youtube
```json
{"action":"transcript", "video_id":"jNQXAC9IVRw", "language":"en"}
{"action":"search", "query":"...", "max_results":5}
{"action":"analyze", "video_id":"...", "question":"..."}
```
- transcript: local python (fără API key), funcționează pe PC, nu pe GCP VM
- search: necesită YOUTUBE_API_KEY (100 req/zi gratis)

### STT — /v1/audio/transcriptions
```json
{"audio_base64":"base64...", "mime_type":"audio/mp3", "language":"en"}
```
- Via Gemini (zero IAM extra), max 20MB

### TTS — /v1/audio/speech (necesită IAM)
```json
{"input":"text...", "voice":"alloy", "speed":1.0}
```
- Cloud TTS Chirp 3 HD, returnează MP3 binar
- Necesită: `gcloud projects add-iam-policy-binding vertex-lab-1774842110 --member="serviceAccount:..." --role="roles/texttospeech.client"`
- Voci: alloy, echo, fable, onyx, nova, shimmer (→ en-US-Chirp3-HD-*)

---

## API Keys necesare (opționale)

| Key | Endpoint | Unde obții | Gratis |
|-----|----------|-----------|--------|
| BRAVE_API_KEY | /v1/tools/search | api.search.brave.com | 2000 req/mo |
| YOUTUBE_API_KEY | /v1/tools/youtube action=search | GCP Console → APIs → YouTube Data API v3 | 100/zi |
| TTS IAM | /v1/audio/speech | GCP Console → IAM → SA → roles/texttospeech.client | DA |

---

## Cum se pornește

```powershell
[System.Environment]::SetEnvironmentVariable("PYTHONUTF8","1","Process")
Start-Process python -ArgumentList "smart-router.py" -WorkingDirectory "F:\llm-router" -WindowStyle Minimized
# Verificare:
Invoke-RestMethod http://localhost:4001/health
```

---

## Probe (2026-04-01 — v2.4)

| Test | Rezultat |
|------|---------|
| Model count | 36 ✅ |
| `gemini-vision` (image download + inlineData) | OK 4253ms: "Glitched Google logo, horizontal lines." ✅ |
| `deepseek-r1` (1000 tokens) | OK 2094ms: `56` (curat, fără think tags) ✅ |
| `deepseek-r1` (50 tokens trunchiat) | OK: `[Thinking truncated by max_tokens...]` ✅ |
| `gemini-code-exec` | OK 3452ms: sum of squares = 385 ✅ |
| `text-embedding-005` | OK 949ms: dims=768 ✅ |
| `/v1/tools/search` (Gemini fallback) | OK 6733ms: 1 result ✅ |
| `/v1/tools/translate` (ro) | OK 3421ms: "Buna, ce mai faci?" ✅ |
| `/v1/tools/youtube transcript` | OK 1342ms: 6 segments ✅ |
| `/v1/audio/transcriptions` STT | OK 2018ms: transcript returnat ✅ |
| STT 413 limit (>20MB) | OK: rejected with 413 ✅ |
| TTS 400 limit (>5000 chars) | OK: rejected with 400 ✅ |
| `/v1/audio/speech` TTS | SKIP: needs roles/texttospeech.client IAM grant |
| YouTube search | SKIP: needs YOUTUBE_API_KEY |

## TODO (nice-to-have, nu blocat)
- **TTS IAM**: `gcloud projects add-iam-policy-binding vertex-lab-1774842110 --member="serviceAccount:vertex-ai-sa@vertex-lab-1774842110.iam.gserviceaccount.com" --role="roles/texttospeech.client"`
- **YOUTUBE_API_KEY**: GCP Console → APIs & Services → YouTube Data API v3 → Enable → Credentials → Create API Key
- **BRAVE_API_KEY**: api.search.brave.com (2000/mo gratis)
- Speech-to-Text Chirp 3: Gemini audio STT e suficient pentru majoritatea use-case-urilor
- Document AI: PDF parsing (complex, necesită processor setup separat)
