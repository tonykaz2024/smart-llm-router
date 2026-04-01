"""
Smart LLM Router v2.4 -- Multi-Backend Intelligent Routing

Combines:
  - CCS CLIProxy (localhost:8317): Claude Haiku/Sonnet/Opus + GPT-5.4
  - DOT PC Vertex (192.168.10.38:4000): Gemini 2.5 Flash/Pro (FREE via $300 credits)
  - Vertex AI Local (SA key F:\\vertex-ai-lab\\vertex-ai-key.json):
      * Llama 4 Maverick/Scout (us-east5 OpenAI-compat MaaS)
      * Gemini 2.5 Flash/Pro (us-central1 generateContent)

Routes requests to the CHEAPEST model that can handle the task:
  TIER 0 (FREE):  Gemini 2.5 Flash / Llama 4 Maverick via Vertex local
  TIER 1 (CHEAP): Claude Haiku 4.5 or GPT-5.4-mini
  TIER 2 (MID):   Claude Sonnet 4.6
  TIER 3 (PREMIUM): Claude Opus 4.6

Usage:
  python smart-router.py                  # Start on port 4001
  python smart-router.py --port 5000      # Custom port
  python smart-router.py --test           # Run routing logic tests
  python smart-router.py --live           # Run live end-to-end tests

Architecture:
  Client -> Smart Router (:4001) -> CCS (:8317) or Vertex DOT-PC (:4000@192.168.10.38)
                                 -> Vertex Local (SA key) -> Llama4/Gemini direct API
"""

import base64
import gzip
import http.server
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import threading
from datetime import datetime
from pathlib import Path

# === API KEYS CONFIGURABILE VIA ENV VARS ===
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BRAVE_API_KEY   = os.environ.get("BRAVE_API_KEY",   "")
ZAI_API_KEY     = os.environ.get("ZAI_API_KEY",     "")
# BRAVE_API_KEY: $env:BRAVE_API_KEY="BSA..."  (https://api.search.brave.com)
# ZAI_API_KEY:   $env:ZAI_API_KEY="4970b..."  (https://api.z.ai — Z.AI GLM models)

# === OPTIONAL DEPS CHECK: youtube-transcript-api ===
try:
    from youtube_transcript_api import YouTubeTranscriptApi as _YTTranscriptApi
    _TRANSCRIPT_API_AVAILABLE = True
except ImportError:
    _YTTranscriptApi = None
    _TRANSCRIPT_API_AVAILABLE = False
# Configurare: setezi în PowerShell → $env:YOUTUBE_API_KEY="AIza..."
# sau adaugi în Windows Environment Variables permanent.

# === VERTEX AI LOCAL CONFIG ===
VERTEX_SA_KEY  = os.environ.get("VERTEX_SA_KEY", r"F:\vertex-ai-lab\vertex-ai-key.json")
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "vertex-lab-1774842110")

# Token cache — refolosit până la expirare (55 min TTL pentru SA tokens)
_vertex_token_cache = {"token": None, "expires_at": 0}
_vertex_token_lock  = threading.Lock()

