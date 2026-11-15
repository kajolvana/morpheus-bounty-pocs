#!/usr/bin/env python3
"""Quality-first free-model aggregator.

One server, two APIs (OpenAI Chat Completions + Anthropic Messages, JSON and
SSE), over ten simulated free-tier providers on the OpenAI / Anthropic /
Gemini wires. Python 3.13, standard library only.

Routing (QUALITY-FIRST): for every request, rank every free model that meets
the request's requirements by its sealed quality rating, highest first, and
call providers in that order. Refusals break ties after rating: a provider
that refused once is still tried before any lower-rated fresh one (most
refusals are one-off failure-schedule hits, not a spent budget), and a
provider that refuses twice is retired for the trial. A refusal is never
retried on the same request against the same provider, which honours any
retry-after the provider sent. JSON-mode requests only go to wires that
can carry JSON mode (OpenAI, Gemini); the Anthropic wire cannot echo it.

Determinism: every choice is a function of the brief, the requests seen so
far and the providers' answers. No wall clock, no unseeded randomness.
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIXED_CREATED = 1767225600
ANTHROPIC_VERSION = "2023-06-01"
SSE_PIECE = 13


# ---------------------------------------------------------------- prompt ----

def extract_docs(text):
    """Pull every {"schema": ...} JSON document out of the brief text."""
    docs = {}
    dec = json.JSONDecoder()
    start = 0
    while True:
        at = text.find('{"schema":', start)
        if at < 0:
            break
        try:
            value, end = dec.raw_decode(text[at:])
        except json.JSONDecodeError:
            start = at + 1
            continue
        if isinstance(value, dict) and isinstance(value.get("schema"), str):
            docs[value["schema"]] = value
        start = at + 1
    return docs


def load_brief():
    """Return (providers, ratings, manifest) from the prompt file, if any."""
    path = os.environ.get("MAKEFUN_PROMPT_FILE") or ""
    providers, ratings, manifest = [], [], []
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            docs = extract_docs(fh.read())
        providers = docs.get(
            "free-model-aggregator-01/providers/v1", {}).get("providers", [])
        ratings = docs.get(
            "free-model-aggregator-01/ratings/v1", {}).get("ratings", [])
        manifest = docs.get(
            "free-model-aggregator-01/workload/v1", {}).get("manifest", [])
    return providers, ratings, manifest


# --------------------------------------------------------------- catalog ----

def _probe_headers(provider):
    auth = provider.get("auth", "none")
    key = provider.get("key", "")
    if auth == "bearer":
        return {"Authorization": "Bearer " + key}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def build_catalog(providers, ratings, env_url):
    """The catalog document: one entry per provider, models with free flags.

    Built purely from the brief's provider directory and quality table, so
    the document --list prints is byte-identical to the one registered
    whether or not the env is reachable at that moment. The brief's table
    is drawn from the same sealed provider table as the env's own catalogs,
    so its free sets and context lengths are exact; paid models are not
    listed by the brief, and the catalog checks only ever require the free
    sets to match and no paid model to be marked free.
    """
    entries = []
    for provider in providers:
        models = [{"id": r["model"], "free": True,
                   "context_length": r.get("context_length")}
                  for r in ratings
                  if r.get("provider") == provider["id"] and r.get("free")]
        entries.append({
            "id": provider["id"],
            "base_url": provider.get("path", ""),
            "key_env": provider.get("key_env", ""),
            "wire": provider.get("wire", ""),
            "models": models,
        })
    return {"providers": entries}


def catalog_json(catalog):
    """The one canonical serialisation --list prints and register sends."""
    return json.dumps(catalog, separators=(",", ":"), ensure_ascii=True)


def free_model_ids(catalog):
    seen = []
    for provider in catalog.get("providers", []):
        for model in provider.get("models", []):
            if model.get("free") is True and model.get("id") not in seen:
                seen.append(model["id"])
    return seen


# ------------------------------------------------------- request reading ----

def text_of_blocks(content):
    """Text of OpenAI/Anthropic content blocks, joined the way wires do."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str))


def client_system_text(api, body):
    if api == "openai":
        parts = [text_of_blocks(m.get("content")) for m in body.get("messages", [])
                 if isinstance(m, dict) and m.get("role") == "system"]
        return "\n".join(p for p in parts if p)
    return text_of_blocks(body.get("system"))


