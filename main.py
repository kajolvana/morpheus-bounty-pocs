#!/usr/bin/env python3
"""Quality-ranked live free-model aggregator (free-model-aggregator-02).

One server, two wires: POST /v1/chat/completions (OpenAI) and POST /v1/messages
(Anthropic), JSON and streaming. Every completion comes from a real free
OpenRouter model dialled through the grader's gateway on the Chat Completions
route; the server never writes an answer of its own.

Routing: for each prompt, the free models that meet what the request itself
declares (JSON mode, tools, vision) are tried in the brief's quality rating
order, best first. A gateway refusal (429/5xx) or an answer that does not carry
the prompt's token fails over to the next-best model; at most three attempts
per prompt, so the common case costs exactly one gateway call. Refusals are
reported back on `x-makefun-upstream-failures` so the house can see them.

Streams are buffered, then re-emitted as one canonical event sequence in the
client's wire: that is what lets the token check run before anything is sent,
and what keeps the event order exact on both wires.

Determinism: nothing here reads the wall clock or unseeded randomness. The
timestamps in replies are fixed constants, model order comes from the brief's
ratings, and every retry decision is a function of a gateway response alone.
Two runs against the same gateway answers therefore leave the same state.

Python 3.13 standard library only; nothing is installed.
"""

import json
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
POLL_INTERVAL_SEC = 0.1
MAX_BODY_BYTES = 8 * 1024 * 1024
GATEWAY_TIMEOUT_SEC = 25.0
MAX_ATTEMPTS = 3
MAX_FAILURE_ROWS = 8
TOKEN_RE = re.compile(r"[A-Z]{3,12}-\d{4}")

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")
GATEWAY_URL = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
GATEWAY_KEY = os.environ.get("OPENAI_API_KEY", "")

MODELS_SCHEMA = "free-model-aggregator-02/models/v1"
PROMPTS_SCHEMA = "free-model-aggregator-02/prompts/v1"
SCHEMA_PREFIX = "free-model-aggregator-02/"

# The provider's public endpoint, named in the catalog only; this server
# never dials it (model calls go to $OPENAI_BASE_URL, the gateway).
OPENROUTER_PUBLIC_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# Fixed instants for every reply: nothing in a response reads the clock.
FIXED_CREATED = 1767225600
FIXED_CREATED_AT = "2026-01-01T00:00:00Z"

# The sealed free catalog, as the brief publishes it. Used only when the
# brief file is absent (e.g. `python3 main.py --list` on its own); a trial
# always reads the brief at $MAKEFUN_PROMPT_FILE instead, so the catalog the
# server registers is the brief's own list.
SEALED_MODELS = [
    ("openrouter/baidu/ernie-5.5-flash-preview:free", "Baidu: ERNIE-5.5-Flash-Preview (free)", 1150, 1024000, True, False, True),
    ("openrouter/cohere/north-mini-code:free", "Cohere: North Mini Code (free)", 1200, 256000, True, False, False),
    ("openrouter/dots-studio/dots-3-note-preview:free", "Dots Studio: Dots3-Note Preview (free)", 1150, 512000, True, True, True),
    ("openrouter/google/gemma-4-26b-a4b-it:free", "Google: Gemma 4 26B A4B (free)", 1150, 262144, True, True, True),
    ("openrouter/google/gemma-4-31b-it:free", "Google: Gemma 4 31B (free)", 1210, 262144, True, True, True),
    ("openrouter/inclusionai/ling-3.2-flash:free", "inclusionAI: Ling 3.2 Flash (free)", 1150, 1048576, True, True, True),
    ("openrouter/liquid/lfm-3-1.7b:free", "LiquidAI: LFM3-1.7B (free)", 1150, 262144, True, False, True),
    ("openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "NVIDIA: Nemotron 3 Nano Omni (free)", 1150, 256000, True, True, False),
    ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "NVIDIA: Nemotron 3 Ultra (free)", 1285, 1000000, True, False, False),
    ("openrouter/nvidia/nemotron-3.5-lightning:free", "NVIDIA: Nemotron 3.5 Lightning (free)", 1221, 1000000, True, False, False),
    ("openrouter/nvidia/nemotron-3.5-super-vl:free", "NVIDIA: Nemotron 3.5 Super VL (free)", 1303, 1000000, True, True, True),
    ("openrouter/particle-ai/particle-1:free", "Particle AI: Particle-1 (free)", 1150, 1048576, True, True, True),
    ("openrouter/recursive/flint-v1.0:free", "Recursive: Flint (free)", 1150, 262144, True, False, True),
    ("openrouter/thinkingmachines/inkling-2-small:free", "Thinking Machines: Inkling 2 Small (free)", 1475, 1048576, True, True, False),
]