def get_vertex_token():
    """Returnează un Bearer token valid pentru Vertex AI.
    Token-ul e cache-uit 55 de minute (SA tokens trăiesc 60 min).
    Thread-safe.
    """
    with _vertex_token_lock:
        now = time.time()
        if _vertex_token_cache["token"] and now < _vertex_token_cache["expires_at"]:
            return _vertex_token_cache["token"]
        # Refresh
        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests as gtr
            creds = service_account.Credentials.from_service_account_file(
                VERTEX_SA_KEY,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            creds.refresh(gtr.Request())
            _vertex_token_cache["token"]      = creds.token
            _vertex_token_cache["expires_at"] = now + 55 * 60  # 55 min
            return creds.token
        except Exception as e:
            print(f"[VERTEX-LOCAL] Token refresh failed: {e}")
            return None


# === VERTEX AI LOCAL — OpenAI-compat MaaS (multi-region) ===
# us-east5:  Llama 4 Maverick/Scout
# global:    Qwen3, DeepSeek V3.2, GLM-4.7, GPT-OSS 120B

MAAS_ENDPOINTS = {
    "us-east5": (
        f"https://us-east5-aiplatform.googleapis.com/v1beta1"
        f"/projects/{VERTEX_PROJECT}/locations/us-east5"
        f"/endpoints/openapi/chat/completions"
    ),
    "global": (
        f"https://aiplatform.googleapis.com/v1beta1"
        f"/projects/{VERTEX_PROJECT}/locations/global"
        f"/endpoints/openapi/chat/completions"
    ),
    # us-central1 MaaS: DeepSeek R1-0528 + alte modele care nu merg pe global
    "us-central1": (
        f"https://us-central1-aiplatform.googleapis.com/v1beta1"
        f"/projects/{VERTEX_PROJECT}/locations/us-central1"
        f"/endpoints/openapi/chat/completions"
    ),
}
# Alias păstrat pentru compatibilitate
LLAMA4_ENDPOINT = MAAS_ENDPOINTS["us-east5"]

def proxy_vertex_llama4(vertex_model_id, openai_payload_dict, handler=None, region="us-east5"):
    """Proxy OpenAI-format request direct la Vertex AI Llama 4 MaaS (us-east5).

    Llama 4 via Vertex MaaS expune un endpoint OpenAI-compat nativ.
    Nu e nevoie de conversie format — trimitem payload-ul ca atare
    cu model= schimbat la ID-ul Vertex.

    Returns (status, headers_dict, body_bytes) sau None dacă streaming trimis.
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable — check SA key")

    payload = dict(openai_payload_dict)
    payload["model"] = vertex_model_id  # ex: "meta/llama-4-maverick-17b-128e-instruct-maas"

    body = json.dumps(payload).encode()
    endpoint = MAAS_ENDPOINTS.get(region, MAAS_ENDPOINTS["us-east5"])
    req  = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")

    is_stream = payload.get("stream", False)
    resp = urllib.request.urlopen(req, timeout=120)

    if is_stream and handler is not None:
        handler.send_response(resp.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        try:
            for raw_line in resp:
                # Fix P0.1: thinking models returnează SSE cu delta.content=null
                # și delta.reasoning_content=<text>. Rewrite-uim chunk-ul la forwarding.
                if raw_line.startswith(b"data: ") and b'"delta"' in raw_line:
                    try:
                        chunk = json.loads(raw_line[6:].strip())
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if not content and delta.get("reasoning_content"):
                            # Caz 1: content=null (GLM-4.7 style) → fallback la reasoning_content
                            delta["content"] = delta["reasoning_content"]
                            raw_line = b"data: " + json.dumps(chunk).encode() + b"\n"
                        elif isinstance(content, str) and content.startswith("<think>"):
                            # Caz 2: streaming cu <think> tags — skip chunk-ul de thinking
                            # (clientul nu vrea să vadă gândirea în streaming)
                            continue
                    except Exception:
                        pass  # parse fail → forward original
                handler.wfile.write(raw_line)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    status = resp.status
    resp_headers = dict(resp.headers)
    body_bytes = resp.read()

    # Fix pentru modele thinking (GLM-4.7, DeepSeek-R1, MiniMax-M2, Qwen3-thinking etc.):
    # Caz 1: content=null, răspunsul real e în reasoning_content (GLM-4.7 style)
    # Caz 2: content conține <think>...</think> tags inline (DeepSeek-R1, MiniMax-M2 style)
    # → stripped la text curat DUPĂ </think>
    try:
        resp_json = json.loads(body_bytes)
        modified = False
        for choice in resp_json.get("choices", []):
            msg = choice.get("message", {})
            content = msg.get("content")
            if not content:
                # Caz 1: fallback la reasoning_content
                reasoning = msg.get("reasoning_content", "")
                if reasoning:
                    msg["content"] = reasoning
                    modified = True
            elif isinstance(content, str) and "<think>" in content:
                # Caz 2: strip <think>...</think> blocks (DeepSeek-R1, MiniMax-M2 style)
                # Cazuri:
                #   a) <think>...</think>actual answer  → păstrăm "actual answer"
                #   b) <think>...trunchiat la max_tokens → returnăm mesaj util
                if "</think>" in content:
                    stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                else:
                    # Thinking trunchiat — nu a ajuns la răspuns; informăm clientul
                    stripped = "[Thinking truncated by max_tokens. Increase max_tokens for full response.]"
                msg["content"] = stripped if stripped else content
                modified = True
        if modified:
            body_bytes = json.dumps(resp_json).encode()
    except Exception:
        pass  # dacă parse eșuează, returnăm body-ul original

    return status, resp_headers, body_bytes


# === VERTEX AI LOCAL — GEMINI (generateContent format) ===

GEMINI_ENDPOINT_TPL = (
    "https://us-central1-aiplatform.googleapis.com/v1"
    "/projects/{project}/locations/us-central1"
    "/publishers/google/models/{model}:generateContent"
)
GEMINI_MODELS = {
    "gemini-2.5-flash-local": "gemini-2.5-flash",
    "gemini-2.5-pro-local":   "gemini-2.5-pro",
}

def _openai_to_gemini(openai_payload, grounded=False, code_exec=False):
    """Convertește OpenAI chat/completions payload → Vertex generateContent format.

    OpenAI: {"messages": [{"role": "user", "content": "..."}], "max_tokens": 1024}
    Gemini: {"contents": [...], "generationConfig": {...}, "tools": [...]}

    grounded=True  → adaugă googleSearch tool (live web context).
    code_exec=True → adaugă code_execution tool (sandbox Python, zero cost extra).
    IMPORTANT (P0.1 fix): `tools` merge la nivelul TOP al payload-ului Gemini,
    NU în generationConfig — altfel tools sunt silently ignored.
    """
    messages = openai_payload.get("messages", [])
    contents = []
    system_text = None

    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")

        # Gemini nu acceptă "system" — îl transformăm în prim user message
        if role == "system":
            system_text = content if isinstance(content, str) else str(content)
            continue

        # Normalizează content: string sau lista de parts (multimodal)
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type", "")
                if ptype == "text":
                    parts.append({"text": p.get("text", "")})
                elif ptype == "image_url":
                    img_obj = p.get("image_url", {})
                    url_val = img_obj.get("url", "") if isinstance(img_obj, dict) else str(img_obj)
                    if url_val.startswith("data:"):
                        # data:image/jpeg;base64,<data>
                        header, b64data = url_val.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]  # e.g. image/jpeg
                        parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                    elif url_val.startswith("http"):
                        # Remote URL — download locally and send as inlineData.
                        # Gemini fileData only works for GCS URIs (gs://); external HTTPS
                        # URLs fail when the server blocks Vertex AI crawlers (robots.txt).
                        ext = url_val.rsplit(".", 1)[-1].lower().split("?")[0] if "." in url_val else "jpeg"
                        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
                        mime = mime_map.get(ext, "image/jpeg")
                        try:
                            img_req = urllib.request.Request(url_val, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(img_req, timeout=10) as ir:
                                img_bytes = ir.read(10 * 1024 * 1024)  # max 10MB
                                ct_hdr = ir.headers.get("Content-Type", "")
                                if ct_hdr and "/" in ct_hdr:
                                    mime = ct_hdr.split(";")[0].strip()
                            img_b64 = base64.b64encode(img_bytes).decode("ascii")
                            parts.append({"inlineData": {"mimeType": mime, "data": img_b64}})
                        except Exception as img_err:
                            # Fallback: try fileData (works for GCS + some public URLs)
                            parts.append({"fileData": {"mimeType": mime, "fileUri": url_val}})
            if not parts:
                parts = [{"text": ""}]
        else:
            parts = [{"text": str(content)}]

        # Gemini acceptă "user" și "model" (nu "assistant")
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": parts})

    # Prepend system prompt ca prim user turn dacă există
    if system_text:
        contents.insert(0, {"role": "user", "parts": [{"text": system_text}]})
        # Dacă primul turn real e tot user, inserăm un model placeholder
        if len(contents) > 1 and contents[1]["role"] == "user":
            contents.insert(1, {"role": "model", "parts": [{"text": "Understood."}]})

    gen_config = {}
    # gemini-2.5-flash/pro sunt modele cu thinking — consumă tokeni înainte de răspuns.
    # Minimum 2048 pentru a nu rămâne fără tokeni după thinking budget.
    requested = openai_payload.get("max_tokens")
    gen_config["maxOutputTokens"] = max(requested, 2048) if requested else 2048
    if "temperature" in openai_payload:
        gen_config["temperature"] = openai_payload["temperature"]
    if "top_p" in openai_payload:
        gen_config["topP"] = openai_payload["top_p"]

    payload = {"contents": contents, "generationConfig": gen_config}

    # Tools la nivel TOP — NU în generationConfig (P0.1 fix: altfel silently ignored)
    tools = []
    if grounded:
        tools.append({"googleSearch": {}})
    if code_exec:
        tools.append({"code_execution": {}})
    if tools:
        payload["tools"] = tools

    return payload


def _gemini_to_openai(gemini_response, model_name):
    """Convertește Vertex generateContent response → OpenAI chat/completions format.

    Include groundingMetadata în câmpul extra 'grounding_metadata' din response
    (P1.4 fix: nu mai e silently dropped).
    """
    candidates = gemini_response.get("candidates", [])
    text = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)

    usage_meta = gemini_response.get("usageMetadata", {})

    # Extrage grounding metadata dacă există (Google Search grounding)
    grounding_meta = None
    if candidates:
        gm = candidates[0].get("groundingMetadata", {})
        if gm:
            grounding_meta = {
                "web_search_queries": gm.get("webSearchQueries", []),
                "grounding_chunks":   [
                    {"uri": c.get("web", {}).get("uri", ""), "title": c.get("web", {}).get("title", "")}
                    for c in gm.get("groundingChunks", [])
                ],
            }

    resp = {
        "id":      f"chatcmpl-gemini-{int(time.time())}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model_name,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens":      usage_meta.get("totalTokenCount", 0),
        },
    }
    if grounding_meta:
        resp["grounding_metadata"] = grounding_meta
    return resp


def proxy_vertex_gemini(vertex_model_id, openai_payload_dict, handler=None, grounded=False, code_exec=False):
    """Proxy OpenAI-format request → Vertex Gemini generateContent.

    Convertește automat: OpenAI format → generateContent → OpenAI format.
    Streaming nu e suportat (Gemini streaming are format SSE diferit).
    grounded=True  → adaugă Google Search grounding tool (zero setup extra).
    code_exec=True → adaugă Code Execution tool (sandbox Python, zero cost extra).
    Returns (status, headers_dict, body_bytes).
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable — check SA key")

    # Nu trimite stream la Gemini (format incompatibil cu SSE OpenAI)
    payload_no_stream = dict(openai_payload_dict)
    payload_no_stream.pop("stream", None)

    gemini_payload = _openai_to_gemini(payload_no_stream, grounded=grounded, code_exec=code_exec)
    url  = GEMINI_ENDPOINT_TPL.format(project=VERTEX_PROJECT, model=vertex_model_id)
    body = json.dumps(gemini_payload).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")

    resp = urllib.request.urlopen(req, timeout=120)
    gemini_resp = json.loads(resp.read())
    openai_resp = _gemini_to_openai(gemini_resp, payload_no_stream.get("model", vertex_model_id))

    return 200, {"Content-Type": "application/json"}, json.dumps(openai_resp).encode()


# === BRAVE SEARCH + GEMINI FALLBACK ===

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

def proxy_brave_search(query, count=5):
    """Returns (results_list, formatted_text).
    Brave Search API if BRAVE_API_KEY set, else Gemini grounding fallback.
    """
    if BRAVE_API_KEY:
        params = urllib.parse.urlencode({"q": query, "count": count, "search_lang": "en"})
        req = urllib.request.Request(f"{BRAVE_SEARCH_URL}?{params}")
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Encoding", "gzip")
        req.add_header("X-Subscription-Token", BRAVE_API_KEY)
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read()
        # decompress gzip if needed
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        data = json.loads(raw)
        results = [
            {
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("description", ""),
                "score":   r.get("score", 0),
            }
            for r in data.get("web", {}).get("results", [])
        ]
        text = "\n".join(
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        )
        return results, text
    else:
        return _proxy_gemini_as_search(query)


def _proxy_gemini_as_search(query):
    """Fallback: Gemini grounding — extract citations as structured search results.

    grounding_chunks schema (Gemini 2.x):
      [{"web": {"uri": "https://...", "title": "..."}}, ...]
    Graceful fallback se `grounding_chunks` e gol (Gemini a raspuns fara search trigger).
    """
    try:
        payload = {
            "messages": [{"role": "user", "content": f"Search the web for: {query}. Summarize the top results with sources."}],
            "max_tokens": 1024,
        }
        status, _, body = proxy_vertex_gemini("gemini-2.5-flash", payload, grounded=True)
        resp = json.loads(body)
        grounding = resp.get("grounding_metadata", {})
        chunks = grounding.get("grounding_chunks", [])
        results = []
        for c in chunks:
            # Schema Gemini 2.x: {"web": {"uri": "...", "title": "..."}}
            web = c.get("web") or c.get("retrievedContext") or {}
            uri   = web.get("uri", "")
            title = web.get("title", "")
            if uri:
                results.append({"title": title, "url": uri, "snippet": "", "score": 0})
        text_content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not results:
            # Gemini didn't trigger search — return answer as single result
            results = [{"title": "Gemini answer (no search triggered)", "url": "", "snippet": text_content[:300], "score": 0}]
        text = text_content if text_content else "\n".join(
            f"{i+1}. {r['title']}\n   {r['url']}"
            for i, r in enumerate(results)
        )
        return results, text
    except Exception as e:
        return [], f"Search error: {e}"


# === TRANSLATION VIA GEMINI ===

def proxy_translate(text, target_lang, source_lang=None):
    """Gemini-powered translation. Zero IAM. Auto-detects source if not provided."""
    if source_lang:
        prompt = f"Translate from {source_lang} to {target_lang}. Output ONLY the translation, no explanations:\n\n{text}"
    else:
        prompt = f"Translate to {target_lang}. Output ONLY the translation, no explanations:\n\n{text}"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.1,
    }
    status, _, body = proxy_vertex_gemini("gemini-2.5-flash", payload)
    resp = json.loads(body)
    translated = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    return translated.strip()