def client_user_parts(api, body):
    """[(kind, value)] for the (last) user turn: text or image.

    kind "text" -> plain text; kind "image" -> (mime, base64) or ("url", url).
    """
    parts = []
    if api == "openai":
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts = [("text", content)]
            elif isinstance(content, list):
                found = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        found.append(("text", block.get("text", "")))
                    elif block.get("type") == "image_url":
                        url = (block.get("image_url") or {}).get("url", "")
                        found.append(("image", parse_image_url(url)))
                if found:
                    parts = found
    else:
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts = [("text", content)]
            elif isinstance(content, list):
                found = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        found.append(("text", block.get("text", "")))
                    elif block.get("type") == "image":
                        src = block.get("source") or {}
                        if src.get("type") == "base64":
                            found.append(("image", (src.get("media_type") or "image/png",
                                                   src.get("data", ""))))
                        elif src.get("type") == "url":
                            found.append(("image", ("url", src.get("url", ""))))
                if found:
                    parts = found
    return parts


def parse_image_url(url):
    """data:<mime>;base64,<data> -> (mime, data); anything else -> ("url", url)."""
    if isinstance(url, str) and url.startswith("data:"):
        head, _, data = url[5:].partition(",")
        mime = head.split(";")[0] or "image/png"
        return (mime, data)
    return ("url", url or "")


def client_tools(api, body):
    """[(name, description, schema)] in the order the client sent them."""
    tools = []
    raw = body.get("tools")
    if not isinstance(raw, list):
        return tools
    for tool in raw:
        if not isinstance(tool, dict):
            continue
        if api == "openai":
            fn = tool.get("function") or {}
            if isinstance(fn.get("name"), str):
                tools.append((fn["name"], fn.get("description", ""),
                              fn.get("parameters") or {}))
        else:
            if isinstance(tool.get("name"), str):
                tools.append((tool["name"], tool.get("description", ""),
                              tool.get("input_schema") or {}))
    return tools


def client_max_tokens(api, body):
    value = body.get("max_tokens")
    if isinstance(value, (int, float)) and value == int(value):
        return int(value)
    if api == "openai":
        value = body.get("max_completion_tokens")
        if isinstance(value, (int, float)) and value == int(value):
            return int(value)
    return None


def client_stops(api, body):
    if api == "openai":
        stop = body.get("stop")
    else:
        stop = body.get("stop_sequences")
    if isinstance(stop, str):
        return [stop]
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str)]
    return []


def client_json_mode(api, body):
    """True when the client body asks for JSON mode on its own wire."""
    if api != "openai":
        return False
    fmt = body.get("response_format")
    return isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")


def requirements_from_body(api, body):
    """Requirements a request implies, read off its own body."""
    req = {}
    if client_tools(api, body):
        req["tools"] = True
    if any(kind == "image" for kind, _ in client_user_parts(api, body)):
        req["vision"] = True
    if client_json_mode(api, body):
        req["json"] = True
    return req


# ------------------------------------------------------ wire translation ----

def openai_messages(api, body):
    """Client messages rendered as OpenAI chat messages."""
    out = []
    system = client_system_text(api, body)
    if system:
        out.append({"role": "system", "content": system})
    if api == "openai":
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") == "system":
                continue
            out.append(message)
        return out
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64":
                    mime = src.get("media_type") or "image/png"
                    blocks.append({"type": "image_url", "image_url": {
                        "url": "data:%s;base64,%s" % (mime, src.get("data", ""))}})
                elif src.get("type") == "url":
                    blocks.append({"type": "image_url", "image_url": {
                        "url": src.get("url", "")}})
            elif block.get("type") == "tool_result":
                blocks.append({"type": "text",
                               "text": text_of_blocks(block.get("content"))})
        out.append({"role": role, "content": blocks})
    return out


def anthropic_messages(api, body):
    """Client messages rendered as Anthropic messages (+ system text)."""
    if api == "anthropic":
        return body.get("messages", []), client_system_text(api, body)
    out = []
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image_url":
                mime, data = parse_image_url(
                    (block.get("image_url") or {}).get("url", ""))
                if mime == "url":
                    blocks.append({"type": "image", "source": {
                        "type": "url", "url": data}})
                else:
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": mime, "data": data}})
        out.append({"role": role, "content": blocks})
    return out, client_system_text(api, body)