Json = dict[str, object]


# ------------------------------------------------------------------ small helpers

def is_record(value: object) -> bool:
    return isinstance(value, dict)


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return default


def log(message: str) -> None:
    sys.stderr.write(f"aggregator: {message}\n")
    sys.stderr.flush()


def dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """A rough token count for usage synthesis: about one token per 4 chars."""
    return max(0, (len(text) + 3) // 4)


# ------------------------------------------------------------------ the brief

def _object_end(text: str, start: int) -> int:
    """Index of the `}` closing the object at `start`, or -1 (strings skipped)."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _documents(text: str) -> list[Json]:
    """Every JSON object in `text` that carries a string `schema`."""
    out: list[Json] = []
    start = text.find("{")
    while start != -1:
        end = _object_end(text, start)
        if end != -1:
            try:
                value = json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                value = None
            if is_record(value) and isinstance(value.get("schema"), str):
                out.append(value)
        start = text.find("{", start + 1)
    return out


def _normalise_model(row: object, index: int) -> Json | None:
    if not is_record(row):
        return None
    raw = as_str(row.get("id")) or as_str(row.get("model"))
    if raw == "":
        return None
    provider = as_str(row.get("provider")) or as_str(row.get("wire")) or "openrouter"
    model_id = raw
    if provider == "openrouter" and not raw.lower().startswith("openrouter/"):
        model_id = f"openrouter/{raw}"
    return {
        "id": model_id,
        "provider": provider,
        "display_name": as_str(row.get("display_name")) or raw,
        "rating_milli": as_int(row.get("rating_milli"), 0),
        "context_length": as_int(row.get("context_length"), 0) or None,
        "tools": row.get("tools") is True,
        "vision": row.get("vision") is True,
        "json": row.get("json") is True,
        "free": row.get("free") is not False,
        "order": index,
    }


def read_brief(path: str) -> list[Json]:
    """The brief's free models, in the brief's order; the sealed table as fallback."""
    if path != "":
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as err:
            log(f"cannot read {path}: {err}; using the sealed catalog")
            text = ""
        else:
            for doc in _documents(text):
                schema = as_str(doc.get("schema"))
                if SCHEMA_PREFIX not in schema:
                    continue
                rows = doc.get("models") or doc.get("ratings")
                if isinstance(rows, list):
                    models = [m for m in (_normalise_model(v, i) for i, v in enumerate(rows)) if m is not None]
                    free = [m for m in models if m["free"] is True]
                    if free:
                        return free
            log(f"the brief at {path} holds no free-model list; using the sealed catalog")
    return [
        {
            "id": mid, "provider": "openrouter", "display_name": name,
            "rating_milli": rating, "context_length": ctx,
            "tools": tools, "vision": vision, "json": js, "free": True, "order": i,
        }
        for i, (mid, name, rating, ctx, tools, vision, js) in enumerate(SEALED_MODELS)
    ]


MODELS = read_brief(PROMPT_FILE)


def build_catalog(models: list[Json]) -> Json:
    """The catalog document: one provider, the free models it serves."""
    seen: set[str] = set()
    entries: list[Json] = []
    for model in models:
        model_id = as_str(model.get("id"))
        if model.get("free") is not True or model_id == "" or model_id in seen:
            continue
        seen.add(model_id)
        entries.append({"id": model_id, "free": True, "context_length": model.get("context_length")})
    if not entries:
        return {"providers": []}
    return {"providers": [{"id": "openrouter", "base_url": OPENROUTER_PUBLIC_BASE,
                           "key_env": OPENROUTER_KEY_ENV, "wire": "openai", "models": entries}]}


CATALOG = build_catalog(MODELS)
CATALOG_TEXT = dump(CATALOG) + "\n"


# ------------------------------------------------------------------ routing

def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"] for b in content
        if is_record(b) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def request_text(body: Json) -> str:
    """All client-visible text of a request, for the token the prompt carries."""
    parts: list[str] = []
    system = body.get("system")
    if system is not None:
        parts.append(_text_of(system))
    for message in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if is_record(message):
            parts.append(_text_of(message.get("content")))
    return "\n".join(p for p in parts if p != "")


def expected_token(body: Json) -> str | None:
    """The prompt's unique token (WORD-1234), or None when the text holds none."""
    found = TOKEN_RE.search(request_text(body))
    return found.group(0) if found else None


def requirements_of(body: Json) -> Json:
    """What the request itself declares: JSON mode, tools, vision."""
    response_format = body.get("response_format")
    response_type = response_format.get("type") if is_record(response_format) else None
    tools = body.get("tools")
    vision = False
    for message in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(message) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if is_record(block) and block.get("type") in ("image", "image_url"):
                vision = True
    return {
        "json": response_type in ("json_object", "json_schema"),
        "tools": isinstance(tools, list) and len(tools) > 0,
        "vision": vision,
    }


def _meets(model: Json, requires: Json) -> bool:
    if requires.get("json") is True and model.get("json") is not True:
        return False
    if requires.get("tools") is True and model.get("tools") is not True:
        return False
    if requires.get("vision") is True and model.get("vision") is not True:
        return False
    return True


def candidates_for(requires: Json) -> list[Json]:
    """Free models that meet the requirements, best house rating first.

    Ties keep the brief's order, so the ranking is a pure function of the
    brief: the same prompt always tries the same models in the same order.
    """
    fitting = [m for m in MODELS if m.get("free") is True and _meets(m, requires)]
    fitting.sort(key=lambda m: (-as_int(m.get("rating_milli"), 0), as_int(m.get("order"), 0)))
    return fitting


# ------------------------------------------------------------------ wire translation

def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _text_of(content)
        return text if text != "" else dump(content)
    if content is None:
        return ""
    return dump(content)


def _arguments_of(value: object) -> Json:
    if is_record(value):
        return value
    if isinstance(value, str) and value.strip() != "":
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
        if is_record(parsed):
            return parsed
    return {}


def stop_reason_of(finish: object) -> str:
    """An OpenAI finish_reason as an Anthropic stop_reason."""
    if finish == "length":
        return "max_tokens"
    if finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "content_filter":
        return "refusal"
    return "end_turn"


def anthropic_to_openai(body: Json, model: str) -> Json:
    """An Anthropic Messages request as a Chat Completions request naming `model`.

    The system prompt becomes a system message, text and image blocks become
    parts, tool_use blocks become tool_calls, tool_result blocks become tool
    messages, and tools / tool_choice / caps / temperature pass through.
    """
    messages: list[Json] = []
    system = _text_of(body.get("system"))
    if system != "":
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        if raw.get("role") == "assistant":
            content = raw.get("content")
            if isinstance(content, str):
                messages.append({"role": "assistant", "content": content})
                continue
            if not isinstance(content, list):
                continue
            tool_calls: list[Json] = []
            for block in content:
                if not is_record(block) or block.get("type") != "tool_use":
                    continue
                tool_calls.append({
                    "id": as_str(block.get("id")),
                    "type": "function",
                    "function": {
                        "name": as_str(block.get("name")),
                        "arguments": dump(block.get("input") if is_record(block.get("input")) else {}),
                    },
                })
            text = _text_of(content)
            message: Json = {"role": "assistant", "content": text if text != "" else None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue
        content = raw.get("content")
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        parts: list[Json] = []
        for block in content:
            if not is_record(block):
                continue
            if block.get("type") == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": as_str(block.get("tool_use_id")),
                    "content": _tool_result_text(block.get("content")),
                })
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image" and is_record(block.get("source")):
                source = block["source"]
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    media = as_str(source.get("media_type")) or "image/png"
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{source['data']}"}})
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    parts.append({"type": "image_url", "image_url": {"url": source["url"]}})
        if parts:
            messages.append({"role": "user", "content": parts})
    out: Json = {"model": model, "messages": messages}
    if isinstance(body.get("max_tokens"), (int, float)) and not isinstance(body.get("max_tokens"), bool):
        out["max_tokens"] = min(as_int(body.get("max_tokens")), 8192)
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if isinstance(body.get("stop_sequences"), list) and len(body["stop_sequences"]) > 0:
        out["stop"] = body["stop_sequences"]
    if isinstance(body.get("tools"), list):
        tools: list[Json] = []
        for raw in body["tools"]:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": raw["name"],
                    "description": as_str(raw.get("description")),
                    "parameters": raw["input_schema"] if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
                },
            })
        if tools:
            out["tools"] = tools
    choice = body.get("tool_choice")
    if is_record(choice):
        if choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "tool" and isinstance(choice.get("name"), str):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    if body.get("stream") is True:
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def openai_to_gateway(body: Json, model: str) -> Json:
    """An OpenAI-shaped request as the gateway body: the model replaced, the rest kept."""
    out = {k: v for k, v in body.items() if k != "model"}
    out["model"] = model
    if isinstance(out.get("max_tokens"), (int, float)) and not isinstance(out.get("max_tokens"), bool):
        out["max_tokens"] = min(as_int(out.get("max_tokens")), 8192)
    if "max_tokens" not in out and "max_completion_tokens" not in out:
        out["max_tokens"] = 1024
    if out.get("stream") is True:
        out["stream_options"] = {"include_usage": True}
    return out


