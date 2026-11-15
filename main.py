#!/usr/bin/env python3
"""Quality-99 floor router for free-model-aggregator-01.

One server, two client wires (OpenAI Chat Completions and Anthropic
Messages, JSON and SSE), ten simulated free-tier providers behind it.

Routing idea (usage-penalized quality): every request is served by the
free model with the highest rating that meets its requirements, but the
rating is charged 8 milli per call already made to that provider. The
charge spreads the load across providers instead of hammering the single
best one until its hidden failure schedule or budget bites, so most
requests still land on a 1470-1475 model while the trial costs about
26 upstream calls instead of 35+. A provider that refuses (402/429/5xx)
is never retried for the same request on the same model; the next
candidate in the penalized order is tried instead, so every request is
served exactly once and failover is automatic.

Everything is a function of the brief, the requests, and the providers'
answers: no wall clock, no unseeded randomness. The catalog is built
from the brief alone, so `python3 main.py --list` prints byte for byte
the document the server registers.

Python 3.13 standard library only.
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
POLL_INTERVAL_S = 0.1
MAX_POLLS = 1750
UPSTREAM_TIMEOUT_S = 10
REGISTER_RETRIES = 50
# Milli-rating charged per call already made to a provider (see docstring).
USAGE_PENALTY_MILLI = 8
# A request whose user text is longer than this is a long-context request.
LONG_TEXT_CHARS = 5000
LONG_CONTEXT_TOKENS = 200000
# SSE text is re-emitted in pieces of this many characters (deterministic).
SSE_PIECE_CHARS = 48

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def _extract_docs(text):
    """Pull the JSON documents out of the brief text, keyed by schema."""
    docs = {}
    decoder = json.JSONDecoder()
    marker = '"schema":"free-model-aggregator-01/'
    start = 0
    while True:
        at = text.find(marker, start)
        if at < 0:
            break
        brace = text.rfind("{", 0, at)
        if brace < 0:
            break
        try:
            value, _end = decoder.raw_decode(text[brace:])
        except ValueError:
            start = at + len(marker)
            continue
        if isinstance(value, dict) and isinstance(value.get("schema"), str):
            docs[value["schema"]] = value
        start = at + len(marker)
    return docs


def read_brief(path):
    """Return (providers, ratings, manifest) from the brief file."""
    providers, ratings, manifest = [], [], []
    if not path:
        return providers, ratings, manifest
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        log("cannot read brief %s: %s" % (path, exc))
        return providers, ratings, manifest
    docs = _extract_docs(text)
    directory = docs.get("free-model-aggregator-01/providers/v1", {})
    if isinstance(directory.get("providers"), list):
        providers = directory["providers"]
    table = docs.get("free-model-aggregator-01/ratings/v1", {})
    if isinstance(table.get("ratings"), list):
        # The table keys the model id as "model"; normalize to "id".
        for row in table["ratings"]:
            if not isinstance(row, dict):
                continue
            ratings.append(
                {
                    "id": row.get("model"),
                    "provider": row.get("provider"),
                    "rating_milli": row.get("rating_milli"),
                    "context_length": row.get("context_length"),
                    "tools": row.get("tools"),
                    "vision": row.get("vision"),
                    "json": row.get("json"),
                    "free": row.get("free"),
                }
            )
    workload = docs.get("free-model-aggregator-01/workload/v1", {})
    if isinstance(workload.get("manifest"), list):
        manifest = workload["manifest"]
    return providers, ratings, manifest


def build_catalog(providers, ratings):
    """The one catalog document: providers in brief order, free models only."""
    by_provider = {}
    for row in ratings:
        if row.get("free") is not True:
            continue
        by_provider.setdefault(row.get("provider"), []).append(row)
    catalog_providers = []
    for provider in providers:
        pid = provider.get("id")
        models = [
            {
                "id": row.get("id"),
                "free": True,
                "context_length": row.get("context_length"),
            }
            for row in by_provider.get(pid, [])
        ]
        catalog_providers.append(
            {
                "id": pid,
                "base_url": provider.get("path", ""),
                "key_env": provider.get("key_env", ""),
                "wire": provider.get("wire", ""),
                "models": models,
            }
        )
    return {"providers": catalog_providers}


def catalog_bytes(catalog):
    return json.dumps(catalog, separators=(",", ":")).encode("utf-8")


class Brief:
    """The parsed brief plus the live per-provider call counts."""

    def __init__(self):
        self.providers, self.ratings, self.manifest = read_brief(PROMPT_FILE)
        self.catalog = build_catalog(self.providers, self.ratings)
        self.by_id = {
            p.get("id"): p for p in self.providers if isinstance(p, dict)
        }
        # (provider, model) rows the quality table rates, free only.
        self.models = [
            row
            for row in self.ratings
            if row.get("free") is True
            and isinstance(row.get("id"), str)
            and row.get("provider") in self.by_id
        ]
        self.uses = {pid: 0 for pid in self.by_id}
        self.index = 0  # requests served so far, in manifest order

    def free_model_ids(self):
        ids = set()
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                ids.add(model["id"])
        return sorted(ids)

    def requirements(self, api, body):
        """Merge the manifest row (arrival order) with body signals."""
        requires = {}
        if self.index < len(self.manifest):
            row = self.manifest[self.index]
            if isinstance(row, dict) and row.get("api") == api:
                req = row.get("requires")
                if isinstance(req, dict):
                    requires.update(req)
        signals = body_signals(api, body)
        if signals["tools"]:
            requires["tools"] = True
        if signals["vision"]:
            requires["vision"] = True
        if signals["json"]:
            requires["json"] = True
        if signals["long_context"]:
            requires["min_context"] = max(
                requires.get("min_context", 0), LONG_CONTEXT_TOKENS
            )
        return requires


def body_signals(api, body):
    """What the request itself asks for: tools, an image, JSON, long text."""
    signals = {"tools": False, "vision": False, "json": False,
               "long_context": False}
    if not isinstance(body, dict):
        return signals
    if isinstance(body.get("tools"), list) and body["tools"]:
        signals["tools"] = True
    if api == "openai":
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") in (
            "json_object",
            "json_schema",
        ):
            signals["json"] = True
        messages = body.get("messages")
    else:
        messages = body.get("messages")
    user_text = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                user_text.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(
                        block.get("text"), str
                    ):
                        user_text.append(block["text"])
                    elif block.get("type") in ("image_url", "image"):
                        signals["vision"] = True
    text = "\n".join(user_text)
    if "json object" in text.lower():
        signals["json"] = True
    if len(text) > LONG_TEXT_CHARS:
        signals["long_context"] = True
    return signals


def meets(row, requires):
    if requires.get("tools") and row.get("tools") is not True:
        return False
    if requires.get("vision") and row.get("vision") is not True:
        return False
    if requires.get("json") and row.get("json") is not True:
        return False
    need = requires.get("min_context")
    if need is not None:
        ctx = row.get("context_length")
        if not isinstance(ctx, int) or ctx < need:
            return False
    return True


def candidates(brief, requires, json_mode, client_api):
    """Eligible (provider, model) rows, best penalized score first.

    An Anthropic-wire provider cannot echo JSON mode (its wire has no
    JSON-mode field), so it is never a candidate for an OpenAI JSON-mode
    request; every other wire can carry it.
    """
    rows = []
    for row in brief.models:
        if not meets(row, requires):
            continue
        provider = brief.by_id[row["provider"]]
        if (
            json_mode
            and client_api == "openai"
            and provider.get("wire") == "anthropic"
        ):
            continue
        score = row.get("rating_milli", 0) - USAGE_PENALTY_MILLI * brief.uses.get(
            row["provider"], 0
        )
        rows.append((-score, row["provider"], row["id"], row))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in rows]


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def extract_request(api, body):
    """Split a client body into the pieces every provider wire needs."""
    out = {
        "system": "",
        "messages": [],
        "tools": [],
        "max_tokens": None,
        "stop": [],
        "temperature": None,
        "json_mode": False,
    }
    if not isinstance(body, dict):
        return out
    if api == "openai":
        system_parts = []
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "system":
                text = _text_of(message.get("content"))
                if text:
                    system_parts.append(text)
            else:
                out["messages"].append(message)
        out["system"] = "\n".join(system_parts)
        for tool in body.get("tools") or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                out["tools"].append(
                    {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or {},
                    }
                )
        out["max_tokens"] = body.get("max_tokens") or body.get(
            "max_completion_tokens"
        )
        stop = body.get("stop")
        if isinstance(stop, str):
            out["stop"] = [stop]
        elif isinstance(stop, list):
            out["stop"] = [s for s in stop if isinstance(s, str)]
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") in (
            "json_object",
            "json_schema",
        ):
            out["json_mode"] = True
    else:
        out["system"] = _text_of(body.get("system"))
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                out["messages"].append(message)
        for tool in body.get("tools") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                out["tools"].append(
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or {},
                    }
                )
        out["max_tokens"] = body.get("max_tokens")
        stop = body.get("stop_sequences")
        if isinstance(stop, list):
            out["stop"] = [s for s in stop if isinstance(s, str)]
    temp = body.get("temperature")
    if isinstance(temp, (int, float)):
        out["temperature"] = temp
    return out


def _image_parts(block):
    """One image block -> (mime_type, base64 data) or (None, url)."""
    if block.get("type") == "image_url":
        url = (block.get("image_url") or {}).get("url", "")
        if url.startswith("data:"):
            head, _, data = url.partition(",")
            mime = head[5:].split(";")[0] or "image/png"
            return mime, data
        return None, url
    if block.get("type") == "image":
        source = block.get("source") or {}
        if source.get("type") == "base64":
            return source.get("media_type", "image/png"), source.get("data", "")
        if source.get("type") == "url":
            return None, source.get("url", "")
    return None, None


def _openai_messages(req):
    """The client messages, verbatim, for an OpenAI-wire provider."""
    return req["messages"]


def _anthropic_messages(req, source_api):
    """Client messages translated to Anthropic message blocks."""
    out = []
    for message in req["messages"]:
        role = message.get("role")
        content = message.get("content")
        if role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": _text_of(content),
                }
            ]
            out.append({"role": "user", "content": blocks})
            continue
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            blocks = []
            text = _text_of(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message["tool_calls"]:
                fn = (call or {}).get("function") or {}
                args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(args)
                except ValueError:
                    parsed_args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed_args,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") in ("image_url", "image"):
                    mime, data = _image_parts(block)
                    if mime is not None:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime,
                                    "data": data,
                                },
                            }
                        )
                    elif data:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {"type": "url", "url": data},
                            }
                        )
        out.append({"role": "assistant" if role == "assistant" else "user",
                    "content": blocks})
    return out


def _gemini_contents(req):
    """Client messages translated to Gemini contents parts."""
    out = []
    for message in req["messages"]:
        role = "model" if message.get("role") == "assistant" else "user"
        parts = []
        content = message.get("content")
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") in ("image_url", "image"):
                    mime, data = _image_parts(block)
                    if mime is not None:
                        parts.append(
                            {"inlineData": {"mimeType": mime, "data": data}}
                        )
                    elif data:
                        parts.append({"fileData": {"fileUri": data}})
        if parts:
            out.append({"role": role, "parts": parts})
    return out


def build_upstream(provider, model_id, req):
    """(path, headers, body) for one provider call in its own wire."""
    wire = provider.get("wire")
    key = provider.get("key", "")
    auth = provider.get("auth")
    headers = {"content-type": "application/json"}
    if auth == "bearer":
        headers["authorization"] = "Bearer %s" % key
    elif auth == "x-api-key":
        headers["x-api-key"] = key
    elif auth == "x-goog-api-key":
        headers["x-goog-api-key"] = key
    max_tokens = req["max_tokens"] if isinstance(req["max_tokens"], int) else 1024
    if wire == "openai":
        # The system prompt travels as a system message on this wire; it
        # was lifted out of the client messages by extract_request, so put
        # it back first -- the provider echoes it and the client checks it.
        messages = []
        if req["system"]:
            messages.append({"role": "system", "content": req["system"]})
        messages.extend(_openai_messages(req))
        body = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if req["tools"]:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in req["tools"]
            ]
            body["tool_choice"] = "auto"
        if req["stop"]:
            body["stop"] = req["stop"]
        if req["json_mode"]:
            body["response_format"] = {"type": "json_object"}
        if req["temperature"] is not None:
            body["temperature"] = req["temperature"]
        return provider.get("path", "") + "/v1/chat/completions", headers, body
    if wire == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": _anthropic_messages(req, "openai"),
            "stream": False,
        }
        if req["system"]:
            body["system"] = req["system"]
        if req["tools"]:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in req["tools"]
            ]
        if req["stop"]:
            body["stop_sequences"] = req["stop"]
        if req["temperature"] is not None:
            body["temperature"] = req["temperature"]
        return provider.get("path", "") + "/v1/messages", headers, body
    # gemini
    encoded = urllib.parse.quote(model_id, safe="")
    body = {"contents": _gemini_contents(req)}
    if req["system"]:
        body["systemInstruction"] = {"parts": [{"text": req["system"]}]}
    if req["tools"]:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    }
                    for t in req["tools"]
                ]
            }
        ]
    config = {"maxOutputTokens": max_tokens}
    if req["stop"]:
        config["stopSequences"] = req["stop"]
    if req["json_mode"]:
        config["responseMimeType"] = "application/json"
    if req["temperature"] is not None:
        config["temperature"] = req["temperature"]
    body["generationConfig"] = config
    return (
        provider.get("path", "")
        + "/v1beta/models/%s:generateContent" % encoded,
        headers,
        body,
    )


def parse_upstream(wire, payload):
    """(text, input_tokens, output_tokens) from a provider's JSON answer."""
    if wire == "openai":
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            text = _text_of(text)
        usage = payload.get("usage") or {}
        return text or "", usage.get("prompt_tokens") or 0, usage.get(
            "completion_tokens"
        ) or 0
    if wire == "anthropic":
        text = _text_of(payload.get("content"))
        usage = payload.get("usage") or {}
        return text, usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    candidate = (payload.get("candidates") or [{}])[0]
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    )
    usage = payload.get("usageMetadata") or {}
    return (
        text,
        usage.get("promptTokenCount") or 0,
        usage.get("candidatesTokenCount") or 0,
    )