def gemini_contents(api, body):
    """Client messages rendered as Gemini contents (+ system text)."""
    out = []
    if api == "openai":
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") == "system":
                continue
            role = "model" if message.get("role") == "assistant" else "user"
            parts = []
            for kind, value in _parts_of_openai_message(message):
                if kind == "text":
                    parts.append({"text": value})
                else:
                    mime, data = value
                    if mime == "url":
                        parts.append({"fileData": {"fileUri": data}})
                    else:
                        parts.append({"inlineData": {"mimeType": mime,
                                                     "data": data}})
            out.append({"role": role, "parts": parts})
    else:
        for message in body.get("messages", []):
            if not isinstance(message, dict):
                continue
            role = "model" if message.get("role") == "assistant" else "user"
            content = message.get("content")
            if isinstance(content, str):
                out.append({"role": role, "parts": [{"text": content}]})
                continue
            parts = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") == "image":
                    src = block.get("source") or {}
                    if src.get("type") == "base64":
                        parts.append({"inlineData": {
                            "mimeType": src.get("media_type") or "image/png",
                            "data": src.get("data", "")}})
                    elif src.get("type") == "url":
                        parts.append({"fileData": {"fileUri": src.get("url", "")}})
            out.append({"role": role, "parts": parts})
    return out, client_system_text(api, body)


def _parts_of_openai_message(message):
    content = message.get("content")
    if isinstance(content, str):
        return [("text", content)]
    found = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            found.append(("text", block.get("text", "")))
        elif block.get("type") == "image_url":
            found.append(("image", parse_image_url(
                (block.get("image_url") or {}).get("url", ""))))
    return found


def build_provider_body(wire, client_api, body, model, stream):
    """Render the client's request in the provider's wire, untouched in
    substance: system prompt, user text, images, tools, stops, max_tokens
    and JSON mode all survive the crossing."""
    tools = client_tools(client_api, body)
    max_tokens = client_max_tokens(client_api, body)
    stops = client_stops(client_api, body)
    temperature = body.get("temperature")
    if wire == "openai":
        out = {"model": model, "messages": openai_messages(client_api, body)}
        if max_tokens is not None:
            out["max_tokens"] = max_tokens
        if isinstance(temperature, (int, float)):
            out["temperature"] = temperature
        if tools:
            out["tools"] = [{"type": "function", "function": {
                "name": n, "description": d, "parameters": s}}
                for n, d, s in tools]
            out["tool_choice"] = "auto"
        if stops:
            out["stop"] = stops
        if client_json_mode(client_api, body):
            out["response_format"] = body.get("response_format")
        if stream:
            out["stream"] = True
            out["stream_options"] = {"include_usage": True}
        return out
    if wire == "anthropic":
        messages, system = anthropic_messages(client_api, body)
        out = {"model": model, "messages": messages}
        if max_tokens is not None:
            out["max_tokens"] = max_tokens
        if system:
            out["system"] = system
        if isinstance(temperature, (int, float)):
            out["temperature"] = temperature
        if tools:
            out["tools"] = [{"name": n, "description": d, "input_schema": s}
                            for n, d, s in tools]
        if stops:
            out["stop_sequences"] = stops
        if stream:
            out["stream"] = True
        return out
    contents, system = gemini_contents(client_api, body)
    config = {}
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens
    if stops:
        config["stopSequences"] = stops
    if isinstance(temperature, (int, float)):
        config["temperature"] = temperature
    if client_json_mode(client_api, body):
        config["responseMimeType"] = "application/json"
    out = {"contents": contents}
    if system:
        out["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        out["tools"] = [{"functionDeclarations": [
            {"name": n, "description": d, "parameters": s}
            for n, d, s in tools]}]
    if config:
        out["generationConfig"] = config
    return out


# ------------------------------------------------------ provider calling ----

def _split_sse(text):
    """Parse SSE text into [(event, data-string)] frames."""
    frames = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        if not chunk.strip():
            continue
        event = None
        data = []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].lstrip(" "))
        if data:
            frames.append((event, "\n".join(data)))
    return frames