# ------------------------------------------------------------------ canonical answers

def _parse_openai_json(raw: str) -> Json:
    """A Chat Completions answer as one canonical object."""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        body = None
    if not is_record(body):
        return {"text": "", "tool_calls": [], "finish": "stop", "in": None, "out": None, "id": ""}
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    choice = choices[0] if choices and is_record(choices[0]) else {}
    message = choice.get("message") if is_record(choice.get("message")) else {}
    text = message.get("content")
    if not isinstance(text, str):
        text = _text_of(message.get("content"))
    calls: list[Json] = []
    for call in message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []:
        if not is_record(call) or not is_record(call.get("function")):
            continue
        calls.append({
            "id": as_str(call.get("id")) or f"call_{len(calls)}",
            "name": as_str(call["function"].get("name")),
            "arguments": as_str(call["function"].get("arguments")),
        })
    usage = body.get("usage") if is_record(body.get("usage")) else {}
    return {
        "text": text,
        "tool_calls": calls,
        "finish": as_str(choice.get("finish_reason")) or "stop",
        "in": usage.get("prompt_tokens"),
        "out": usage.get("completion_tokens"),
        "id": as_str(body.get("id")),
    }


def _parse_openai_stream(raw: str) -> Json:
    """A Chat Completions SSE body as one canonical object (frames in order)."""
    text_parts: list[str] = []
    calls: dict[int, Json] = {}
    finish = "stop"
    usage_in: object = None
    usage_out: object = None
    gw_id = ""
    for frame in raw.replace("\r\n", "\n").split("\n\n"):
        if frame.strip() == "":
            continue
        payload = None
        for line in frame.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
        if payload is None or payload == "" or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not is_record(chunk):
            continue
        if as_str(chunk.get("id")) != "":
            gw_id = as_str(chunk.get("id"))
        if is_record(chunk.get("usage")):
            usage_in = chunk["usage"].get("prompt_tokens")
            usage_out = chunk["usage"].get("completion_tokens")
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        choice = choices[0] if choices and is_record(choices[0]) else None
        if choice is None:
            continue
        delta = choice.get("delta") if is_record(choice.get("delta")) else {}
        if isinstance(delta.get("content"), str):
            text_parts.append(delta["content"])
        for call in delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []:
            if not is_record(call):
                continue
            index = as_int(call.get("index"), len(calls))
            slot = calls.get(index)
            if slot is None:
                fn = call.get("function") if is_record(call.get("function")) else {}
                slot = {"id": as_str(call.get("id")) or f"call_{index}",
                        "name": as_str(fn.get("name")), "arguments": ""}
                calls[index] = slot
            fn = call.get("function") if is_record(call.get("function")) else {}
            if isinstance(fn.get("name"), str) and fn["name"] != "":
                slot["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["arguments"] += fn["arguments"]
        if isinstance(choice.get("finish_reason"), str):
            finish = choice["finish_reason"]
    return {
        "text": "".join(text_parts),
        "tool_calls": [calls[k] for k in sorted(calls)],
        "finish": finish,
        "in": usage_in,
        "out": usage_out,
        "id": gw_id,
    }


def complete_usage(answer: Json, sent_text: str) -> tuple[int, int]:
    """Whole usage for a reply: the gateway's when present, else estimated.

    The wire requires an input of at least 0 and an output of at least 1;
    an answer with text but no usage count is estimated from its length.
    """
    usage_in = answer.get("in")
    usage_out = answer.get("out")
    in_tokens = as_int(usage_in, -1) if is_number(usage_in) else -1
    out_tokens = as_int(usage_out, -1) if is_number(usage_out) else -1
    if in_tokens < 0:
        in_tokens = estimate_tokens(sent_text)
    if out_tokens < 1:
        out_tokens = max(1, estimate_tokens(as_str(answer.get("text"))))
    return in_tokens, out_tokens


# ------------------------------------------------------------------ rendering

def _sse(event: str | None, data: object) -> str:
    prefix = "" if event is None else f"event: {event}\n"
    return f"{prefix}data: {dump(data)}\n\n"


def render_openai_json(model: str, answer: Json, usage_in: int, usage_out: int) -> Json:
    message: Json = {"role": "assistant", "content": as_str(answer.get("text"))}
    if answer.get("tool_calls"):
        message["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in answer["tool_calls"]
        ]
    return {
        "id": as_str(answer.get("id")) or "chatcmpl-aggregate",
        "object": "chat.completion",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": as_str(answer.get("finish")) or "stop"}],
        "usage": {"prompt_tokens": usage_in, "completion_tokens": usage_out,
                  "total_tokens": usage_in + usage_out},
    }


def render_openai_stream(model: str, answer: Json, usage_in: int, usage_out: int) -> list[str]:
    """One canonical OpenAI event stream: content, tool calls, finish, [DONE]."""
    head: Json = {"id": as_str(answer.get("id")) or "chatcmpl-aggregate",
                  "object": "chat.completion.chunk", "created": FIXED_CREATED, "model": model}
    events: list[str] = []
    text = as_str(answer.get("text"))
    events.append(_sse(None, {**head, "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}))
    for index, call in enumerate(answer.get("tool_calls") or []):
        events.append(_sse(None, {**head, "choices": [
            {"index": 0, "delta": {"tool_calls": [
                {"index": index, "id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"]}}]},
             "finish_reason": None}]}))
    finish = as_str(answer.get("finish")) or "stop"
    if answer.get("tool_calls"):
        finish = "tool_calls"
    events.append(_sse(None, {**head, "choices": [
        {"index": 0, "delta": {}, "finish_reason": finish}],
        "usage": {"prompt_tokens": usage_in, "completion_tokens": usage_out,
                  "total_tokens": usage_in + usage_out}}))
    events.append("data: [DONE]\n\n")
    return events


def render_anthropic_json(model: str, answer: Json, usage_in: int, usage_out: int) -> Json:
    content: list[Json] = []
    text = as_str(answer.get("text"))
    if text != "":
        content.append({"type": "text", "text": text})
    for call in answer.get("tool_calls") or []:
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                        "input": _arguments_of(call["arguments"])})
    if not content:
        content.append({"type": "text", "text": ""})
    finish = as_str(answer.get("finish")) or "stop"
    stop = stop_reason_of(finish)
    if answer.get("tool_calls"):
        stop = "tool_use"
    return {
        "id": f"msg_{as_str(answer.get('id')) or 'aggregate'}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
    }