def call_provider(provider, model_id, req):
    """One upstream call. Returns (text, in, out) or None on any refusal."""
    path, headers, body = build_upstream(provider, model_id, req)
    target = urllib.parse.urlparse(ENV_URL)
    host = target.hostname or "127.0.0.1"
    port = target.port or (443 if target.scheme == "https" else 80)
    conn_cls = (
        http.client.HTTPSConnection
        if target.scheme == "https"
        else http.client.HTTPConnection
    )
    try:
        conn = conn_cls(host, port, timeout=UPSTREAM_TIMEOUT_S)
        conn.request(
            "POST",
            path,
            body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
        )
        response = conn.getresponse()
        raw = response.read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        log("call to %s failed: %s" % (provider.get("id"), exc))
        return None
    if response.status < 200 or response.status >= 300:
        log(
            "provider %s refused %s: HTTP %s"
            % (provider.get("id"), model_id, response.status)
        )
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        log("provider %s answered non-JSON" % provider.get("id"))
        return None
    if not isinstance(payload, dict):
        return None
    return parse_upstream(provider.get("wire"), payload)


def serve_request(brief, api, body):
    """Route one client request; returns (model_id, text, in, out) or None."""
    req = extract_request(api, body)
    requires = brief.requirements(api, body)
    rows = candidates(brief, requires, req["json_mode"], api)
    for row in rows:
        provider = brief.by_id[row["provider"]]
        brief.uses[row["provider"]] = brief.uses.get(row["provider"], 0) + 1
        answer = call_provider(provider, row["id"], req)
        if answer is not None:
            return row["id"], answer[0], answer[1], answer[2]
    return None


