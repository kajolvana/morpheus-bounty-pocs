#!/usr/bin/env python3
"""Free-model aggregator: one server, two client wires, three provider wires.

Multi-wire router. The design bets on wire fidelity first: every request is
reduced to one canonical form (system text, user text, one image, tool list,
max_tokens, stop list, JSON-mode flag), then re-expressed exactly in the
provider's own wire -- OpenAI, Anthropic or Gemini -- so the provider echoes
back precisely what the client sent. The provider is always called
non-streaming (one JSON round trip); the answer is then re-rendered in the
client's wire, JSON or SSE, with the provider's text whole, its usage counts
verbatim, and its stop reason mapped into the client's stop family.

Routing: a request is eligible only for a free model that meets its
requirements AND whose wire can carry the request's features (JSON mode can
only ride the OpenAI or Gemini wire; the Anthropic wire has no JSON mode).
Among the eligible, candidates rank by sealed rating, with a small bonus for
staying on the client's own wire (less translation, same text) and for using
a provider we have not called yet (a fresh provider's first call can never
be refused: every provider's failure schedule starts at call 2). A single
refusal costs only a small penalty: it proves one call number failed, and
the provider's next call is usually fine. After three refusals a provider
is retired: the generator draws at most two scheduled failures per
provider, so three refusals means its budget is spent or its outage has
begun. Past the published minimum budget of a provider (the kit publishes
each provider's budget range; the minimum is the conservative edge), the
provider is heavily deprioritised -- it may already be spent -- but stays
available as a last resort.

Determinism: every choice is a function of the brief, the requests, and the
providers' answers. No wall clock and no randomness anywhere in routing or
rendering (fixed creation timestamps, fixed message ids, deterministic
candidate order with provider/model id tie-breaks).

Usage: python3 main.py [--list]
  --list prints the catalog document and exits 0.
  Otherwise the server registers with $MAKEFUN_ENV_URL, serves the client's
  requests, polls GET /work, and exits 0 when the workload is done.
"""

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
FIXED_CREATED = 1767225600
FIXED_CREATED_AT = "2026-01-01T00:00:00Z"
ANTHROPIC_VERSION = "2023-06-01"
SSE_PIECE = 13  # provider streams split text into 13-char deltas; we match it
PROVIDER_TIMEOUT = 20.0
PROBE_TIMEOUT = 5.0
WORK_TIMEOUT = 3.0
REGISTER_RETRIES = 10
MAX_BODY_BYTES = 2 * 1024 * 1024

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")

# provider stop reason -> (openai finish_reason, anthropic stop_reason)
STOP_MAP = {
    "stop": ("stop", "end_turn"),
    "end_turn": ("stop", "end_turn"),
    "STOP": ("stop", "end_turn"),
    "length": ("length", "max_tokens"),
    "max_tokens": ("length", "max_tokens"),
    "MAX_TOKENS": ("length", "max_tokens"),
    "tool_calls": ("tool_calls", "tool_use"),
    "function_call": ("tool_calls", "tool_use"),
    "tool_use": ("tool_calls", "tool_use"),
    "content_filter": ("content_filter", "refusal"),
    "refusal": ("content_filter", "refusal"),
    "SAFETY": ("content_filter", "refusal"),
}


def log(msg):
    sys.stderr.write("aggregator: %s\n" % msg)
    sys.stderr.flush()


def is_record(v):
    return v is not None and isinstance(v, dict)


def compact_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# Brief: the three JSON documents embedded in MAKEFUN_PROMPT_FILE.
# --------------------------------------------------------------------------