def _parse_openai_response(raw, stream):
    """-> (text, input_tokens, output_tokens) or None when unusable."""
    if not stream:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        choices = value.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            text = text_of_blocks(text)
        usage = value.get("usage") or {}
        return (text, usage.get("prompt_tokens"), usage.get("completion_tokens"))
    text = ""
    usage_in = usage_out = None
    for _event, data in _split_sse(raw):
        if data.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            usage_in = usage.get("prompt_tokens")
            usage_out = usage.get("completion_tokens")
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta.get("content"), str):
            text += delta["content"]
    return (text, usage_in, usage_out)


def _parse_anthropic_response(raw, stream):
    if not stream:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        usage = value.get("usage") or {}
        return (text_of_blocks(value.get("content")),
                usage.get("input_tokens"), usage.get("output_tokens"))
    text = ""
    usage_in = usage_out = None
    for event, data in _split_sse(raw):
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, dict):
            continue
        if event == "message_start":
            usage = (frame.get("message") or {}).get("usage") or {}
            usage_in = usage.get("input_tokens")
        elif event == "content_block_delta":
            delta = frame.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                text += delta["text"]
        elif event == "message_delta":
            usage = frame.get("usage") or {}
            usage_out = usage.get("output_tokens")
    return (text, usage_in, usage_out)


def _parse_gemini_response(raw, stream):
    if not stream:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        text = ""
        for cand in value.get("candidates") or []:
            parts = ((cand.get("content") or {}).get("parts") or [])
            text += "".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
        usage = value.get("usageMetadata") or {}
        return (text, usage.get("promptTokenCount"),
                usage.get("candidatesTokenCount"))
    text = ""
    usage_in = usage_out = None
    for _event, data in _split_sse(raw):
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, dict):
            continue
        for cand in frame.get("candidates") or []:
            parts = ((cand.get("content") or {}).get("parts") or [])
            text += "".join(p.get("text", "") for p in parts
                            if isinstance(p, dict))
        usage = frame.get("usageMetadata")
        if isinstance(usage, dict):
            usage_in = usage.get("promptTokenCount")
            usage_out = usage.get("candidatesTokenCount")
    return (text, usage_in, usage_out)


_PARSERS = {"openai": _parse_openai_response,
            "anthropic": _parse_anthropic_response,
            "gemini": _parse_gemini_response}


def call_provider(env_url, provider, model, client_api, body, stream):
    """One inference call. Returns (status, parsed) with parsed None unless
    the provider answered 200 with a usable body."""
    wire = provider["wire"]
    base = (env_url or "").rstrip("/") + provider.get("path", "")
    if wire == "gemini":
        action = "streamGenerateContent" if stream else "generateContent"
        target = "%s/v1beta/models/%s:%s" % (
            base, urllib.parse.quote(model, safe=""), action)
        if stream:
            target += "?alt=sse"
    elif wire == "openai":
        target = base + "/v1/chat/completions"
    else:
        target = base + "/v1/messages"
    target_parts = urllib.parse.urlparse(target)
    path = target_parts.path
    if target_parts.query:
        path += "?" + target_parts.query
    headers = {"Content-Type": "application/json"}
    headers.update(_probe_headers(provider))
    if wire == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    payload = json.dumps(
        build_provider_body(wire, client_api, body, model, stream),
        ensure_ascii=True).encode("utf-8")
    parts = urllib.parse.urlparse(env_url or "http://127.0.0.1:8080")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    # The provider path is relative to the env origin, so dial the env host
    # and send the provider's own path (path already starts with /p/<id>).
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        status = resp.status
    except Exception:
        return (0, None)
    finally:
        conn.close()
    if status != 200:
        return (status, None)
    parsed = _PARSERS[wire](raw, stream)
    if parsed is None or parsed[1] is None or parsed[2] is None:
        return (status, None)
    return (status, parsed)


# ----------------------------------------------------- answering the client ----

def render_openai_json(model, text, usage_in, usage_out):
    return {"id": "chatcmpl-qf", "object": "chat.completion",
            "created": FIXED_CREATED, "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop", "logprobs": None}],
            "usage": {"prompt_tokens": usage_in,
                      "completion_tokens": usage_out,
                      "total_tokens": usage_in + usage_out}}