# === AUDIO TRANSCRIPTION VIA GEMINI STT ===

def proxy_gemini_transcribe(audio_base64, mime_type="audio/mp3", language="en"):
    """STT via Gemini 2.5 Flash audio understanding. Zero IAM extra."""
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable")
    prompt = f"Transcribe this audio accurately. Language: {language}. Output ONLY the transcript text, nothing else."
    gemini_payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime_type, "data": audio_base64}},
            {"text": prompt},
        ]}],
        "generationConfig": {"maxOutputTokens": 8192},
    }
    url = GEMINI_ENDPOINT_TPL.format(project=VERTEX_PROJECT, model="gemini-2.5-flash")
    body = json.dumps(gemini_payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=120)
    gemini_resp = json.loads(resp.read())
    candidates = gemini_resp.get("candidates", [])
    text = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
    return text.strip()


# === TTS VIA CLOUD TEXT-TO-SPEECH (Chirp 3 HD) ===

TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

# OpenAI voice alias -> Chirp 3 HD voice name
TTS_VOICE_ALIASES = {
    "alloy":  "en-US-Chirp3-HD-Kore",
    "nova":   "en-US-Chirp3-HD-Puck",
    "echo":   "en-US-Chirp3-HD-Charon",
    "fable":  "en-US-Chirp3-HD-Fenrir",
    "onyx":   "en-US-Chirp3-HD-Orus",
    "shimmer":"en-US-Chirp3-HD-Aoede",
}