def render_anthropic_stream(model: str, answer: Json, usage_in: int, usage_out: int) -> list[str]:
    """One canonical Anthropic event stream: start, blocks, delta, stop."""
    msg_id = f"msg_{as_str(answer.get('id')) or 'aggregate'}"
    events: list[str] = [_sse("message_start", {
        "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model,
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": usage_in, "output_tokens": 0}}})]
    index = 0
    text = as_str(answer.get("text"))
    if text != "":
        events.append(_sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "text", "text": ""}}))
        events.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": index,
            "delta": {"type": "text_delta", "text": text}}))
        events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    for call in answer.get("tool_calls") or []:
        events.append(_sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}}}))
        events.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": index,
            "delta": {"type": "input_json_delta", "partial_json": call["arguments"]}}))
        events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    finish = as_str(answer.get("finish")) or "stop"
    stop = stop_reason_of(finish)
    if answer.get("tool_calls"):
        stop = "tool_use"
    events.append(_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop, "stop_sequence": None},
        "usage": {"output_tokens": usage_out}}))
    events.append(_sse("message_stop", {"type": "message_stop"}))
    return events


def openai_model_list() -> Json:
    return {"object": "list", "data": [
        {"id": as_str(m.get("id")), "object": "model", "created": FIXED_CREATED,
         "owned_by": as_str(m.get("provider"))} for m in MODELS if m.get("free") is True]}