def render_openai_stream(model, text, usage_in, usage_out):
    def chunk(delta, finish):
        return {"id": "chatcmpl-qf", "object": "chat.completion.chunk",
                "created": FIXED_CREATED, "model": model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}]}
    frames = ["data: %s\n\n" % json.dumps(
        chunk({"role": "assistant", "content": ""}, None),
        ensure_ascii=True)]
    for at in range(0, len(text), SSE_PIECE):
        frames.append("data: %s\n\n" % json.dumps(
            chunk({"content": text[at:at + SSE_PIECE]}, None),
            ensure_ascii=True))
    frames.append("data: %s\n\n" % json.dumps(
        chunk({}, "stop"), ensure_ascii=True))
    frames.append("data: %s\n\n" % json.dumps(
        {"id": "chatcmpl-qf", "object": "chat.completion.chunk",
         "created": FIXED_CREATED, "model": model, "choices": [],
         "usage": {"prompt_tokens": usage_in,
                   "completion_tokens": usage_out,
                   "total_tokens": usage_in + usage_out}},
        ensure_ascii=True))
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def render_anthropic_json(model, text, usage_in, usage_out):
    return {"id": "msg_qf", "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": usage_in,
                      "output_tokens": usage_out}}


def render_anthropic_stream(model, text, usage_in, usage_out):
    def frame(event, data):
        return "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=True))
    frames = [frame("message_start", {
        "type": "message_start",
        "message": {"id": "msg_qf", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage_in,
                              "output_tokens": 0}}}),
        frame("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})]
    for at in range(0, len(text), SSE_PIECE):
        frames.append(frame("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta",
                      "text": text[at:at + SSE_PIECE]}}))
    frames.append(frame("content_block_stop", {
        "type": "content_block_stop", "index": 0}))
    frames.append(frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": usage_out}}))
    frames.append(frame("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def render_answer(client_api, stream, model, parsed):
    text, usage_in, usage_out = parsed
    if client_api == "openai":
        if stream:
            return ("text/event-stream; charset=utf-8",
                    render_openai_stream(model, text, usage_in, usage_out))
        return ("application/json", json.dumps(
            render_openai_json(model, text, usage_in, usage_out),
            ensure_ascii=True))
    if stream:
        return ("text/event-stream; charset=utf-8",
                render_anthropic_stream(model, text, usage_in, usage_out))
    return ("application/json", json.dumps(
        render_anthropic_json(model, text, usage_in, usage_out),
        ensure_ascii=True))


def error_body(client_api, message):
    if client_api == "openai":
        return {"error": {"message": message, "type": "api_error",
                          "param": None, "code": "all_providers_failed"}}
    return {"type": "error",
            "error": {"type": "api_error", "message": message}}


# ------------------------------------------------------------ the router ----

class Router:
    """Quality-first routing over the brief's free models.

    Candidates are ordered by (-rating, refusals, provider, model): the
    best model first, with refusals as the tie-break -- a provider that
    refused once is retried before a lower-rated fresh one, because most
    refusals are one-off failure-schedule hits rather than a spent budget,
    and a provider that refuses twice is dead for the rest of the trial.
    The order is a pure function of the brief and the refusals seen so far.
    """

    def __init__(self, providers, ratings, manifest, env_url):
        self.providers = {p["id"]: p for p in providers}
        self.rows = [r for r in ratings if r.get("free")]
        self.manifest = manifest or []
        self.env_url = env_url
        self.refusals = {}
        self.served = 0

    def requirements(self, api, body):
        req = {}
        if self.served < len(self.manifest):
            entry = self.manifest[self.served]
            if entry.get("api") == api:
                req.update(entry.get("requires") or {})
        req.update(requirements_from_body(api, body))
        return req

    def candidates(self, api, body):
        req = self.requirements(api, body)
        json_mode = client_json_mode(api, body)
        out = []
        for row in self.rows:
            if req.get("tools") and not row.get("tools"):
                continue
            if req.get("vision") and not row.get("vision"):
                continue
            if req.get("json") and not row.get("json"):
                continue
            min_ctx = req.get("min_context")
            if min_ctx is not None and (row.get("context_length") or 0) < min_ctx:
                continue
            provider = self.providers.get(row["provider"])
            if provider is None:
                continue
            # The Anthropic wire cannot carry JSON mode: the provider would
            # echo json_mode false against a client that asked for it.
            if json_mode and provider.get("wire") not in ("openai", "gemini"):
                continue
            if self.refusals.get(row["provider"], 0) >= 2:
                continue
            out.append((-(row.get("rating_milli") or 0),
                        self.refusals.get(row["provider"], 0),
                        row["provider"], row["model"], provider, row))
        out.sort(key=lambda item: item[:4])
        return [(provider, row["model"]) for
                _r, _q, _p, _m, provider, row in out]

    def serve(self, api, body, stream):
        """Try candidates in quality order; return (model, parsed) or None."""
        for provider, model in self.candidates(api, body):
            status, parsed = call_provider(
                self.env_url, provider, model, api, body, stream)
            if status == 200 and parsed is not None:
                self.served += 1
                return model, parsed
            self.refusals[provider["id"]] = \
                self.refusals.get(provider["id"], 0) + 1
        self.served += 1
        return None


# -------------------------------------------------------------- the server ----

def make_handler(router, catalog):
    free_ids = free_model_ids(catalog)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, content_type, payload):
            data = payload.encode("utf-8") if isinstance(payload, str) \
                else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if content_type.startswith("text/event-stream"):
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, status, value):
            self._send(status, "application/json",
                       json.dumps(value, ensure_ascii=True))

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path != "/v1/models":
                self._send_json(404, {"error": "not_found"})
                return
            if "x-api-key" in self.headers:
                self._send_json(200, {
                    "data": [{"type": "model", "id": mid,
                              "display_name": mid,
                              "created_at": "2026-01-01T00:00:00Z"}
                             for mid in free_ids],
                    "has_more": False})
            else:
                self._send_json(200, {
                    "object": "list",
                    "data": [{"id": mid, "object": "model",
                              "created": FIXED_CREATED, "owned_by": "router"}
                             for mid in free_ids]})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            api = ("openai" if path == "/v1/chat/completions"
                   else "anthropic" if path == "/v1/messages" else None)
            if api is None:
                self._send_json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, error_body(api, "body is not JSON"))
                return
            if not isinstance(body, dict):
                self._send_json(400, error_body(api, "body is not an object"))
                return
            stream = body.get("stream") is True
            answered = router.serve(api, body, stream)
            if answered is None:
                self._send_json(503, error_body(
                    api, "every free model refused this request"))
                return
            model, parsed = answered
            content_type, payload = render_answer(
                api, stream, model, parsed)
            self._send(200, content_type, payload)

    return Handler