def proxy_tts(text, voice="en-US-Chirp3-HD-Kore", speed=1.0):
    """OpenAI-compatible TTS via Cloud Text-to-Speech Chirp 3 HD.
    Requires roles/texttospeech.client IAM role on the SA key.
    Returns (audio_base64_str, mime_type).
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable")

    # Resolve voice alias
    resolved_voice = TTS_VOICE_ALIASES.get(voice, voice)

    # Detect language code from voice name (e.g. en-US-Chirp3-HD-Kore -> en-US)
    parts = resolved_voice.split("-")
    lang_code = "-".join(parts[:2]) if len(parts) >= 2 else "en-US"

    tts_payload = {
        "input": {"text": text},
        "voice": {
            "languageCode": lang_code,
            "name": resolved_voice,
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": speed,
        },
    }
    body = json.dumps(tts_payload).encode()
    req = urllib.request.Request(TTS_ENDPOINT, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        audio_b64 = data.get("audioContent", "")
        return audio_b64, "audio/mpeg"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 403:
            raise RuntimeError(
                "TTS 403 Forbidden — SA key needs 'roles/texttospeech.client' IAM role. "
                "Grant it at: https://console.cloud.google.com/iam-admin/iam"
            )
        raise RuntimeError(f"TTS error {e.code}: {err_body[:300]}")


# === VERTEX AI LOCAL — EMBEDDINGS ===

EMBED_ENDPOINT_TPL = (
    "https://us-central1-aiplatform.googleapis.com/v1"
    "/projects/{project}/locations/us-central1"
    "/publishers/google/models/{model}:predict"
)
EMBED_MODELS = {
    "text-embedding-004":              "text-embedding-004",
    "text-embedding-005":              "text-embedding-005",
    "text-multilingual-embedding-002": "text-multilingual-embedding-002",
}

def proxy_vertex_embeddings(vertex_model_id, openai_payload_dict):
    """Proxy OpenAI /v1/embeddings → Vertex text-embedding-004.

    OpenAI input:  {"model": "...", "input": "text" | ["text1","text2"]}
    Vertex input:  {"instances": [{"content": "text"}, ...]}
    Vertex output: {"predictions": [{"embeddings": {"values": [...]}}]}
    OpenAI output: {"object":"list","data":[{"object":"embedding","embedding":[...],"index":0}],...}
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable")

    raw_input = openai_payload_dict.get("input", "")
    if isinstance(raw_input, str):
        texts = [raw_input]
    else:
        texts = list(raw_input)

    instances = [{"content": t} for t in texts]
    url  = EMBED_ENDPOINT_TPL.format(project=VERTEX_PROJECT, model=vertex_model_id)
    body = json.dumps({"instances": instances}).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")

    resp = urllib.request.urlopen(req, timeout=60)
    vertex_resp = json.loads(resp.read())

    data_items = []
    total_tokens = 0
    for i, pred in enumerate(vertex_resp.get("predictions", [])):
        values = pred.get("embeddings", {}).get("values", [])
        stats  = pred.get("embeddings", {}).get("statistics", {})
        total_tokens += stats.get("token_count", 0)
        data_items.append({"object": "embedding", "embedding": values, "index": i})

    openai_resp = {
        "object": "list",
        "data":   data_items,
        "model":  vertex_model_id,
        "usage":  {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }
    return 200, {"Content-Type": "application/json"}, json.dumps(openai_resp).encode()


# === VERTEX AI — IMAGEN 3 IMAGE GENERATION ===

IMAGEN_ENDPOINT_TPL = (
    "https://us-central1-aiplatform.googleapis.com/v1"
    "/projects/{project}/locations/us-central1"
    "/publishers/google/models/{model}:predict"
)
IMAGEN_MODELS = {
    "imagen-3":      "imagen-3.0-generate-002",
    "imagen-3-fast": "imagen-3.0-fast-generate-001",
}

def proxy_vertex_imagen(vertex_model_id, openai_img_payload):
    """Proxy OpenAI /v1/images/generations → Vertex Imagen 3.

    OpenAI input:  {"prompt": "...", "n": 1, "size": "1024x1024", "response_format": "b64_json"}
    Vertex input:  {"instances": [{"prompt": "..."}], "parameters": {"sampleCount": 1, ...}}
    Vertex output: {"predictions": [{"bytesBase64Encoded": "...", "mimeType": "image/png"}]}
    OpenAI output: {"created": 123, "data": [{"b64_json": "..."}]}

    Content filter: Imagen poate returna predictions=[] silently → returnăm eroare explicativă.
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable — check SA key")

    prompt = openai_img_payload.get("prompt", "")
    n = openai_img_payload.get("n", 1)
    size = openai_img_payload.get("size", "1024x1024")

    # Mapare size → aspectRatio
    aspect_map = {
        "1024x1024": "1:1",
        "1024x1792": "9:16",
        "1792x1024": "16:9",
        "512x512":   "1:1",
        "256x256":   "1:1",
    }
    aspect = aspect_map.get(size, "1:1")

    vertex_payload = {
        "instances":  [{"prompt": prompt}],
        "parameters": {
            "sampleCount": min(n, 4),  # max 4 per request
            "aspectRatio": aspect,
        },
    }

    url  = IMAGEN_ENDPOINT_TPL.format(project=VERTEX_PROJECT, model=vertex_model_id)
    body = json.dumps(vertex_payload).encode()
    req  = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")

    resp = urllib.request.urlopen(req, timeout=120)
    vertex_resp = json.loads(resp.read())

    predictions = vertex_resp.get("predictions", [])
    if not predictions:
        # Content filter blocat silently (P0.4 awareness)
        return 200, {"Content-Type": "application/json"}, json.dumps({
            "created": int(time.time()),
            "data":    [],
            "error":   "Content policy filter blocked this prompt. Try a different prompt.",
        }).encode()

    openai_resp = {
        "created": int(time.time()),
        "data": [
            {
                "b64_json":       p.get("bytesBase64Encoded", ""),
                "revised_prompt": prompt,
            }
            for p in predictions
        ],
    }
    return 200, {"Content-Type": "application/json"}, json.dumps(openai_resp).encode()


# === YOUTUBE DATA API v3 ===

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

def proxy_youtube_search(query, max_results=5):
    """Caută videoclipuri pe YouTube și returnează metadata.

    Returnează răspuns în format text (OpenAI-compat content string) cu
    titluri, URL-uri și descrieri scurte.

    Necesită YOUTUBE_API_KEY env var (API key din GCP Console — nu SA Bearer token).
    Quota: 100 searches/day gratuit (100 units/search × 100 quota = 100 searches).
    """
    if not YOUTUBE_API_KEY:
        return None, "YOUTUBE_API_KEY not configured. Set env var: $env:YOUTUBE_API_KEY='AIza...'"

    params = urllib.parse.urlencode({
        "part":       "snippet",
        "q":          query,
        "maxResults": max_results,
        "type":       "video",
        "key":        YOUTUBE_API_KEY,
    })
    url = f"{YOUTUBE_SEARCH_URL}?{params}"
    req = urllib.request.Request(url)

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return None, f"YouTube API error {e.code}: {body[:200]}"

    items = data.get("items", [])
    if not items:
        return [], "No results found."

    results = []
    for item in items:
        vid_id  = item.get("id", {}).get("videoId", "")
        snippet = item.get("snippet", {})
        results.append({
            "title":       snippet.get("title", ""),
            "videoId":     vid_id,
            "url":         f"https://www.youtube.com/watch?v={vid_id}",
            "channel":     snippet.get("channelTitle", ""),
            "description": snippet.get("description", "")[:200],
            "publishedAt": snippet.get("publishedAt", ""),
        })

    # Formată ca text pentru răspuns OpenAI-compat
    text_lines = [f"YouTube search results for: \"{query}\"\n"]
    for i, r in enumerate(results, 1):
        text_lines.append(
            f"{i}. **{r['title']}**\n"
            f"   URL: {r['url']}\n"
            f"   Channel: {r['channel']} | Published: {r['publishedAt'][:10]}\n"
            f"   {r['description']}\n"
        )

    return results, "\n".join(text_lines)


# === VERTEX AI SELF-DEPLOY ENDPOINT (rawPredict) ===

def proxy_vertex_rawpredict(endpoint_resource_name, openai_payload_dict, handler=None):
    """Proxy OpenAI-format request → Vertex AI custom deployed endpoint (rawPredict).

    endpoint_resource_name: "projects/{project}/locations/{region}/endpoints/{endpoint_id}"
    ex: "projects/vertex-lab-1774842110/locations/us-central1/endpoints/1234567890"

    rawPredict trimite body-ul exact ca atare (fără conversie Vertex nativă).
    Funcționează dacă modelul deployat acceptă OpenAI format nativ
    (ex: vLLM, TGI, sau orice server OpenAI-compat pe Vertex).

    Pentru modele cu format Vertex nativ (:predict cu {"instances": [...]}),
    trebuie o funcție separată per tip de model.
    """
    token = get_vertex_token()
    if not token:
        raise RuntimeError("Vertex token unavailable — check SA key")

    # Extrage region din resource name
    parts = endpoint_resource_name.split("/")
    region = "us-central1"
    try:
        loc_idx = parts.index("locations")
        region = parts[loc_idx + 1]
    except (ValueError, IndexError):
        pass

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1"
        f"/{endpoint_resource_name}:rawPredict"
    )

    payload = dict(openai_payload_dict)
    body    = json.dumps(payload).encode()
    req     = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type",  "application/json")

    is_stream = payload.get("stream", False)
    resp = urllib.request.urlopen(req, timeout=120)

    if is_stream and handler is not None:
        handler.send_response(resp.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        try:
            for line in resp:
                handler.wfile.write(line)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    return resp.status, dict(resp.headers), resp.read()


# === MULTIPART FORM PARSER (for audio uploads) ===

def _parse_multipart_audio(body, content_type):
    """Extract audio bytes + metadata from multipart/form-data.
    Returns (audio_bytes, mime_type, language).

    Parser manual (nu email module) — email module poate corupe binary data
    daca boundary apare accidental in continut (P0.2 fix).
    """
    # Extrage boundary din Content-Type header
    boundary = b""
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            b_val = part[len("boundary="):].strip('"')
            boundary = b_val.encode("ascii")
            break
    if not boundary:
        raise ValueError("No boundary in Content-Type header")

    # Split body pe boundary (RFC 2046: "--boundary")
    delimiter = b"--" + boundary
    end_delim  = b"--" + boundary + b"--"

    parts = body.split(delimiter)
    audio_bytes = b""
    mime_type   = "audio/mp3"
    language    = "en"

    for raw_part in parts:
        if not raw_part or raw_part.startswith(b"--") or len(raw_part) < 4:
            continue
        raw_part = raw_part.lstrip(b"\r\n")
        # Split headers from body on first double-CRLF
        if b"\r\n\r\n" in raw_part:
            headers_raw, part_body = raw_part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in raw_part:
            headers_raw, part_body = raw_part.split(b"\n\n", 1)
        else:
            continue
        # Remove trailing CRLF from body
        part_body = part_body.rstrip(b"\r\n")

        # Parse headers (case-insensitive)
        headers = {}
        for line in headers_raw.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("ascii", errors="replace")] = v.strip().decode("utf-8", errors="replace")

        cd = headers.get("content-disposition", "")
        ct = headers.get("content-type", "")

        if 'name="file"' in cd or "name=file" in cd or 'name="audio"' in cd:
            audio_bytes = part_body
            if ct and ct != "application/octet-stream":
                mime_type = ct.split(";")[0].strip()
        elif 'name="language"' in cd:
            language = part_body.decode("utf-8", errors="replace").strip()
        # "model" field ignored — always Gemini

    if not audio_bytes:
        raise ValueError("No 'file' field found in multipart body")

    return audio_bytes, mime_type, language


# === BACKEND CONFIGURATION ===
BACKENDS = {
    "ccs": {"url": "http://localhost:8317/v1", "key": "ccs-internal-managed"},
    # Z.AI direct — GLM models via Coding PaaS API (OpenAI-compatible)
    "zai": {"url": "https://api.z.ai/api/coding/paas/v4", "key_env": "ZAI_API_KEY"},
    # vertex-local-llama4 și vertex-local-gemini sunt tratate special în handle_chat
    # (autentificare SA key proprie — nu trec prin proxy_to_backend)
}

# Model registry: name -> (backend, actual_model_id, tier, cost_label)
MODEL_REGISTRY = {
    # Tier 0: FREE — Vertex MaaS us-east5
    "llama-4-maverick":          ("vertex-local-llama4", "meta/llama-4-maverick-17b-128e-instruct-maas", 0, "FREE"),
    "llama-4-scout":             ("vertex-local-llama4", "meta/llama-4-scout-17b-16e-instruct-maas",    0, "FREE"),
    # Tier 0: FREE — Vertex MaaS global (Qwen3, DeepSeek, GLM, GPT-OSS, Kimi, MiniMax)
    "qwen3-235b":                ("vertex-local-global", "qwen/qwen3-235b-a22b-instruct-2507-maas",     0, "FREE"),
    "qwen3-coder":               ("vertex-local-global", "qwen/qwen3-coder-480b-a35b-instruct-maas",    0, "FREE"),
    "qwen3-80b":                 ("vertex-local-global", "qwen/qwen3-next-80b-a3b-instruct-maas",       0, "FREE"),
    "qwen3-80b-think":           ("vertex-local-global", "qwen/qwen3-next-80b-a3b-thinking-maas",       0, "FREE"),
    "deepseek-v3":               ("vertex-local-global", "deepseek-ai/deepseek-v3.2-maas",              0, "FREE"),
    "glm-4.7":                   ("vertex-local-global", "zai-org/glm-4.7-maas",                        0, "FREE"),
    "glm-5":                     ("vertex-local-global", "zai-org/glm-5-maas",                          0, "FREE"),
    "kimi-k2":                   ("vertex-local-global", "moonshotai/kimi-k2-thinking-maas",            0, "FREE"),
    "minimax-m2":                ("vertex-local-global", "minimaxai/minimax-m2-maas",                   0, "FREE"),
    "gpt-oss":                   ("vertex-local-global", "openai/gpt-oss-120b-maas",                    0, "FREE"),
    # Tier 0: FREE — Vertex MaaS us-central1 (DeepSeek R1-0528, alte modele specifice regiunii)
    "deepseek-r1":               ("vertex-local-central", "deepseek-ai/deepseek-r1-0528-maas",   0, "FREE"),
    # Tier 0: FREE — Vertex Gemini us-central1
    "gemini-2.5-flash":          ("vertex-local-gemini", "gemini-2.5-flash",         0, "FREE"),
    "gemini-2.5-pro":            ("vertex-local-gemini", "gemini-2.5-pro",           0, "FREE"),
    "gemini-2.5-flash-thinking": ("vertex-local-gemini", "gemini-2.5-flash-thinking",0, "FREE"),
    # Tier 0: FREE — Gemini cu Google Search Grounding (live web context)
    # NOTĂ: NU în TIER_MODELS — se accesează explicit cu model="gemini-grounded"
    "gemini-grounded":           ("vertex-local-gemini-grounded", "gemini-2.5-flash", 0, "FREE"),
    # Tier 0: FREE — Gemini cu Code Execution (sandbox Python, zero cost extra față de chat normal)
    # NOTĂ: NU în TIER_MODELS — se accesează explicit cu model="gemini-code-exec"
    "gemini-code-exec":          ("vertex-local-gemini-code", "gemini-2.5-flash",    0, "FREE"),
    # Tier 0: FREE — Vertex Embeddings (text-embedding-004/005)
    "text-embedding-004":              ("vertex-local-embed", "text-embedding-004",              0, "FREE"),
    "text-embedding-005":              ("vertex-local-embed", "text-embedding-005",              0, "FREE"),
    "text-multilingual-embedding-002": ("vertex-local-embed", "text-multilingual-embedding-002", 0, "FREE"),
    "embedding":                       ("vertex-local-embed", "text-embedding-005",              0, "FREE"),  # default → v005
    # Imagen 3 — Image generation (~$0.04/image)
    # NOTĂ: NU în TIER_MODELS — se accesează via /v1/images/generations sau model="imagen-3"
    "imagen-3":      ("vertex-local-imagen", "imagen-3.0-generate-002",   0, "IMAGEN"),
    "imagen-3-fast": ("vertex-local-imagen", "imagen-3.0-fast-generate-001", 0, "IMAGEN"),
    # YouTube search — necesită YOUTUBE_API_KEY env var
    # NOTĂ: NU în TIER_MODELS — se accesează explicit sau via /v1/tools/youtube
    "youtube-search": ("youtube", "", 0, "TOOL"),
    # Gemini Vision alias — same backend as gemini-2.5-flash, signals image/audio support
    "gemini-vision":  ("vertex-local-gemini", "gemini-2.5-flash", 0, "FREE"),
    # Vertex self-deploy — adaugă endpoint-urile tale custom:
    # "my-model": ("vertex-local-endpoint", "projects/PROJ/locations/REGION/endpoints/ID", 0, "CUSTOM"),
    # Z.AI Direct — GLM models via api.z.ai (separate quota from Vertex MaaS GLM)
    # Coding PaaS endpoint, OpenAI-compatible. Avantaj: GLM-5.1 (coding specialist), GLM-5 Turbo (fast)
    "glm-5.1":     ("zai", "glm-5.1",     0, "FREE-ZAI"),
    "glm-5-turbo": ("zai", "glm-5-turbo", 0, "FREE-ZAI"),
    "glm-4.5":     ("zai", "glm-4.5",     0, "FREE-ZAI"),
    # Tier 1: CHEAP (CCS CLIProxy — subscription quota)
    "claude-haiku-4-5-20251001": ("ccs", "claude-haiku-4-5-20251001", 1, "CHEAP"),
    "gpt-5.4-mini":              ("ccs", "gpt-5.4-mini",              1, "CHEAP"),
    # Tier 2: MEDIUM
    "claude-sonnet-4-6":         ("ccs", "claude-sonnet-4-6",         2, "MEDIUM"),
    "gpt-5.4":                   ("ccs", "gpt-5.4",                   2, "MEDIUM"),
    # Tier 3: PREMIUM
    "claude-opus-4-6":           ("ccs", "claude-opus-4-6",           3, "PREMIUM"),
}

# Tier-based model selection
# T0: Llama4 (us-east5) primul — cel mai rapid; Qwen3/DeepSeek (global) fallback
TIER_MODELS = {
    0: ["llama-4-maverick", "qwen3-235b", "deepseek-v3", "kimi-k2", "gemini-2.5-flash"],
    1: ["claude-haiku-4-5-20251001", "gpt-5.4-mini"],
    2: ["claude-sonnet-4-6"],
    3: ["claude-opus-4-6"],
}

LISTEN_PORT = 4001

# Stats tracking
stats = {
    "total_requests": 0,
    "routed": {},
    "tier_usage": {0: 0, 1: 0, 2: 0, 3: 0},
    "tokens": {"input": 0, "output": 0},
    "quota_saved_requests": 0,
    "start_time": datetime.now().isoformat(),
    "errors": 0,
}
stats_lock = threading.Lock()
STATS_FILE = Path(__file__).parent / "router-stats.json"

# === ROUTING CLASSIFICATION ===

TIER0_PATTERNS = [
    r"\b(hi|hello|hey|thanks|thank you|yes|no|ok|sure|got it)\b",
    r"\b(rename|explain what|what does|what is|translate|format|lint)\b",
    r"\b(fix typo|add comment|remove comment|capitalize|lowercase|uppercase)\b",
    r"\b(define|spell|abbreviat|convert|trim|count|summarize briefly)\b",
    r"\b(one word|short answer|say hello|greet)\b",
]

TIER2_PATTERNS = [
    r"\b(debug|refactor|implement|fix bug|code review|write function)\b",
    r"\b(write class|write test|unit test|add feature|modify|update)\b",
    r"\b(change|improve|optimize|create component|api endpoint|database)\b",
    r"\b(generate code|write script|automate|integration|middleware)\b",
    r"\b(parse|validate|serialize|handle error|exception)\b",
    r"\bwrite\b.{0,30}\b(test|spec|component|module|service|handler)\b",
    r"\b(unit test|write test|test for|tests for)\b",
]

TIER3_PATTERNS = [
    r"\b(architect|design system|design pattern|complex|research)\b",
    r"\b(analyze deeply|comprehensive|multi-step|trade-off)\b",
    r"\b(security audit|performance optimiz|migrate|refactor entire)\b",
    r"\b(write a paper|essay|novel approach|creative solution)\b",
    r"\b(compare and contrast|pros and cons detailed|deep dive)\b",
    r"\b(implement from scratch|build entire|full implementation)\b",
    r"\b(review architecture|system design|distributed)\b",
    r"\bdesign\b.*\b(system|architecture|platform|service)\b",
    r"\barchitect\b",
]

def classify_tier(messages):
    if not messages:
        return 2

    last_msg = messages[-1].get("content", "") if messages else ""
    if isinstance(last_msg, list):
        last_msg = " ".join(
            p.get("text", "") for p in last_msg if isinstance(p, dict)
        )

    all_text = " ".join(
        m.get("content", "") if isinstance(m.get("content", ""), str)
        else " ".join(p.get("text", "") for p in m.get("content", []) if isinstance(p, dict))
        for m in messages
    ).lower()

    total_words = len(all_text.split())
    user_lower = last_msg.lower().strip()

    # Check tier 3 first (complex) -- before tier 0 to avoid misclassification
    for pat in TIER3_PATTERNS:
        if re.search(pat, user_lower):
            return 3

    # Check tier 2 (medium)
    for pat in TIER2_PATTERNS:
        if re.search(pat, user_lower):
            return 2

    # Check tier 0 (simple) -- only for truly short/simple requests
    if total_words < 15:
        for pat in TIER0_PATTERNS:
            if re.search(pat, user_lower):
                return 0
        return 0

    # Context length heuristics
    code_blocks = all_text.count("```")
    if total_words > 5000 or code_blocks >= 8:
        return 3
    if total_words > 1000 or code_blocks >= 3:
        return 2

    # Default: tier 1 (cheap Claude)
    return 1


# Round-robin counter per tier — distribuie load între modele T0
_tier_rr_counter: dict = {}
_tier_rr_lock = threading.Lock()

def select_model(tier):
    """Selectează modelul din tier cu round-robin (nu mereu primul)."""
    models = TIER_MODELS.get(tier, TIER_MODELS[2])
    if len(models) == 1:
        return models[0]
    with _tier_rr_lock:
        idx = _tier_rr_counter.get(tier, 0) % len(models)
        _tier_rr_counter[tier] = idx + 1
    return models[idx]


def proxy_to_backend(backend_name, path, method, headers, body=None, handler=None):
    """Proxy request to backend.

    If handler is provided AND request has stream=True, forwards SSE chunks
    directly to the handler's wfile (returns None).
    Otherwise returns (status, headers_dict, body_bytes).
    """
    backend = BACKENDS[backend_name]
    url = f"{backend['url']}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    # Support both static key and env var key
    api_key = backend.get("key") or os.environ.get(backend.get("key_env", ""), "")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        if k.lower() not in ("host", "authorization", "content-length", "transfer-encoding"):
            req.add_header(k, v)

    # Detect streaming request
    is_stream = False
    if body:
        try:
            is_stream = json.loads(body).get("stream", False)
        except Exception:
            pass

    resp = urllib.request.urlopen(req, timeout=120)

    if is_stream and handler is not None:
        # Forward SSE line by line without buffering
        handler.send_response(resp.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        try:
            for raw_line in resp:
                # Thinking model fix for Z.AI/CCS streaming: reasoning_content → content
                if raw_line.startswith(b"data: ") and b'"delta"' in raw_line:
                    try:
                        chunk = json.loads(raw_line[6:].strip())
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if not content and delta.get("reasoning_content"):
                            delta["content"] = delta["reasoning_content"]
                            raw_line = b"data: " + json.dumps(chunk).encode() + b"\n"
                        elif isinstance(content, str) and content.startswith("<think>"):
                            continue
                    except Exception:
                        pass
                handler.wfile.write(raw_line)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None  # Already sent response

    # Non-streaming: apply thinking model post-processing
    status_code = resp.status
    resp_headers_dict = dict(resp.headers)
    body_bytes = resp.read()
    try:
        resp_json = json.loads(body_bytes)
        modified = False
        for choice in resp_json.get("choices", []):
            msg = choice.get("message", {})
            content = msg.get("content")
            if not content:
                reasoning = msg.get("reasoning_content", "")
                if reasoning:
                    msg["content"] = reasoning
                    modified = True
            elif isinstance(content, str) and "<think>" in content:
                if "</think>" in content:
                    stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                else:
                    stripped = "[Thinking truncated by max_tokens. Increase max_tokens for full response.]"
                msg["content"] = stripped if stripped else content
                modified = True
        if modified:
            body_bytes = json.dumps(resp_json).encode()
    except Exception:
        pass
    return status_code, resp_headers_dict, body_bytes


def save_stats():
    try:
        STATS_FILE.write_text(json.dumps(stats, indent=2))
    except Exception:
        pass


# === ALIAS MAPPING ===
MODEL_ALIASES = {
    "auto": None, "smart": None, "router": None, "": None,
    "free": 0, "gemini": 0, "vertex": 0,
    "cheap": 1, "haiku": 1, "fast": 1,
    "medium": 2, "balanced": 2, "sonnet": 2,
    "expensive": 3, "premium": 3, "opus": 3, "best": 3,
    "claude-haiku": 1, "claude-sonnet": 2, "claude-opus": 3,
}


class RouterHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/v1/models", "/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            models_list = [
                {"id": name, "object": "model", "owned_by": f"tier-{info[2]}-{info[3].lower()}"}
                for name, info in MODEL_REGISTRY.items()
            ]
            # Add aliases
            for alias in ["auto", "free", "cheap", "medium", "expensive"]:
                models_list.append({"id": alias, "object": "model", "owned_by": "router-alias"})
            self.wfile.write(json.dumps({"data": models_list, "object": "list"}).encode())
        elif self.path == "/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats, indent=2).encode())
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        if self.path in ("/v1/chat/completions", "/chat/completions"):
            self.handle_chat(body)
        elif self.path in ("/v1/embeddings", "/embeddings"):
            self.handle_embeddings(body)
        elif self.path in ("/v1/images/generations", "/images/generations"):
            self.handle_images(body)
        elif self.path in ("/v1/tools/youtube", "/tools/youtube"):
            self.handle_youtube(body)
        elif self.path in ("/v1/tools/search", "/tools/search"):
            self.handle_search(body)
        elif self.path in ("/v1/tools/translate", "/tools/translate"):
            self.handle_translate(body)
        elif self.path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
            self.handle_transcriptions(body)
        elif self.path in ("/v1/audio/speech", "/audio/speech"):
            self.handle_speech(body)
        else:
            self.send_error(404)

    def handle_images(self, body):
        """Handler pentru /v1/images/generations — OpenAI-compat image generation via Imagen 3."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        model_alias = data.get("model", "imagen-3")
        if model_alias not in MODEL_REGISTRY:
            model_alias = "imagen-3"

        backend_name, actual_model, _, _ = MODEL_REGISTRY.get(model_alias, ("vertex-local-imagen", "imagen-3.0-generate-002", 0, "IMAGEN"))

        try:
            status, resp_headers, resp_body = proxy_vertex_imagen(actual_model, data)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            # P0.4: content filter returneaza 400 — nu fallback pe chat
            err_body = e.read().decode('utf-8', errors='replace')
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": err_body[:300]}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_youtube(self, body):
        """Handler pentru /v1/tools/youtube — YouTube search + transcript + analyze.

        Input (search):     {"query": "...", "max_results": 5}
        Input (transcript): {"action": "transcript", "video_id": "...", "language": "en"}
        Input (analyze):    {"action": "analyze", "video_id": "...", "question": "..."}
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        action = data.get("action", "search")

        if action in ("transcript", "analyze"):
            video_id = data.get("video_id", "") or data.get("url", "")
            language = data.get("language", "en")
            question = data.get("question", "Summarize this video in detail.")

            # Extrage video_id din URL dacă e nevoie
            if video_id and ("youtube.com" in video_id or "youtu.be" in video_id):
                # https://www.youtube.com/watch?v=XXXX sau https://youtu.be/XXXX
                if "v=" in video_id:
                    video_id = video_id.split("v=")[1].split("&")[0]
                elif "youtu.be/" in video_id:
                    video_id = video_id.split("youtu.be/")[1].split("?")[0]

            if not video_id:
                self._json_error(400, "Missing 'video_id' or 'url'")
                return
            if not _TRANSCRIPT_API_AVAILABLE:
                self._json_error(503, "youtube-transcript-api not installed. Run: pip install youtube-transcript-api")
                return

            try:
                # youtube-transcript-api v0.6.x: class method; v2.x: instance method
                # Suportam ambele versiuni
                try:
                    ytt = _YTTranscriptApi()
                    transcript_list = ytt.fetch(video_id)
                except TypeError:
                    # v0.6.x: fetch nu e metoda de instanta
                    transcript_list = _YTTranscriptApi.get_transcript(video_id, languages=[language, "en"])

                # Normalizeaza: lista de {text, start, duration}
                segments = []
                for t in transcript_list:
                    if hasattr(t, "text"):
                        segments.append({"text": t.text, "start": getattr(t, "start", 0), "duration": getattr(t, "duration", 0)})
                    elif isinstance(t, dict):
                        segments.append(t)

                # Curata artefacte: [Music], [Applause], newlines etc.
                full_text = " ".join(
                    re.sub(r"\[.*?\]", "", s.get("text", "")).replace("\n", " ").strip()
                    for s in segments
                    if s.get("text", "").strip()
                )
                full_text = re.sub(r"\s+", " ", full_text).strip()

                if action == "transcript":
                    self._json_ok({"video_id": video_id, "language": language,
                                   "segments": segments, "text": full_text})
                else:  # analyze
                    prompt = f"{question}\n\nVideo Transcript:\n{full_text[:15000]}"
                    payload = {"messages": [{"role": "user", "content": prompt}], "max_tokens": 2048}
                    _, _, resp_body = proxy_vertex_gemini("gemini-2.5-flash", payload)
                    resp = json.loads(resp_body)
                    answer = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    self._json_ok({"video_id": video_id, "question": question,
                                   "answer": answer, "transcript_chars": len(full_text)})

            except Exception as e:
                # Eroare clara per tip de problema
                err_str = str(e)
                if "disabled" in err_str.lower() or "unavailable" in err_str.lower():
                    self._json_error(404, f"Transcripts disabled or unavailable for video '{video_id}': {err_str}")
                elif "429" in err_str or "too many" in err_str.lower():
                    self._json_error(429, f"YouTube rate limit hit. Wait before retrying: {err_str}")
                elif "private" in err_str.lower() or "age" in err_str.lower():
                    self._json_error(403, f"Video is private or age-gated: {err_str}")
                else:
                    self._json_error(500, f"Transcript error: {err_str}")
            return

        # Default: action=search (original behavior)
        query       = data.get("query", "") or data.get("q", "")
        max_results = int(data.get("max_results", 5))

        if not query:
            self._json_error(400, "Missing 'query' field")
            return

        results, text = proxy_youtube_search(query, max_results)
        self._json_ok({"results": results or [], "text": text})

    def _json_ok(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _json_error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def handle_search(self, body):
        """POST /v1/tools/search — Brave Search API or Gemini grounding fallback.

        Input:  {"query": "...", "count": 5}
        Output: {"results": [...], "text": "formatted"}
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        query = data.get("query", "") or data.get("q", "")
        count = int(data.get("count", 5))
        if not query:
            self._json_error(400, "Missing 'query' field")
            return
        try:
            results, text = proxy_brave_search(query, count)
            self._json_ok({"results": results, "text": text})
        except Exception as e:
            self._json_error(500, f"Search error: {e}")

    def handle_translate(self, body):
        """POST /v1/tools/translate — Gemini-powered translation.

        Input:  {"text": "...", "target": "ro", "source": "en" (optional)}
        Output: {"translated": "...", "target": "ro"}
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        text        = data.get("text", "")
        target_lang = data.get("target", data.get("target_lang", "en"))
        source_lang = data.get("source", data.get("source_lang", None))
        if not text:
            self._json_error(400, "Missing 'text' field")
            return
        try:
            translated = proxy_translate(text, target_lang, source_lang)
            self._json_ok({"translated": translated, "target": target_lang})
        except Exception as e:
            self._json_error(500, f"Translation error: {e}")

    def handle_transcriptions(self, body):
        """POST /v1/audio/transcriptions — OpenAI-compatible STT via Gemini.

        Input JSON: {"audio_base64": "...", "mime_type": "audio/mp3", "language": "en"}
        OR multipart/form-data with 'file' field (base64-encodes the bytes).
        Output: {"text": "transcript"}
        """
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" in content_type:
            # Parse multipart: extract 'file' field and encode to base64
            try:
                audio_bytes, mime_type, language = _parse_multipart_audio(body, content_type)
                audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            except Exception as e:
                self._json_error(400, f"Multipart parse error: {e}")
                return
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            audio_b64 = data.get("audio_base64", "")
            mime_type = data.get("mime_type", "audio/mp3")
            language  = data.get("language", "en")

        if not audio_b64:
            self._json_error(400, "Missing audio data")
            return
        # P0.7: Gemini rejects inlineData > 20MB (raw bytes, not base64 size)
        raw_bytes_est = len(audio_b64) * 3 // 4
        if raw_bytes_est > 20 * 1024 * 1024:
            self._json_error(413, f"Audio too large ({raw_bytes_est // (1024*1024)}MB). Gemini STT limit is 20MB.")
            return
        try:
            transcript = proxy_gemini_transcribe(audio_b64, mime_type, language)
            self._json_ok({"text": transcript})
        except Exception as e:
            self._json_error(500, f"Transcription error: {e}")

    def handle_speech(self, body):
        """POST /v1/audio/speech — OpenAI-compatible TTS via Cloud TTS Chirp 3 HD.

        Input:  {"input": "text", "voice": "alloy"|"en-US-Chirp3-HD-Kore",
                 "response_format": "mp3", "speed": 1.0}
        Output: raw MP3 audio bytes (binary response, same as OpenAI)
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        text  = data.get("input", "")
        voice = data.get("voice", "alloy")
        speed = float(data.get("speed", 1.0))
        if not text:
            self._json_error(400, "Missing 'input' field")
            return
        # P0.10: Cloud TTS hard limit is 5000 bytes (not chars, but conservative check)
        if len(text) > 5000:
            self._json_error(400, f"Text too long ({len(text)} chars). Cloud TTS limit is 5000 chars.")
            return
        try:
            audio_b64, mime_type = proxy_tts(text, voice, speed)
            audio_bytes = base64.b64decode(audio_b64)
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.end_headers()
            self.wfile.write(audio_bytes)
        except Exception as e:
            self._json_error(500, str(e))

    def handle_chat(self, body):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        requested_model = data.get("model", "auto")
        messages = data.get("messages", [])

        # Determine tier and model
        if requested_model in MODEL_ALIASES:
            forced_tier = MODEL_ALIASES[requested_model]
            if forced_tier is None:
                tier = classify_tier(messages)
            else:
                tier = forced_tier
            target_model_name = select_model(tier)
        elif requested_model in MODEL_REGISTRY:
            target_model_name = requested_model
            tier = MODEL_REGISTRY[requested_model][2]
        else:
            # Unknown model -- try passthrough to CCS
            target_model_name = requested_model
            tier = -1

        if target_model_name in MODEL_REGISTRY:
            backend_name, actual_model, _, cost_label = MODEL_REGISTRY[target_model_name]
        else:
            backend_name, actual_model, cost_label = "ccs", target_model_name, "PASSTHROUGH"

        data["model"] = actual_model
        modified_body = json.dumps(data).encode()

        # Log
        user_preview = ""
        if messages:
            last = messages[-1].get("content", "")
            if isinstance(last, str):
                user_preview = last[:50].replace("\n", " ")
        ts = datetime.now().strftime("%H:%M:%S")
        tier_label = f"T{tier}" if tier >= 0 else "PT"
        print(f"[{ts}] {requested_model:15s} -> {tier_label} {cost_label:10s} {actual_model:30s} via {backend_name:6s} | \"{user_preview}...\"")

        # P0.2 fix: modele non-chat (imagen, youtube) nu sunt rutabile via handle_chat
        NON_CHAT_BACKENDS = {"vertex-local-imagen", "youtube"}
        if backend_name in NON_CHAT_BACKENDS:
            err = f"Model '{target_model_name}' is not a chat model. " \
                  f"Use /v1/images/generations for imagen or /v1/tools/youtube for search."
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": err, "type": "invalid_request_error"}}).encode())
            return

        # Forward — dispatch pe backend type
        try:
            result = None
            if backend_name == "vertex-local-llama4":
                result = proxy_vertex_llama4(actual_model, data, handler=self, region="us-east5")
            elif backend_name == "vertex-local-global":
                result = proxy_vertex_llama4(actual_model, data, handler=self, region="global")
            elif backend_name == "vertex-local-central":
                # us-central1 MaaS: DeepSeek R1-0528 + alte modele specifice acestei regiuni
                result = proxy_vertex_llama4(actual_model, data, handler=self, region="us-central1")
            elif backend_name == "vertex-local-gemini":
                result = proxy_vertex_gemini(actual_model, data, handler=self)
            elif backend_name == "vertex-local-gemini-grounded":
                result = proxy_vertex_gemini(actual_model, data, handler=self, grounded=True)
            elif backend_name == "vertex-local-gemini-code":
                # Gemini cu Code Execution sandbox — rulează Python în siguranță, zero cost extra
                result = proxy_vertex_gemini(actual_model, data, handler=self, code_exec=True)
            elif backend_name == "vertex-local-endpoint":
                # Self-deploy: actual_model conține resource name complet
                result = proxy_vertex_rawpredict(actual_model, data, handler=self)
            else:
                result = proxy_to_backend(
                    backend_name, "/chat/completions", "POST", dict(self.headers), modified_body,
                    handler=self
                )

            if result is None:
                # Streaming: already sent
                with stats_lock:
                    stats["total_requests"] += 1
                    stats["routed"][actual_model] = stats["routed"].get(actual_model, 0) + 1
                    if tier >= 0:
                        stats["tier_usage"][tier] = stats["tier_usage"].get(tier, 0) + 1
                    if tier in (0, 1):
                        stats["quota_saved_requests"] += 1
                    save_stats()
                return

            status, resp_headers, resp_body = result

            with stats_lock:
                stats["total_requests"] += 1
                stats["routed"][actual_model] = stats["routed"].get(actual_model, 0) + 1
                if tier >= 0:
                    stats["tier_usage"][tier] = stats["tier_usage"].get(tier, 0) + 1
                if tier in (0, 1):
                    stats["quota_saved_requests"] += 1
                try:
                    resp_data = json.loads(resp_body)
                    usage = resp_data.get("usage", {})
                    stats["tokens"]["input"] += usage.get("prompt_tokens", 0)
                    stats["tokens"]["output"] += usage.get("completion_tokens", 0)
                except Exception:
                    pass
                save_stats()

            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            with stats_lock:
                stats["errors"] += 1
            # Fallback: try next tier
            if 0 <= tier < 3:
                fallback_tier = tier + 1
                fallback_model = select_model(fallback_tier)
                fb_backend, fb_actual, _, fb_cost = MODEL_REGISTRY[fallback_model]
                fallback_data = dict(data)
                fallback_data["model"] = fb_actual
                print(f"[{ts}] FALLBACK -> T{fallback_tier} {fb_cost} {fb_actual} via {fb_backend}")
                try:
                    if fb_backend == "vertex-local-llama4":
                        fb_result = proxy_vertex_llama4(fb_actual, fallback_data, handler=self, region="us-east5")
                    elif fb_backend == "vertex-local-global":
                        fb_result = proxy_vertex_llama4(fb_actual, fallback_data, handler=self, region="global")
                    elif fb_backend == "vertex-local-central":
                        fb_result = proxy_vertex_llama4(fb_actual, fallback_data, handler=self, region="us-central1")
                    elif fb_backend == "vertex-local-gemini":
                        fb_result = proxy_vertex_gemini(fb_actual, fallback_data, handler=self)
                    elif fb_backend == "vertex-local-gemini-grounded":
                        fb_result = proxy_vertex_gemini(fb_actual, fallback_data, handler=self, grounded=True)
                    elif fb_backend == "vertex-local-gemini-code":
                        fb_result = proxy_vertex_gemini(fb_actual, fallback_data, handler=self, code_exec=True)
                    else:
                        fb_result = proxy_to_backend(fb_backend, "/chat/completions", "POST", dict(self.headers), json.dumps(fallback_data).encode(), handler=self)
                    if fb_result is None:
                        return
                    s2, h2, b2 = fb_result
                    self.send_response(s2)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b2)
                    return
                except Exception:
                    pass
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_body.encode() if isinstance(error_body, str) else error_body)
        except Exception as e:
            with stats_lock:
                stats["errors"] += 1
            self.send_error(502, f"Backend error: {e}")


    def handle_embeddings(self, body):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        requested_model = data.get("model", "text-embedding-004")

        # Normalizare alias → model din registry
        if requested_model in MODEL_REGISTRY:
            backend_name, actual_model, tier, cost_label = MODEL_REGISTRY[requested_model]
        else:
            # Fallback: orice model necunoscut → text-embedding-004 Vertex
            backend_name, actual_model, tier, cost_label = "vertex-local-embed", "text-embedding-004", 0, "FREE"

        ts = datetime.now().strftime("%H:%M:%S")
        input_preview = str(data.get("input", ""))[:50].replace("\n", " ")
        print(f"[{ts}] EMBED {requested_model:25s} -> {cost_label:5s} {actual_model:35s} | \"{input_preview}...\"")

        try:
            if backend_name == "vertex-local-embed":
                status, headers, resp_body = proxy_vertex_embeddings(actual_model, data)
            else:
                # Fallback la CCS dacă modelul e acolo
                data["model"] = actual_model
                modified = json.dumps(data).encode()
                status, headers, resp_body = proxy_to_backend(
                    backend_name, "/embeddings", "POST", dict(self.headers), modified
                )

            with stats_lock:
                stats["total_requests"] += 1
                stats["routed"][actual_model] = stats["routed"].get(actual_model, 0) + 1
                stats["tier_usage"][tier] = stats["tier_usage"].get(tier, 0) + 1
                if tier in (0, 1):
                    stats["quota_saved_requests"] += 1
                save_stats()

            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            with stats_lock:
                stats["errors"] += 1
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_body.encode() if isinstance(error_body, str) else error_body)
        except Exception as e:
            with stats_lock:
                stats["errors"] += 1
            self.send_error(502, f"Embedding error: {e}")