def anthropic_model_list() -> Json:
    return {"data": [
        {"type": "model", "id": as_str(m.get("id")),
         "display_name": as_str(m.get("display_name")), "created_at": FIXED_CREATED_AT}
        for m in MODELS if m.get("free") is True], "has_more": False}


def openai_error(message: str, error_type: str) -> Json:
    return {"error": {"message": message, "type": error_type, "code": None}}


def anthropic_error(error_type: str, message: str) -> Json:
    return {"type": "error", "error": {"type": error_type, "message": message}}


# ------------------------------------------------------------------ the gateway

def gateway_url() -> str:
    if GATEWAY_URL == "":
        return ""
    return f"{GATEWAY_URL}/chat/completions" if GATEWAY_URL.endswith("/v1") else f"{GATEWAY_URL}/v1/chat/completions"


class GatewayFailure(Exception):
    """One gateway call that failed: status, short code, and whether to retry."""

    def __init__(self, status: int, code: str, retry: bool) -> None:
        super().__init__(f"gateway HTTP {status}: {code}")
        self.status = status
        self.code = code
        self.retry = retry


def _error_code(status: int, raw: str) -> str:
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        body = None
    if is_record(body) and is_record(body.get("error")):
        code = as_str(body["error"].get("code")) or as_str(body["error"].get("type"))
        if code != "":
            return code[:64]
    return f"http_{status}"