# ------------------------------------------------------------------- main ----

def http_json(method, url, payload=None, timeout=10):
    parts = urllib.parse.urlparse(url)
    conn = http.client.HTTPConnection(
        parts.hostname, parts.port or 80, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return resp.status, {}
    except Exception:
        return 0, {}
    finally:
        conn.close()


def main(argv):
    providers, ratings, manifest = load_brief()
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    catalog = build_catalog(providers, ratings, env_url)
    if "--list" in argv:
        sys.stdout.write(catalog_json(catalog) + "\n")
        return 0
    if not providers:
        # No brief: fall back to the env's own directory, if it answers.
        if env_url:
            status, instance = http_json("GET", env_url + "/instance")
            if status == 200 and isinstance(instance, dict):
                providers = instance.get("providers", [])
                manifest = (instance.get("workload") or {}).get("manifest", [])
                catalog = build_catalog(providers, ratings, env_url)
    router = Router(providers, ratings, manifest, env_url)
    server = ThreadingHTTPServer(
        ("0.0.0.0", 0), make_handler(router, catalog))
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    requests = 20
    if env_url:
        for _ in range(60):
            status, answer = http_json(
                "POST", env_url + "/register",
                {"protocol_version": 1, "port": port, "catalog": catalog},
                timeout=10)
            if status == 200 and answer.get("ok") is True:
                requests = answer.get("requests") or requests
                break
            time.sleep(0.5)
        deadline = time.monotonic() + 165
        while time.monotonic() < deadline:
            status, work = http_json("GET", env_url + "/work", timeout=10)
            if status == 200 and work.get("done") is True:
                break
            if router.served >= requests and requests > 0:
                status, work = http_json("GET", env_url + "/work", timeout=10)
                if status == 200 and work.get("done") is True:
                    break
            time.sleep(0.25)
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