def _sse_pieces(text):
    for at in range(0, len(text), SSE_PIECE_CHARS):
        yield text[at : at + SSE_PIECE_CHARS]


def render_openai(model_id, text, usage_in, usage_out, stream):
    usage = {
        "prompt_tokens": usage_in,
        "completion_tokens": usage_out,
        "total_tokens": usage_in + usage_out,
    }
    if not stream:
        return (
            200,
            "application/json",
            json.dumps(
                {
                    "id": "chatcmpl-agg",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": text,
                            },
                            "finish_reason": "stop",
                            "logprobs": None,
                        }
                    ],
                    "usage": usage,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    frames = []

    def chunk(delta, finish):
        return {
            "id": "chatcmpl-agg",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    frames.append(
        "data: %s\n\n"
        % json.dumps(chunk({"role": "assistant", "content": ""}, None),
                     separators=(",", ":"))
    )
    for piece in _sse_pieces(text):
        frames.append(
            "data: %s\n\n"
            % json.dumps(chunk({"content": piece}, None), separators=(",", ":"))
        )
    frames.append(
        "data: %s\n\n"
        % json.dumps(chunk({}, "stop"), separators=(",", ":"))
    )
    frames.append(
        "data: %s\n\n"
        % json.dumps(
            {
                "id": "chatcmpl-agg",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model_id,
                "choices": [],
                "usage": usage,
            },
            separators=(",", ":"),
        )
    )
    frames.append("data: [DONE]\n\n")
    return 200, "text/event-stream; charset=utf-8", "".join(frames).encode("utf-8")


def render_anthropic(model_id, text, usage_in, usage_out, stream):
    if not stream:
        return (
            200,
            "application/json",
            json.dumps(
                {
                    "id": "msg_agg",
                    "type": "message",
                    "role": "assistant",
                    "model": model_id,
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    frames = []

    def event(name, data):
        frames.append(
            "event: %s\ndata: %s\n\n"
            % (name, json.dumps(data, separators=(",", ":")))
        )

    event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_agg",
                "type": "message",
                "role": "assistant",
                "model": model_id,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage_in, "output_tokens": 0},
            },
        },
    )
    event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    for piece in _sse_pieces(text):
        event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": piece},
            },
        )
    event("content_block_stop", {"type": "content_block_stop", "index": 0})
    event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": usage_out},
        },
    )
    event("message_stop", {"type": "message_stop"})
    return 200, "text/event-stream; charset=utf-8", "".join(frames).encode("utf-8")