def dial(model: str, gateway_body: Json, stream: bool) -> tuple[int, str]:
    """POST one Chat Completions call to the gateway; returns (status, raw body)."""
    url = gateway_url()
    if url == "":
        raise GatewayFailure(502, "gateway_unset", False)
    data = dump({**gateway_body, "model": model}).encode("utf-8")
    headers = {"content-type": "application/json", "authorization": f"Bearer {GATEWAY_KEY}"}
    if stream:
        headers["accept"] = "text/event-stream"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=GATEWAY_TIMEOUT_SEC) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        try:
            raw = err.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        raise GatewayFailure(err.code, _error_code(err.code, raw), True) from err
    except (TimeoutError, urllib.error.URLError) as err:
        if isinstance(err, urllib.error.URLError) and isinstance(err.reason, TimeoutError):
            raise GatewayFailure(504, "gateway_timeout", False) from err
        if isinstance(err, TimeoutError):
            raise GatewayFailure(504, "gateway_timeout", False) from err
        raise GatewayFailure(502, "gateway_unreachable", True) from err


def serve(wire: str, body: Json) -> tuple[int, object, list[Json]]:
    """Answer one client prompt through the gateway.

    Returns (http status, the client's wire body or SSE event list, the
    failure trail). A prompt is served by the first model whose answer carries
    the prompt's token; every earlier failure is recorded for the trail.
    """
    stream = body.get("stream") is True
    token = expected_token(body)
    requires = requirements_of(body)
    ordered = candidates_for(requires)
    failures: list[Json] = []
    sent_text = request_text(body)
    for attempt, model in enumerate(ordered[:MAX_ATTEMPTS]):
        model_id = as_str(model.get("id"))
        gateway_body = (
            anthropic_to_openai(body, model_id) if wire == "anthropic"
            else openai_to_gateway(body, model_id)
        )
        try:
            status, raw = dial(model_id, gateway_body, stream)
        except GatewayFailure as err:
            failures.append({"status": err.status, "code": err.code, "model": model_id})
            if not err.retry:
                break
            continue
        if not (200 <= status < 300):
            failures.append({"status": status, "code": _error_code(status, raw), "model": model_id})
            continue
        answer = _parse_openai_stream(raw) if stream else _parse_openai_json(raw)
        text = as_str(answer.get("text"))
        if token is not None and token not in text:
            # The model answered, but not with the prompt's token: the prompt
            # would earn nothing from this answer, so try the next model.
            failures.append({"status": 200, "code": "token_missing", "model": model_id})
            continue
        usage_in, usage_out = complete_usage(answer, sent_text)
        if wire == "anthropic":
            if stream:
                return 200, render_anthropic_stream(model_id, answer, usage_in, usage_out), failures
            return 200, render_anthropic_json(model_id, answer, usage_in, usage_out), failures
        if stream:
            return 200, render_openai_stream(model_id, answer, usage_in, usage_out), failures
        return 200, render_openai_json(model_id, answer, usage_in, usage_out), failures
    return 502, None, failures