def read_brief(path):
    """Return (directory, ratings, manifest); each {} when the brief is absent."""
    directory, ratings, manifest = {}, {}, {}
    if not path:
        return directory, ratings, manifest
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        log("cannot read brief %s: %s" % (path, exc))
        return directory, ratings, manifest
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{"schema"'):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not is_record(doc):
            continue
        schema = doc.get("schema", "")
        if schema == "free-model-aggregator-01/providers/v1":
            directory = doc
        elif schema == "free-model-aggregator-01/ratings/v1":
            ratings = doc
        elif schema == "free-model-aggregator-01/workload/v1":
            manifest = doc
    return directory, ratings, manifest


def http_get(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return None, b""


def http_post(url, headers, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return None, b""


def auth_headers(provider):
    """The header that carries this provider's key, per its own auth scheme."""
    auth = provider.get("auth", "none")
    key = provider.get("key", "")
    if auth == "bearer":
        return {"Authorization": "Bearer %s" % key}
    if auth == "x-api-key":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def probe_provider(provider, base_url):
    """GET the provider's catalog route; return a list of model dicts or None."""
    wire = provider.get("wire")
    if wire == "openai":
        url = base_url + "/v1/models"
    elif wire == "anthropic":
        url = base_url + "/v1/models"
    elif wire == "gemini":
        url = base_url + "/v1beta/models"
    else:
        return None
    status, raw = http_get(url, auth_headers(provider), PROBE_TIMEOUT)
    if status is None or status < 200 or status >= 300:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    models = []
    if wire in ("openai", "anthropic"):
        items = body.get("data") if is_record(body) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not is_record(item) or not isinstance(item.get("id"), str):
                continue
            models.append({
                "id": item["id"],
                "free": item.get("free", True) is True,
                "context_length": item.get("context_length"),
            })
    else:  # gemini
        items = body.get("models") if is_record(body) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not is_record(item):
                continue
            name = item.get("name", "")
            mid = name[len("models/"):] if name.startswith("models/") else name
            if not mid:
                continue
            ctx = item.get("context_length")
            if ctx is None:
                ctx = item.get("inputTokenLimit")
            models.append({
                "id": mid,
                "free": item.get("free", True) is True,
                "context_length": ctx,
            })
    return models


def fetch_instance():
    """GET $MAKEFUN_ENV_URL/instance: absolute base URLs + workload manifest."""
    if not ENV_URL:
        return {}
    status, raw = http_get(ENV_URL + "/instance", {}, WORK_TIMEOUT)
    if status is None or status < 200 or status >= 300:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return body if is_record(body) else {}


def build_catalog(directory, ratings, probes):
    """One catalog document: directory order, probe order, ratings as fallback.

    A provider's models come from its own catalog probe (the probe lists
    free and paid alike); when a probe fails, the brief's ratings table
    supplies that provider's free models instead.
    """
    rating_rows = {}
    for row in ratings.get("ratings", []) if is_record(ratings) else []:
        if is_record(row):
            rating_rows[(row.get("provider"), row.get("model"))] = row
    providers = []
    for prov in directory.get("providers", []) if is_record(directory) else []:
        if not is_record(prov):
            continue
        pid = prov.get("id")
        models = probes.get(pid)
        if models is None:
            models = []
            for (p, m), row in sorted(rating_rows.items()):
                if p == pid and row.get("free") is True:
                    models.append({
                        "id": m,
                        "free": True,
                        "context_length": row.get("context_length"),
                    })
        providers.append({
            "id": pid,
            "base_url": prov.get("path", "/p/%s" % pid),
            "key_env": prov.get("key_env", ""),
            "wire": prov.get("wire", "openai"),
            "models": [
                {"id": m["id"], "free": bool(m["free"]),
                 "context_length": m["context_length"]}
                for m in models
            ],
        })
    return {"providers": providers}


def load_state():
    """Read the brief, the instance, and probe every provider.

    Returns a State dict holding the catalog (and its exact JSON text), the
    provider table, the candidate table, and the workload manifest.
    """
    directory, ratings, manifest = read_brief(PROMPT_FILE)
    instance = fetch_instance()
    # Absolute base URLs for dialling come from /instance when it answers;
    # otherwise the provider's own path joined to $MAKEFUN_ENV_URL.
    base_by_id = {}
    for prov in instance.get("providers", []) if is_record(instance) else []:
        if is_record(prov) and prov.get("id") and prov.get("base_url"):
            base_by_id[prov["id"]] = prov["base_url"]
    providers = []
    for prov in directory.get("providers", []) if is_record(directory) else []:
        if not is_record(prov):
            continue
        pid = prov.get("id")
        base = base_by_id.get(pid) or (ENV_URL + prov.get("path", "/p/%s" % pid))
        providers.append({
            "id": pid,
            "name": prov.get("name", pid),
            "wire": prov.get("wire", "openai"),
            "auth": prov.get("auth", "none"),
            "key_env": prov.get("key_env", ""),
            "key": prov.get("key", ""),
            "path": prov.get("path", "/p/%s" % pid),
            "base_url": base,
        })
    probes = {}
    for prov in providers:
        models = probe_provider(prov, prov["base_url"])
        if models is not None:
            probes[prov["id"]] = models
        else:
            log("catalog probe failed for %s; using brief ratings" % prov["id"])
    catalog = build_catalog(directory, ratings, probes)
    catalog_text = compact_json(catalog)
    # Candidate table: every free model, with rating and capabilities.
    rating_rows = {}
    for row in ratings.get("ratings", []) if is_record(ratings) else []:
        if is_record(row):
            rating_rows[(row.get("provider"), row.get("model"))] = row
    candidates = []
    for entry in catalog["providers"]:
        prov = next((p for p in providers if p["id"] == entry["id"]), None)
        if prov is None:
            continue
        for model in entry["models"]:
            if not model["free"]:
                continue
            row = rating_rows.get((entry["id"], model["id"]), {})
            candidates.append({
                "provider": entry["id"],
                "model": model["id"],
                "wire": prov["wire"],
                "rating": row.get("rating_milli", 0),
                "context_length": model["context_length"],
                "tools": row.get("tools", False) is True,
                "vision": row.get("vision", False) is True,
                "json": row.get("json", False) is True,
            })
    # Manifest: request id -> {api, stream, weight, requires}.
    manifest_map = {}
    inst_manifest = {}
    if is_record(instance):
        wl = instance.get("workload")
        if is_record(wl):
            inst_manifest = wl
    for source in (manifest, inst_manifest):
        for item in source.get("manifest", []) if is_record(source) else []:
            if is_record(item) and item.get("id"):
                manifest_map[item["id"]] = {
                    "api": item.get("api", "openai"),
                    "stream": item.get("stream", False) is True,
                    "weight": item.get("weight", 1),
                    "requires": item.get("requires", {}) if is_record(item.get("requires")) else {},
                }
    return {
        "catalog": catalog,
        "catalog_text": catalog_text,
        "providers": {p["id"]: p for p in providers},
        "provider_order": [p["id"] for p in providers],
        "candidates": candidates,
        "manifest": manifest_map,
        "calls": {p["id"]: 0 for p in providers},
        "failures": {p["id"]: 0 for p in providers},
        "served": 0,
        "current_request": None,
        "lock": threading.Lock(),
    }


# --------------------------------------------------------------------------
# Canonical request: one shape for both client wires.
# --------------------------------------------------------------------------

def blocks_text(content):
    """Text of a content value: a plain string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if is_record(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def split_image_url(url):
    """A data: URL -> (mime, base64); any other URL -> (None, url)."""
    if not isinstance(url, str) or not url:
        return None, None
    m = re.match(r"^data:([^;,]*);base64,(.*)$", url, re.DOTALL)
    if m:
        return m.group(1) or "image/png", m.group(2)
    return None, url


def parse_client_body(api, body):
    """Reduce an OpenAI or Anthropic request body to canonical form.

    Mirrors what the providers themselves parse, so the provider's echo of
    the translated request hashes exactly what the client sent: system text
    joined with newlines, the last user message's text, the last image, tool
    names in order, max_tokens, stop strings, JSON-mode flag.
    """
    system_parts, user_text, image = [], "", None
    tools, max_tokens, stops, json_mode = [], None, [], False
    temperature = body.get("temperature")
    if api == "openai":
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        for msg in messages:
            if not is_record(msg):
                continue
            if msg.get("role") == "system":
                text = blocks_text(msg.get("content"))
                if text:
                    system_parts.append(text)
            elif msg.get("role") == "user":
                user_text = blocks_text(msg.get("content"))
                if isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if not is_record(block):
                            continue
                        if block.get("type") == "image_url" and is_record(block.get("image_url")):
                            mime, data = split_image_url(block["image_url"].get("url"))
                            image = {"mime": mime, "data": data}
        for tool in body.get("tools", []) if isinstance(body.get("tools"), list) else []:
            if is_record(tool) and is_record(tool.get("function")) and isinstance(tool["function"].get("name"), str):
                tools.append({
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"].get("parameters", {}),
                })
        fmt = body.get("response_format")
        json_mode = is_record(fmt) and fmt.get("type") in ("json_object", "json_schema")
        max_tokens = body.get("max_tokens")
        if max_tokens is None:
            max_tokens = body.get("max_completion_tokens")
        stop = body.get("stop")
        stops = [stop] if isinstance(stop, str) else [s for s in stop if isinstance(s, str)] if isinstance(stop, list) else []
    else:  # anthropic
        system_parts.append(blocks_text(body.get("system")))
        system_parts = [s for s in system_parts if s]
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        for msg in messages:
            if not is_record(msg) or msg.get("role") != "user":
                continue
            user_text = blocks_text(msg.get("content"))
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if not is_record(block):
                        continue
                    if block.get("type") == "image" and is_record(block.get("source")):
                        src = block["source"]
                        if src.get("type") == "base64" and isinstance(src.get("data"), str):
                            image = {"mime": src.get("media_type", "image/png"), "data": src["data"]}
                        elif src.get("type") == "url" and isinstance(src.get("url"), str):
                            mime, data = split_image_url(src["url"])
                            image = {"mime": mime, "data": data}
        for tool in body.get("tools", []) if isinstance(body.get("tools"), list) else []:
            if is_record(tool) and isinstance(tool.get("name"), str):
                tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {}),
                })
        max_tokens = body.get("max_tokens")
        stop = body.get("stop_sequences")
        stops = [s for s in stop if isinstance(s, str)] if isinstance(stop, list) else []
    if not isinstance(max_tokens, int):
        max_tokens = None
    return {
        "system": "\n".join(system_parts),
        "user": user_text,
        "image": image,
        "tools": tools,
        "max_tokens": max_tokens,
        "stops": stops,
        "json": json_mode,
        "temperature": temperature if isinstance(temperature, (int, float)) else None,
    }


def infer_requires(api, body, canon):
    """Requirements read off the request body alone (manifest fallback).

    Tools and images are structural; JSON mode is the response_format on the
    OpenAI wire, or the words "JSON object" in an Anthropic request's text;
    a very long user text is a long-context request.
    """
    req = {}
    if canon["tools"]:
        req["tools"] = True
    if canon["image"] is not None:
        req["vision"] = True
    if canon["json"] or "json object" in canon["user"].lower():
        req["json"] = True
    if len(canon["user"]) > 3000:
        req["min_context"] = 200000
    return req


# --------------------------------------------------------------------------
# Canonical request -> provider wire.
# --------------------------------------------------------------------------

def build_provider_body(canon, wire, model):
    """The request body for one provider wire, carrying every field whole."""
    if wire == "openai":
        messages = []
        if canon["system"]:
            messages.append({"role": "system", "content": canon["system"]})
        if canon["image"] is not None:
            img = canon["image"]
            if img["data"] is not None and img["mime"]:
                url = "data:%s;base64,%s" % (img["mime"], img["data"])
            else:
                url = img["data"] or ""
            content = [{"type": "text", "text": canon["user"]},
                       {"type": "image_url", "image_url": {"url": url}}]
        else:
            content = canon["user"]
        messages.append({"role": "user", "content": content})
        body = {"model": model, "messages": messages, "stream": False}
        if canon["max_tokens"] is not None:
            body["max_tokens"] = canon["max_tokens"]
        if canon["temperature"] is not None:
            body["temperature"] = canon["temperature"]
        if canon["tools"]:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in canon["tools"]]
            body["tool_choice"] = "auto"
        if canon["json"]:
            body["response_format"] = {"type": "json_object"}
        if canon["stops"]:
            body["stop"] = canon["stops"]
        return body, "/v1/chat/completions"
    if wire == "anthropic":
        if canon["image"] is not None:
            img = canon["image"]
            if img["data"] is not None and img["mime"]:
                source = {"type": "base64", "media_type": img["mime"], "data": img["data"]}
            else:
                source = {"type": "url", "url": img["data"] or ""}
            content = [{"type": "text", "text": canon["user"]},
                       {"type": "image", "source": source}]
        else:
            content = canon["user"]
        body = {"model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": False}
        if canon["system"]:
            body["system"] = canon["system"]
        if canon["max_tokens"] is not None:
            body["max_tokens"] = canon["max_tokens"]
        if canon["temperature"] is not None:
            body["temperature"] = canon["temperature"]
        if canon["tools"]:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["input_schema"]} for t in canon["tools"]]
        if canon["stops"]:
            body["stop_sequences"] = canon["stops"]
        return body, "/v1/messages"
    # gemini
    parts = [{"text": canon["user"]}]
    if canon["image"] is not None:
        img = canon["image"]
        if img["data"] is not None and img["mime"]:
            parts.append({"inlineData": {"mimeType": img["mime"], "data": img["data"]}})
        elif img["data"]:
            parts.append({"fileData": {"mimeType": img["mime"] or "image/png",
                                       "fileUri": img["data"]}})
    body = {"contents": [{"role": "user", "parts": parts}]}
    if canon["system"]:
        body["systemInstruction"] = {"parts": [{"text": canon["system"]}]}
    if canon["tools"]:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["input_schema"]} for t in canon["tools"]]}]
    config = {}
    if canon["max_tokens"] is not None:
        config["maxOutputTokens"] = canon["max_tokens"]
    if canon["stops"]:
        config["stopSequences"] = canon["stops"]
    if canon["json"]:
        config["responseMimeType"] = "application/json"
    if canon["temperature"] is not None:
        config["temperature"] = canon["temperature"]
    if config:
        body["generationConfig"] = config
    from urllib.parse import quote
    path = "/v1beta/models/%s:generateContent" % quote(model, safe="")
    return body, path


def parse_provider_answer(wire, status, raw):
    """A provider's 200 answer -> (text, input_tokens, output_tokens, stop).

    Returns None when the answer is not a well-formed answer of its wire.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not is_record(body):
        return None
    if wire == "openai":
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        choice = choices[0]
        if not is_record(choice) or not is_record(choice.get("message")):
            return None
        text = choice["message"].get("content")
        if not isinstance(text, str):
            text = blocks_text(choice["message"].get("content"))
        usage = body.get("usage") if is_record(body.get("usage")) else {}
        stop = choice.get("finish_reason") or "stop"
        return (text, usage.get("prompt_tokens"), usage.get("completion_tokens"), stop)
    if wire == "anthropic":
        content = body.get("content")
        if not isinstance(content, list):
            return None
        usage = body.get("usage") if is_record(body.get("usage")) else {}
        return (blocks_text(content), usage.get("input_tokens"),
                usage.get("output_tokens"), body.get("stop_reason") or "end_turn")
    # gemini
    cands = body.get("candidates")
    if not isinstance(cands, list) or not cands:
        return None
    cand = cands[0]
    if not is_record(cand):
        return None
    parts = []
    content = cand.get("content") if is_record(cand.get("content")) else {}
    for part in content.get("parts", []) if isinstance(content.get("parts"), list) else []:
        if is_record(part) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    meta = body.get("usageMetadata") if is_record(body.get("usageMetadata")) else {}
    return ("".join(parts), meta.get("promptTokenCount"),
            meta.get("candidatesTokenCount"), cand.get("finishReason") or "STOP")


def call_provider(state, provider, model, canon):
    """One inference call. Returns the parsed answer, or None on any refusal."""
    body, path = build_provider_body(canon, provider["wire"], model)
    url = provider["base_url"] + path
    headers = dict(auth_headers(provider))
    headers["content-type"] = "application/json"
    status, raw = http_post(url, headers, body, PROVIDER_TIMEOUT)
    if status is None:
        log("no answer from %s/%s" % (provider["id"], model))
        return None
    if status < 200 or status >= 300:
        log("%s/%s refused HTTP %s" % (provider["id"], model, status))
        return None
    parsed = parse_provider_answer(provider["wire"], status, raw)
    if parsed is None:
        log("%s/%s answered outside its wire" % (provider["id"], model))
        return None
    text, inp, outp, stop = parsed
    if not isinstance(inp, int) or not isinstance(outp, int):
        log("%s/%s answered without usage" % (provider["id"], model))
        return None
    return {"text": text, "input": inp, "output": outp, "stop": stop}


# --------------------------------------------------------------------------
# Provider answer -> client wire (JSON and SSE).
# --------------------------------------------------------------------------

def split_pieces(text):
    pieces = [text[i:i + SSE_PIECE] for i in range(0, len(text), SSE_PIECE)]
    return pieces if pieces else [""]


def sse_frame(event, data):
    head = "" if event is None else "event: %s\n" % event
    return "%sdata: %s\n\n" % (head, json.dumps(data, ensure_ascii=False))


def map_stop(stop, client_api):
    pair = STOP_MAP.get(stop, ("stop", "end_turn"))
    return pair[0] if client_api == "openai" else pair[1]


def render_openai_json(model, answer):
    usage = {"prompt_tokens": answer["input"], "completion_tokens": answer["output"],
             "total_tokens": answer["input"] + answer["output"]}
    return {
        "id": "chatcmpl-sim", "object": "chat.completion", "created": FIXED_CREATED,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": answer["text"]},
                     "finish_reason": map_stop(answer["stop"], "openai"),
                     "logprobs": None}],
        "usage": usage,
    }


def render_openai_stream(model, answer):
    stop = map_stop(answer["stop"], "openai")
    usage = {"prompt_tokens": answer["input"], "completion_tokens": answer["output"],
             "total_tokens": answer["input"] + answer["output"]}

    def chunk(delta, finish):
        return {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                "created": FIXED_CREATED, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    frames = [sse_frame(None, chunk({"role": "assistant", "content": ""}, None))]
    for piece in split_pieces(answer["text"]):
        frames.append(sse_frame(None, chunk({"content": piece}, None)))
    frames.append(sse_frame(None, chunk({}, stop)))
    frames.append(sse_frame(None, {"id": "chatcmpl-sim",
                                   "object": "chat.completion.chunk",
                                   "created": FIXED_CREATED, "model": model,
                                   "choices": [], "usage": usage}))
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def render_anthropic_json(model, answer):
    return {
        "id": "msg_sim", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": answer["text"]}],
        "stop_reason": map_stop(answer["stop"], "anthropic"),
        "stop_sequence": None,
        "usage": {"input_tokens": answer["input"], "output_tokens": answer["output"]},
    }


def render_anthropic_stream(model, answer):
    stop = map_stop(answer["stop"], "anthropic")
    frames = [
        sse_frame("message_start", {
            "type": "message_start",
            "message": {"id": "msg_sim", "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": answer["input"], "output_tokens": 0}}}),
        sse_frame("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
    ]
    for piece in split_pieces(answer["text"]):
        frames.append(sse_frame("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece}}))
    frames.append(sse_frame("content_block_stop",
                            {"type": "content_block_stop", "index": 0}))
    frames.append(sse_frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop, "stop_sequence": None},
        "usage": {"output_tokens": answer["output"]}}))
    frames.append(sse_frame("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def render_client(api, stream, model, answer):
    """The provider's answer in the wire the client used."""
    if api == "openai":
        if stream:
            return ("text/event-stream; charset=utf-8",
                    render_openai_stream(model, answer).encode("utf-8"))
        return ("application/json",
                (compact_json(render_openai_json(model, answer)) + "\n").encode("utf-8"))
    if stream:
        return ("text/event-stream; charset=utf-8",
                render_anthropic_stream(model, answer).encode("utf-8"))
    return ("application/json",
            (compact_json(render_anthropic_json(model, answer)) + "\n").encode("utf-8"))


def error_body(api, status, message):
    if api == "openai":
        body = {"error": {"message": message, "type": "api_error",
                          "param": None, "code": "upstream_unavailable"}}
    else:
        body = {"type": "error",
                "error": {"type": "api_error", "message": message}}
    return status, ("application/json",
                    (compact_json(body) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# Router: wire capability first, then rating, freshness, and failure memory.
# --------------------------------------------------------------------------

def eligible_candidates(state, client_api, requires):
    """Free models meeting the requirements whose wire can carry the request."""
    out = []
    for cand in state["candidates"]:
        if requires.get("tools") and not cand["tools"]:
            continue
        if requires.get("vision") and not cand["vision"]:
            continue
        if requires.get("json") and not cand["json"]:
            continue
        min_ctx = requires.get("min_context")
        if min_ctx is not None:
            ctx = cand["context_length"]
            if ctx is None or ctx < min_ctx:
                continue
        # JSON mode rides the OpenAI or Gemini wire only; the Anthropic wire
        # has no JSON mode to carry, and its echo would report it absent.
        if requires.get("json") and cand["wire"] == "anthropic":
            continue
        out.append(cand)
    return out


# Published minimum budget per provider (the kit's generator publishes each
# provider's budget range; the minimum is the conservative edge). Past this
# many calls a provider may already be spent, so it is deprioritised -- but
# never banned outright: the real budget for this trial may be higher.
SOFT_BUDGET = {
    "groq": 9, "cerebras": 5, "gemini": 6, "zai": 4, "openrouter": 10,
    "sambanova": 4, "pollinations": 10, "cloudflare": 7, "ovh": 5, "kilol": 12,
}


def rank_candidates(state, client_api, cands):
    """Order candidates: rating, same-wire bonus, fresh-provider bonus,
    small failure penalty, heavy soft-budget penalty. Deterministic:
    provider then model id break ties."""
    def score(cand):
        s = cand["rating"]
        if cand["wire"] == client_api:
            s += 6
        calls = state["calls"].get(cand["provider"], 0)
        if calls == 0:
            s += 20
        elif calls == 1:
            s += 8
        # Concentration risk grows with every call: budget exhaustion, the
        # remaining scheduled failures, and the outage all become likelier
        # the more a provider has already been called.
        s -= 8 * calls
        s -= 10 * state["failures"].get(cand["provider"], 0)
        if calls >= SOFT_BUDGET.get(cand["provider"], 99):
            s -= 80
        return s

    def key(cand):
        return (-score(cand), cand["provider"], cand["model"])

    live, retired = [], []
    for cand in cands:
        if state["failures"].get(cand["provider"], 0) >= 3:
            retired.append(cand)
        else:
            live.append(cand)
    live.sort(key=key)
    retired.sort(key=key)
    return live + retired


def serve_request(state, api, body):
    """Route one client request; return (status, (content_type, payload))."""
    canon = parse_client_body(api, body)
    stream = body.get("stream") is True
    requires = None
    req_id = state.get("current_request")
    if req_id is not None and req_id in state["manifest"]:
        requires = state["manifest"][req_id]["requires"]
    if requires is None:
        requires = infer_requires(api, body, canon)
    cands = rank_candidates(state, api, eligible_candidates(state, api, requires))
    for cand in cands:
        provider = state["providers"][cand["provider"]]
        answer = call_provider(state, provider, cand["model"], canon)
        if answer is not None:
            state["calls"][cand["provider"]] += 1
            state["served"] += 1
            return 200, render_client(api, stream, cand["model"], answer)
        state["calls"][cand["provider"]] += 1
        state["failures"][cand["provider"]] += 1
        log("failover: %s/%s refused; trying next candidate"
            % (cand["provider"], cand["model"]))
    return error_body(api, 503, "no provider could serve this request")


# --------------------------------------------------------------------------
# HTTP server: the two client APIs and the model list.
# --------------------------------------------------------------------------

def free_model_ids(state):
    """Free model ids across providers, first-seen order, de-duplicated."""
    seen, out = set(), []
    for entry in state["catalog"]["providers"]:
        for model in entry["models"]:
            if model["free"] and model["id"] not in seen:
                seen.add(model["id"])
                out.append(model["id"])
    return out


def model_list_body(state, api):
    ids = free_model_ids(state)
    if api == "anthropic":
        return {"data": [{"type": "model", "id": mid, "display_name": mid,
                          "created_at": FIXED_CREATED_AT, "free": True}
                         for mid in ids],
                "has_more": False}
    return {"object": "list",
            "data": [{"id": mid, "object": "model", "created": FIXED_CREATED,
                      "owned_by": "aggregator", "free": True} for mid in ids]}


class Handler(BaseHTTPRequestHandler):
    state = None  # set by main()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout/stderr clean of access logs
        pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length)

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/v1/models":
            api = "anthropic" if self.headers.get("x-api-key") else "openai"
            payload = (compact_json(model_list_body(self.state, api)) + "\n").encode("utf-8")
            self._send(200, "application/json", payload)
            return
        self._send(404, "application/json",
                   (compact_json({"error": "not_found"}) + "\n").encode("utf-8"))

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            api = "openai"
        elif self.path == "/v1/messages":
            api = "anthropic"
        else:
            self._send(404, "application/json",
                       (compact_json({"error": "not_found"}) + "\n").encode("utf-8"))
            return
        raw = self._read_body()
        if raw is None:
            status, out = error_body(api, 400, "unreadable request body")
            self._send(status, out[0], out[1])
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            status, out = error_body(api, 400, "request body is not JSON")
            self._send(status, out[0], out[1])
            return
        if not is_record(body):
            status, out = error_body(api, 400, "request body is not one JSON object")
            self._send(status, out[0], out[1])
            return
        with self.state["lock"]:
            refresh_current_request(self.state)
            status, (content_type, payload) = serve_request(self.state, api, body)
        self._send(status, content_type, payload)


# --------------------------------------------------------------------------
# Env: register, poll /work, exit when done.
# --------------------------------------------------------------------------

def refresh_current_request(state):
    """Ask the env which request the client is sending right now."""
    if not ENV_URL:
        return
    status, raw = http_get(ENV_URL + "/work", {}, WORK_TIMEOUT)
    if status is None or status < 200 or status >= 300:
        return
    try:
        work = json.loads(raw.decode("utf-8"))
    except ValueError:
        return
    if is_record(work) and isinstance(work.get("next"), str):
        state["current_request"] = work["next"]


def register(state, port):
    body = {"protocol_version": PROTOCOL_VERSION, "port": port,
            "catalog": state["catalog"]}
    for attempt in range(REGISTER_RETRIES):
        status, raw = http_post(ENV_URL + "/register",
                                {"content-type": "application/json"}, body, WORK_TIMEOUT)
        if status == 200:
            try:
                answer = json.loads(raw.decode("utf-8"))
            except ValueError:
                answer = {}
            log("registered: %s" % compact_json(answer))
            return True
        log("register attempt %d failed (HTTP %s)" % (attempt + 1, status))
        threading.Event().wait(0.5)
    return False


def poll_work(state, server, done_event):
    """Poll GET /work; when the workload is done, stop the server."""
    failures = 0
    while not done_event.is_set():
        done_event.wait(0.1)
        if done_event.is_set():
            break
        status, raw = http_get(ENV_URL + "/work", {}, WORK_TIMEOUT)
        if status is None:
            failures += 1
            if failures > 300 and state["served"] >= 20:
                log("/work unreachable after serving %d; exiting" % state["served"])
                break
            continue
        failures = 0
        try:
            work = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if is_record(work):
            if isinstance(work.get("next"), str):
                state["current_request"] = work["next"]
            if work.get("done") is True:
                log("workload done after %d served; exiting" % state["served"])
                break
    done_event.set()
    threading.Thread(target=server.shutdown, daemon=True).start()


def main(argv):
    state = load_state()
    if "--list" in argv:
        sys.stdout.write(state["catalog_text"] + "\n")
        return 0
    if not ENV_URL:
        log("MAKEFUN_ENV_URL is not set; nothing to serve")
        return 1
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    log("listening on 0.0.0.0:%d" % port)
    done_event = threading.Event()
    poller = threading.Thread(target=poll_work, args=(state, server, done_event),
                              daemon=True)
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    if not register(state, port):
        log("could not register with the env")
        done_event.set()
        server.shutdown()
        return 1
    poller.start()
    try:
        while not done_event.is_set():
            done_event.wait(0.5)
    except KeyboardInterrupt:
        done_event.set()
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