def run_tests():
    test_cases = [
        ([{"role": "user", "content": "hi"}], 0),
        ([{"role": "user", "content": "rename this variable"}], 0),
        ([{"role": "user", "content": "explain what this does"}], 0),
        ([{"role": "user", "content": "yes"}], 0),
        ([{"role": "user", "content": "format this code"}], 0),
        ([{"role": "user", "content": "translate to Romanian"}], 0),
        ([{"role": "user", "content": "debug this function that returns null when given empty input"}], 2),
        ([{"role": "user", "content": "implement a REST API endpoint for user registration with validation"}], 2),
        ([{"role": "user", "content": "refactor this component to use React hooks instead of class state"}], 2),
        ([{"role": "user", "content": "write unit tests for the authentication module"}], 2),
        ([{"role": "user", "content": "fix bug in the payment processing pipeline causing duplicate charges"}], 2),
        ([{"role": "user", "content": "design system architecture for a real-time collaborative editor"}], 3),
        ([{"role": "user", "content": "architect a distributed event-driven microservices platform with CQRS"}], 3),
        ([{"role": "user", "content": "comprehensive security audit of the entire authentication and authorization flow"}], 3),
        ([{"role": "user", "content": "analyze deeply the performance implications and trade-off of migrating to GraphQL"}], 3),
        ([{"role": "user", "content": "research the best strategy for database migration from PostgreSQL to CockroachDB"}], 3),
    ]

    print("=== Smart Router v2.0 -- Routing Logic Tests ===\n")
    passed = failed = 0
    tier_names = {0: "FREE", 1: "CHEAP", 2: "MEDIUM", 3: "PREMIUM"}
    for msgs, expected in test_cases:
        result = classify_tier(msgs)
        ok = result == expected
        passed += ok
        failed += not ok
        prompt = msgs[0]["content"][:55]
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] expect={tier_names[expected]:7s} got={tier_names.get(result,'?'):7s} | \"{prompt}...\"")

    print(f"\n  Results: {passed}/{passed + failed} passed")
    return failed == 0