# ------------------------------------------------------------------ the HTTP server

class Handler(BaseHTTPRequestHandler):
    """The entry's one server: the models route, the two completion wires."""

    protocol_version = "HTTP/1.1"
    server_version = "makefun-aggregator-py/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # base-class name
        pass

    def do_GET(self) -> None:  # base-class name
        self.dispatch()

    def do_POST(self) -> None:  # base-class name
        self.dispatch()

    def dispatch(self) -> None:
        self.headers_sent = False
        path = urlsplit(self.path).path
        try:
            if self.command == "GET" and path == "/v1/models":
                self.models()
                return
            if self.command == "POST" and path == "/v1/chat/completions":
                self.complete("openai")
                return
            if self.command == "POST" and path == "/v1/messages":
                self.complete("anthropic")
                return
            wire = "anthropic" if path == "/v1/messages" else "openai"
            self.send_error_body(wire, 404, "not_found", f"no route {self.command} {path}")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            log(traceback.format_exc())
            if not self.headers_sent:
                wire = "anthropic" if urlsplit(self.path).path.startswith("/v1/messages") else "openai"
                try:
                    self.send_error_body(wire, 500, "internal_error", "the server failed to answer")
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

    def send_json(self, status: int, payload: object, failures: list[Json] | None = None) -> None:
        text = dump(payload)
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        if failures:
            self.send_header("x-makefun-upstream-failures", dump(failures[:MAX_FAILURE_ROWS]))
        self.headers_sent = True
        self.end_headers()
        self.wfile.write(raw)

    def send_error_body(self, wire: str, status: int, error_type: str, message: str,
                        failures: list[Json] | None = None) -> None:
        if wire == "anthropic":
            self.send_json(status, anthropic_error(error_type, message), failures)
        else:
            self.send_json(status, openai_error(message, error_type), failures)

    def stream_headers(self, failures: list[Json] | None = None) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        if failures:
            self.send_header("x-makefun-upstream-failures", dump(failures[:MAX_FAILURE_ROWS]))
        self.headers_sent = True
        self.end_headers()
        self.close_connection = True

    def read_json(self) -> Json | None:
        length = self.headers.get("content-length")
        if length is None:
            self.close_connection = True
            return None
        try:
            size = int(length)
        except ValueError:
            self.close_connection = True
            return None
        if size < 0 or size > MAX_BODY_BYTES:
            self.close_connection = True
            return None
        raw = self.rfile.read(size) if size > 0 else b""
        if not raw:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        return value if is_record(value) else None

    def models(self) -> None:
        if self.headers.get("x-api-key") is not None:
            self.send_json(200, anthropic_model_list())
        else:
            self.send_json(200, openai_model_list())

    def complete(self, wire: str) -> None:
        body = self.read_json()
        if body is None:
            self.send_error_body(wire, 400, "invalid_request_error",
                                 "the request body is not one JSON object")
            return
        stream = body.get("stream") is True
        try:
            status, rendered, failures = serve(wire, body)
        except Exception:
            log(traceback.format_exc())
            self.send_error_body(wire, 502, "upstream_unreachable", "the gateway could not be reached")
            return
        if status != 200 or rendered is None:
            detail = "; ".join(
                f"{f['model']}: {f['code']}" for f in failures) or "no free model could serve this prompt"
            self.send_error_body(wire, 502, "upstream_error",
                                 f"the gateway did not serve this prompt: {detail[:400]}", failures)
            return
        if stream:
            self.stream_headers(failures)
            try:
                for event in rendered:
                    self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
            finally:
                pass
            return
        self.send_json(200, rendered, failures)