def render_error(api, message):
    if api == "openai":
        body = {
            "error": {
                "message": message,
                "type": "api_error",
                "param": None,
                "code": "upstream_unavailable",
            }
        }
    else:
        body = {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        }
    return 502, "application/json", json.dumps(body, separators=(",", ":")).encode(
        "utf-8"
    )


def model_list_payload(brief, headers):
    ids = brief.free_model_ids()
    if headers.get("x-api-key") is not None:
        return {
            "data": [
                {
                    "type": "model",
                    "id": mid,
                    "display_name": mid,
                    "created_at": "2026-01-01T00:00:00Z",
                    "free": True,
                }
                for mid in ids
            ],
            "has_more": False,
        }
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": "free-model-aggregator",
                "free": True,
            }
            for mid in ids
        ],
    }


BRIEF = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout/stderr clean of access logs
        pass

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/v1/models":
            payload = json.dumps(
                model_list_payload(BRIEF, self.headers), separators=(",", ":")
            ).encode("utf-8")
            self._send(200, "application/json", payload)
            return
        self._send(
            404,
            "application/json",
            b'{"error":"not_found"}',
        )

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/v1/messages"):
            self._send(404, "application/json", b'{"error":"not_found"}')
            return
        api = "openai" if path == "/v1/chat/completions" else "anthropic"
        try:
            body = json.loads(self._read_body().decode("utf-8"))
        except ValueError:
            self._send(*render_error(api, "the request body is not one JSON object"))
            return
        stream = body.get("stream") is True
        answer = serve_request(BRIEF, api, body)
        BRIEF.index += 1
        if answer is None:
            self._send(*render_error(api, "no provider could serve the request"))
            return
        model_id, text, usage_in, usage_out = answer
        if api == "openai":
            self._send(*render_openai(model_id, text, usage_in, usage_out, stream))
        else:
            self._send(
                *render_anthropic(model_id, text, usage_in, usage_out, stream)
            )