def run_live():
    print("\n=== Live End-to-End Tests ===\n")
    tests = [
        ("auto (simple)", "auto", "Say hello", 0),
        ("auto (medium)", "auto", "Debug this Python function that crashes on empty lists", 2),
        ("auto (complex)", "auto", "Design a comprehensive architecture for a real-time analytics platform", 3),
        ("free (forced)", "free", "What is 2+2?", 0),
        ("cheap (forced)", "cheap", "Hello world", 1),
    ]
    for label, model, prompt, expected_tier in tests:
        tier = classify_tier([{"role": "user", "content": prompt}])
        target = select_model(tier if model in ("auto", "smart", "router") else MODEL_ALIASES.get(model, tier))
        backend_name, actual_model, _, cost_label = MODEL_REGISTRY[target]
        payload_dict = {
            "model": actual_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 50
        }
        start = time.time()
        try:
            if backend_name == "vertex-local-llama4":
                status, _, resp_body = proxy_vertex_llama4(actual_model, payload_dict)
                data = json.loads(resp_body)
            elif backend_name == "vertex-local-gemini":
                status, _, resp_body = proxy_vertex_gemini(actual_model, payload_dict)
                data = json.loads(resp_body)
            else:
                backend = BACKENDS[backend_name]
                req = urllib.request.Request(
                    f"{backend['url']}/chat/completions",
                    data=json.dumps(payload_dict).encode(),
                    headers={"Authorization": f"Bearer {backend['key']}", "Content-Type": "application/json"}
                )
                resp = urllib.request.urlopen(req, timeout=30)
                data = json.loads(resp.read())
            elapsed = time.time() - start
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "(empty)")
            safe_content = (content or "(empty)")[:40].encode("ascii", "replace").decode("ascii")
            print(f"  [OK] {label:20s} -> T{tier} {cost_label:8s} {actual_model:30s} | {elapsed:.1f}s | {safe_content}")
        except Exception as e:
            print(f"  [FAIL] {label:20s} -> {actual_model}: {e}")
    print()