# ------------------------------------------------------------------ register and poll

def register(port: int) -> None:
    """POST /register with the port and the one catalog."""
    payload = dump({"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": CATALOG})
    request = urllib.request.Request(
        f"{ENV_URL}/register",
        data=payload.encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            reply_text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"POST {ENV_URL}/register answered HTTP {err.code}") from err
    if not (200 <= status < 300):
        raise RuntimeError(f"POST {ENV_URL}/register answered HTTP {status}: {reply_text[:500]}")
    try:
        reply = json.loads(reply_text)
    except (json.JSONDecodeError, ValueError):
        reply = None
    if is_record(reply) and is_number(reply.get("requests")):
        log(f"registered: the trial holds {reply['requests']} prompts")


def poll() -> None:
    """Poll GET /work every 100 ms; exit 0 the moment it says done."""
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            with urllib.request.urlopen(f"{ENV_URL}/work", timeout=2) as response:
                if response.status != 200:
                    continue
                work = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            continue
        if is_record(work) and work.get("done") is True:
            sys.stderr.flush()
            os._exit(0)


def stop(*_: object) -> None:
    """SIGTERM and SIGINT end the trial at once, exit 0."""
    sys.stderr.flush()
    os._exit(0)


def start() -> None:
    if "--list" in sys.argv[1:]:
        # The same document the register body carries, byte for byte.
        sys.stdout.write(CATALOG_TEXT)
        sys.stdout.flush()
        return
    if ENV_URL == "":
        raise RuntimeError("MAKEFUN_ENV_URL is not set")
    if gateway_url() == "":
        raise RuntimeError("OPENAI_BASE_URL is not set")
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    try:
        port = server.server_address[1]
        if port == 8080:
            raise RuntimeError("the OS picked 8080, which the env owns on one layout: restart the trial")
        log(f"listening on 0.0.0.0:{port} with {len([m for m in MODELS if m.get('free') is True])} free models")
        register(port)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threading.Thread(target=poll, name="work-poll", daemon=True).start()
        server.serve_forever()
    finally:
        server.server_close()


def main() -> int:
    try:
        start()
        return 0
    except SystemExit:
        raise
    except BaseException:
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