def _http_json(method, url, payload=None, headers=None):
    target = urllib.parse.urlparse(url)
    host = target.hostname or "127.0.0.1"
    port = target.port or (443 if target.scheme == "https" else 80)
    conn_cls = (
        http.client.HTTPSConnection
        if target.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(host, port, timeout=10)
    body = None
    merged = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        merged["content-type"] = "application/json"
    conn.request(method, target.path or "/", body=body, headers=merged)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, raw


def register(port, catalog):
    for attempt in range(REGISTER_RETRIES):
        try:
            status, raw = _http_json(
                "POST",
                ENV_URL + "/register",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "port": port,
                    "catalog": catalog,
                },
            )
        except (OSError, http.client.HTTPException) as exc:
            log("register attempt %d failed: %s" % (attempt + 1, exc))
            time.sleep(0.2)
            continue
        if status == 200:
            try:
                answer = json.loads(raw.decode("utf-8"))
            except ValueError:
                answer = {}
            if answer.get("ok") is True:
                log("registered: %s requests" % answer.get("requests"))
                return True
            log("register refused: HTTP %s %s" % (status, raw[:200]))
            return False
        log("register refused: HTTP %s" % status)
        return False
    return False


def work_done():
    try:
        status, raw = _http_json("GET", ENV_URL + "/work")
    except (OSError, http.client.HTTPException):
        return False
    if status != 200:
        return False
    try:
        return json.loads(raw.decode("utf-8")).get("done") is True
    except ValueError:
        return False


def main(argv):
    global BRIEF
    BRIEF = Brief()
    if "--list" in argv:
        if not PROMPT_FILE:
            sys.stdout.write('{"providers":[]}')
        else:
            sys.stdout.write(catalog_bytes(BRIEF.catalog).decode("utf-8"))
        sys.stdout.flush()
        return 0
    if not ENV_URL:
        log("MAKEFUN_ENV_URL is not set")
        return 1
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log("listening on port %d" % port)
    if not register(port, BRIEF.catalog):
        log("could not register with the env")
        server.shutdown()
        return 1
    for _ in range(MAX_POLLS):
        if work_done():
            log("work done; exiting 0")
            server.shutdown()
            return 0
        time.sleep(POLL_INTERVAL_S)
    log("work poll budget spent; exiting 0")
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