def main():
    if "--test" in sys.argv:
        ok = run_tests()
        run_live()
        sys.exit(0 if ok else 1)

    if "--live" in sys.argv:
        run_live()
        return

    port = LISTEN_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    # Startup warnings pentru API keys / optional deps lipsa
    if not YOUTUBE_API_KEY:
        print("[WARN] YOUTUBE_API_KEY not set -- youtube-search unavailable.")
        print("       Set: $env:YOUTUBE_API_KEY='AIza...'")
    if not BRAVE_API_KEY:
        print("[WARN] BRAVE_API_KEY not set -- /v1/tools/search uses Gemini grounding fallback.")
        print("       Set: $env:BRAVE_API_KEY='BSA...'  (https://api.search.brave.com)")
    if not _TRANSCRIPT_API_AVAILABLE:
        print("[WARN] youtube-transcript-api not installed -- transcript/analyze unavailable.")
        print("       Fix: pip install youtube-transcript-api")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), RouterHandler)
    print(f"""
+======================================================+
|          Smart LLM Router v2.4                        |
+======================================================+
|  Listen:   http://localhost:{port:<5}                   |
|  Stats:    http://localhost:{port}/stats                |
|  Health:   http://localhost:{port}/health               |
|  Chat:     POST /v1/chat/completions                  |
|  Images:   POST /v1/images/generations (Imagen 3)    |
|  YouTube:  POST /v1/tools/youtube (search+transcript) |
|  Search:   POST /v1/tools/search   (Brave/Gemini)    |
|  Translate:POST /v1/tools/translate (Gemini)         |
|  STT:      POST /v1/audio/transcriptions (Gemini)    |
|  TTS:      POST /v1/audio/speech   (Chirp 3 HD)      |
|                                                       |
|  Backends:                                            |
|    CCS:    localhost:8317 (Claude + GPT)              |
|    Vertex: SA key local (Llama 4 + Gemini -- FREE)   |
|                                                       |
|  Routing (model= parameter):                         |
|    auto/smart       -> detect complexity              |
|    free/gemini      -> Gemini 2.5 Flash (Vertex)     |
|    gemini-vision    -> Gemini 2.5 Flash (image/audio)|
|    llama-4-maverick -> Llama 4 (us-east5)            |
|    cheap/haiku      -> Claude Haiku ($)              |
|    medium/sonnet    -> Claude Sonnet ($$)             |
|    expensive/opus   -> Claude Opus ($$$)              |
|                                                       |
|  Tiers:                                               |
|    T0 FREE:    Llama 4 / Gemini (Vertex SA key)       |
|    T1 CHEAP:   Haiku / GPT-5.4-mini (subscription)   |
|    T2 MEDIUM:  Sonnet (subscription)                  |
|    T3 PREMIUM: Opus (subscription)                    |
|                                                       |
|  Fallback: T0->T1->T2->T3 on errors                  |
+======================================================+
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        save_stats()
        server.server_close()


if __name__ == "__main__":
    main()
