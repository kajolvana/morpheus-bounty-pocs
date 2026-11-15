#!/usr/bin/env python3
"""Adaptive free-model aggregator for free-model-aggregator-01.

One server, two client wires (OpenAI Chat Completions, Anthropic Messages),
ten simulated free-tier providers on three upstream wires (openai, anthropic,
gemini). Python 3.13, standard library only.

ADAPTIVE ROUTING (this entry's strategy)
----------------------------------------
Candidates are ranked by the brief's quality table (rating_milli), but the
router *learns* each provider's failure schedule from live responses instead
of trusting the first plan:

* Every provider starts healthy. A refusal (402 / 429 / 5xx / unreachable)
  is recorded with the request index at which it happened.
* After a refusal a provider is cooled down: it is skipped for the next
  1 request after its 1st consecutive refusal, 3 requests after its 2nd,
  and retired for the rest of the trial after its 3rd consecutive refusal.
  A retry-after answer is read the same way (skip the next request).
  A refusal is usually a scheduled fail-call or a spent budget; either way
  the next call to that provider is the least likely to pay, so the router
  shifts load to healthy providers first and only returns to the cooled
  provider once the cooldown has passed (a scheduled fail-call has passed
  by then) or when no healthy candidate is left.
* A success clears the consecutive-refusal count, so a provider that
  refused once on its schedule comes back into rotation.
* Rating ties are broken by fewest calls made so far on that provider,
  which spreads load across providers and keeps any single budget from
  being the first to run dry; then by provider id, then model id.
* All state is a function of the request index and the provider answers
  seen so far: no wall clock, no unseeded randomness. A replay therefore
  makes exactly the same routing decisions.

Wire rules the translator must preserve (the client compares the provider's
echo of the request against what it sent): system text, user text, tool
names in order, max_tokens, stop sequences, the image, and JSON mode.
An anthropic-wire provider can never echo JSON mode, so a JSON-mode
request from an OpenAI client is never routed to one; the gemini wire
carries it through responseMimeType instead.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
POLL_INTERVAL_S = 0.1
PROBE_TIMEOUT_S = 5.0
PROVIDER_TIMEOUT_S = 15.0
MAX_BODY_BYTES = 32 * 1024 * 1024
FIXED_CREATED = 1767225600
FIXED_CREATED_AT = "2026-01-01T00:00:00Z"
ANTHROPIC_VERSION = "2023-06-01"
SSE_PIECE = 13  # characters per streamed text chunk, like the providers

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")


def log(msg):
    sys.stderr.write("aggregator: %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def is_record(v):
    return isinstance(v, dict)


def str_of(v):
    return v if isinstance(v, str) else ""


def num_or_none(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def bool_or_none(v):
    return v if isinstance(v, bool) else None


def text_of(content):
    """Text of a string or of a block array's text blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if is_record(b) and b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(parts)


def split_text(text):
    if text == "":
        return [""]
    return [text[i:i + SSE_PIECE] for i in range(0, len(text), SSE_PIECE)]


# --------------------------------------------------------------------------
# brief parsing: the three JSON documents embedded in the prompt file
# --------------------------------------------------------------------------

def _object_end(text, start):
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _documents_in(text):
    out = []
    start = text.find("{")
    while start != -1:
        end = _object_end(text, start)
        if end != -1:
            try:
                value = json.loads(text[start:end + 1])
            except ValueError:
                value = None
            if is_record(value) and isinstance(value.get("schema"), str):
                out.append(value)
        start = text.find("{", start + 1)
    return out


class Brief:
    def __init__(self, path):
        self.providers = []   # [{id,name,wire,auth,base,key_env,key}]
        self.ratings = []     # [{model,provider,rating_milli,context_length,tools,vision,json,free}]
        self.best = {}
        self.manifest = []    # [{id,api,stream,weight,requires}]
        self.error = None
        if not path:
            self.error = "MAKEFUN_PROMPT_FILE is not set"
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            self.error = "cannot read %s: %s" % (path, exc)
            return
        docs = _documents_in(text)
        directory = next((d for d in docs
                          if d.get("schema") == "free-model-aggregator-01/providers/v1"
                          and isinstance(d.get("providers"), list)), None)
        if directory is None:
            self.error = "the brief holds no providers document"
            return
        for raw in directory["providers"]:
            if not is_record(raw):
                continue
            pid = str_of(raw.get("id"))
            wire = raw.get("wire")
            if pid == "" or wire not in ("openai", "anthropic", "gemini"):
                continue
            auth = raw.get("auth")
            if auth not in ("bearer", "x-api-key", "x-goog-api-key", "none"):
                auth = "bearer"
            base = (str_of(raw.get("path")) or str_of(raw.get("base_path"))
                    or str_of(raw.get("base_url")) or str_of(raw.get("base")))
            self.providers.append({
                "id": pid, "name": str_of(raw.get("name")) or pid,
                "wire": wire, "auth": auth, "base": base,
                "key_env": str_of(raw.get("key_env")),
                "key": str_of(raw.get("key")),
            })
        ratings_doc = next((d for d in docs
                            if d.get("schema") == "free-model-aggregator-01/ratings/v1"
                            and isinstance(d.get("ratings"), list)), None)
        if ratings_doc is not None:
            for raw in ratings_doc["ratings"]:
                if not is_record(raw):
                    continue
                model = str_of(raw.get("model"))
                provider = str_of(raw.get("provider"))
                if model == "" or provider == "":
                    continue
                self.ratings.append({
                    "model": model, "provider": provider,
                    "rating_milli": raw.get("rating_milli") if isinstance(raw.get("rating_milli"), (int, float)) else 0,
                    "context_length": num_or_none(raw.get("context_length")),
                    "tools": raw.get("tools") is True,
                    "vision": raw.get("vision") is True,
                    "json": raw.get("json") is True,
                    "free": raw.get("free") is True,
                })
            for raw in ratings_doc.get("best") or []:
                if is_record(raw) and str_of(raw.get("id")):
                    self.best[raw["id"]] = raw.get("rating_milli") or 0
        workload_doc = next((d for d in docs
                             if d.get("schema") == "free-model-aggregator-01/workload/v1"
                             and isinstance(d.get("manifest"), list)), None)
        if workload_doc is not None:
            for raw in workload_doc["manifest"]:
                if not is_record(raw) or str_of(raw.get("id")) == "":
                    continue
                req = raw.get("requires") if is_record(raw.get("requires")) else {}
                requires = {}
                if req.get("tools") is True:
                    requires["tools"] = True
                if req.get("vision") is True:
                    requires["vision"] = True
                if req.get("json") is True:
                    requires["json"] = True
                if isinstance(req.get("min_context"), (int, float)):
                    requires["min_context"] = req["min_context"]
                self.manifest.append({
                    "id": raw["id"],
                    "api": "anthropic" if raw.get("api") == "anthropic" else "openai",
                    "stream": raw.get("stream") is True,
                    "weight": 2 if raw.get("weight") == 2 else 1,
                    "requires": requires,
                })

    def rating_row(self, provider_id, model_id):
        for row in self.ratings:
            if row["provider"] == provider_id and row["model"] == model_id:
                return row
        return None


# --------------------------------------------------------------------------
# catalog probe
# --------------------------------------------------------------------------

def join_base(env_url, base):
    b = (base or "").strip()
    if b.lower().startswith("http://") or b.lower().startswith("https://"):
        return b.rstrip("/")
    if b == "":
        return env_url
    return env_url + ("" if b.startswith("/") else "/") + b.rstrip("/")


def auth_headers(auth, key):
    if key == "":
        return {}
    if auth == "bearer":
        return {"authorization": "Bearer " + key}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def _zero_priced(record):
    pricing = record.get("pricing") if is_record(record.get("pricing")) else None
    if pricing is None:
        return None
    values = []
    for v in pricing.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            values.append(v)
        elif isinstance(v, str) and v.strip() != "":
            try:
                values.append(float(v))
            except ValueError:
                pass
    if not values:
        return None
    return all(v == 0 for v in values)


def _model_in(wire, raw):
    if not is_record(raw):
        return None
    if wire == "gemini":
        mid = str_of(raw.get("name")).removeprefix("models/")
    else:
        mid = str_of(raw.get("id"))
    if mid == "":
        return None
    context = num_or_none(raw.get("context_length"))
    if context is None:
        context = num_or_none(raw.get("context_window"))
    if context is None:
        context = num_or_none(raw.get("inputTokenLimit"))
    display = (str_of(raw.get("display_name")) or str_of(raw.get("displayName")) or mid)
    free = bool_or_none(raw.get("free"))
    if free is None:
        free = _zero_priced(raw)
    return {
        "id": mid, "display_name": display, "free": free,
        "context_length": context,
        "tools": bool_or_none(raw.get("tools")),
        "vision": bool_or_none(raw.get("vision")),
        "json": bool_or_none(raw.get("json")),
    }


def probe_provider(env_url, provider):
    """GET one provider's catalog route in its own wire."""
    base = join_base(env_url, provider["base"])
    url = base + ("/v1beta/models" if provider["wire"] == "gemini" else "/v1/models")
    headers = {"accept": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            if resp.status != 200:
                return {"provider": provider, "ok": False, "models": [],
                        "error": "GET %s answered HTTP %s" % (url, resp.status)}
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a failed probe leaves the provider out
        return {"provider": provider, "ok": False, "models": [],
                "error": "GET %s failed: %s" % (url, exc)}
    if not is_record(body):
        return {"provider": provider, "ok": False, "models": [],
                "error": "GET %s answered non-JSON" % url}
    listing = body.get("models") if provider["wire"] == "gemini" else body.get("data")
    models = []
    if isinstance(listing, list):
        for raw in listing:
            model = _model_in(provider["wire"], raw)
            if model is not None:
                models.append(model)
    return {"provider": provider, "ok": True, "models": models, "error": None}


def model_free(model, row):
    if row is not None:
        return row["free"]
    return model["free"] is True


def capabilities_of(model, row):
    if row is not None:
        return {"context_length": row["context_length"], "tools": row["tools"],
                "vision": row["vision"], "json": row["json"]}
    return {"context_length": model["context_length"], "tools": model["tools"],
            "vision": model["vision"], "json": model["json"]}


def meets_requirements(requires, caps):
    if requires.get("tools") is True and caps.get("tools") is not True:
        return False
    if requires.get("vision") is True and caps.get("vision") is not True:
        return False
    if requires.get("json") is True and caps.get("json") is not True:
        return False
    min_ctx = requires.get("min_context")
    if min_ctx is not None:
        ctx = caps.get("context_length")
        if ctx is None or ctx < min_ctx:
            return False
    return True


def build_catalog(brief, probes):
    """The --list document and the register body: the same object."""
    providers = []
    for probe in probes:
        if not probe["ok"]:
            continue
        seen = set()
        models = []
        for model in sorted(probe["models"], key=lambda m: m["id"]):
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            row = brief.rating_row(probe["provider"]["id"], model["id"])
            context = row["context_length"] if row is not None else model["context_length"]
            models.append({"id": model["id"], "free": model_free(model, row),
                           "context_length": context})
        providers.append({
            "id": probe["provider"]["id"],
            "base_url": probe["provider"]["base"],
            "key_env": probe["provider"]["key_env"],
            "wire": probe["provider"]["wire"],
            "models": models,
        })
    return {"providers": providers}


# --------------------------------------------------------------------------
# wire translation
# --------------------------------------------------------------------------

def _args_object(value):
    if is_record(value):
        return value
    if isinstance(value, str) and value.strip() != "":
        try:
            parsed = json.loads(value)
            if is_record(parsed):
                return parsed
        except ValueError:
            pass
    return {}


def _image_part_to_anthropic(part):
    """An OpenAI image_url part as an Anthropic image block, or None."""
    if not is_record(part) or part.get("type") != "image_url":
        return None
    image_url = part.get("image_url") if is_record(part.get("image_url")) else {}
    url = str_of(image_url.get("url"))
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if url != "":
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _image_block_to_openai(block):
    """An Anthropic image block as an OpenAI image_url part, or None."""
    if not is_record(block) or block.get("type") != "image":
        return None
    source = block.get("source") if is_record(block.get("source")) else {}
    if source.get("type") == "base64" and isinstance(source.get("data"), str):
        mime = str_of(source.get("media_type")) or "image/png"
        return {"type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (mime, source["data"])}}
    if source.get("type") == "url" and isinstance(source.get("url"), str):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = text_of(content)
        return text if text != "" else json.dumps(content)
    return "" if content is None else json.dumps(content)


# --- requests: client wire -> provider wire -------------------------------

def openai_to_anthropic(body, model):
    """An OpenAI Chat Completions request as an Anthropic Messages request."""
    messages = []
    system_texts = []
    for raw in body.get("messages") or []:
        if not is_record(raw):
            continue
        if raw.get("role") == "system":
            text = text_of(raw.get("content"))
            if text != "":
                system_texts.append(text)
            continue
        if raw.get("role") == "assistant":
            content = []
            text = text_of(raw.get("content"))
            if text != "":
                content.append({"type": "text", "text": text})
            for call in raw.get("tool_calls") or []:
                if not is_record(call) or not is_record(call.get("function")):
                    continue
                fn = call["function"]
                content.append({"type": "tool_use",
                                "id": str_of(call.get("id")),
                                "name": str_of(fn.get("name")),
                                "input": _args_object(fn.get("arguments"))})
            if content:
                messages.append({"role": "assistant", "content": content})
            continue
        if raw.get("role") == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": str_of(raw.get("tool_call_id")),
                "content": raw.get("content") if isinstance(raw.get("content"), str)
                           else _tool_result_text(raw.get("content")),
            }]})
            continue
        # user
        content = raw.get("content")
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        if isinstance(content, list):
            parts = []
            for block in content:
                if not is_record(block):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append({"type": "text", "text": block["text"]})
                else:
                    image = _image_part_to_anthropic(block)
                    if image is not None:
                        parts.append(image)
            if parts:
                messages.append({"role": "user", "content": parts})
    out = {"model": model, "messages": messages}
    if system_texts:
        out["system"] = "\n".join(system_texts)
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    elif isinstance(body.get("max_completion_tokens"), (int, float)):
        out["max_tokens"] = body["max_completion_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stops = body.get("stop")
    if isinstance(stops, str):
        stops = [stops]
    if isinstance(stops, list) and stops:
        out["stop_sequences"] = [s for s in stops if isinstance(s, str)]
    tools = []
    for raw in body.get("tools") or []:
        if not is_record(raw) or not is_record(raw.get("function")):
            continue
        fn = raw["function"]
        if str_of(fn.get("name")) == "":
            continue
        tools.append({"name": fn["name"],
                      "description": str_of(fn.get("description")),
                      "input_schema": fn.get("parameters") if is_record(fn.get("parameters"))
                                      else {"type": "object", "properties": {}}})
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif is_record(choice) and choice.get("type") == "function" and is_record(choice.get("function")):
        out["tool_choice"] = {"type": "tool", "name": str_of(choice["function"].get("name"))}
    if body.get("stream") is True:
        out["stream"] = True
    return out


def anthropic_to_openai(body, model):
    """An Anthropic Messages request as an OpenAI Chat Completions request."""
    messages = []
    system = text_of(body.get("system"))
    if system != "":
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages") or []:
        if not is_record(raw):
            continue
        content = raw.get("content")
        if raw.get("role") == "assistant":
            tool_calls = []
            for block in content if isinstance(content, list) else []:
                if not is_record(block) or block.get("type") != "tool_use":
                    continue
                tool_calls.append({
                    "id": str_of(block.get("id")),
                    "type": "function",
                    "function": {"name": str_of(block.get("name")),
                                 "arguments": json.dumps(block.get("input") if is_record(block.get("input")) else {})},
                })
            text = text_of(content)
            entry = {"role": "assistant", "content": text if text != "" else None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            messages.append(entry)
            continue
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        if isinstance(content, list):
            parts = []
            for block in content:
                if not is_record(block):
                    continue
                if block.get("type") == "tool_result":
                    messages.append({"role": "tool",
                                     "tool_call_id": str_of(block.get("tool_use_id")),
                                     "content": _tool_result_text(block.get("content"))})
                    continue
                part = _image_block_to_openai(block)
                if part is not None:
                    parts.append(part)
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append({"type": "text", "text": block["text"]})
            if parts:
                messages.append({"role": "user", "content": parts})
    out = {"model": model, "messages": messages}
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        out["stop"] = [s for s in stops if isinstance(s, str)]
    tools = []
    for raw in body.get("tools") or []:
        if not is_record(raw) or str_of(raw.get("name")) == "":
            continue
        tools.append({"type": "function", "function": {
            "name": raw["name"],
            "description": str_of(raw.get("description")),
            "parameters": raw.get("input_schema") if is_record(raw.get("input_schema"))
                          else {"type": "object", "properties": {}},
        }})
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


def _gemini_image_part(url):
    if not isinstance(url, str) or url == "":
        return None
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        return {"inlineData": {"mimeType": mime, "data": data}}
    return {"fileData": {"fileUri": url, "mimeType": "image/png"}}


def _to_gemini_parts_openai(content):
    parts = []
    if isinstance(content, str):
        if content != "":
            parts.append({"text": content})
        return parts
    if isinstance(content, list):
        for block in content:
            if not is_record(block):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"text": block["text"]})
            elif block.get("type") == "image_url":
                image_url = block.get("image_url") if is_record(block.get("image_url")) else {}
                part = _gemini_image_part(str_of(image_url.get("url")))
                if part is not None:
                    parts.append(part)
    return parts


def _to_gemini_parts_anthropic(content):
    parts = []
    if isinstance(content, str):
        if content != "":
            parts.append({"text": content})
        return parts
    if isinstance(content, list):
        for block in content:
            if not is_record(block):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"text": block["text"]})
            elif block.get("type") == "image":
                source = block.get("source") if is_record(block.get("source")) else {}
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    parts.append({"inlineData": {"mimeType": str_of(source.get("media_type")) or "image/png",
                                                 "data": source["data"]}})
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    parts.append({"fileData": {"fileUri": source["url"], "mimeType": "image/png"}})
    return parts


def to_gemini(body, client_api, model):
    """An OpenAI or Anthropic request as a Gemini generateContent body.

    `model` is carried in the URL (:generateContent), not the body.
    """
    system_texts = []
    contents = []
    tools = []
    if client_api == "openai":
        for raw in body.get("messages") or []:
            if not is_record(raw):
                continue
            if raw.get("role") == "system":
                text = text_of(raw.get("content"))
                if text != "":
                    system_texts.append(text)
                continue
            if raw.get("role") == "assistant":
                parts = _to_gemini_parts_openai(raw.get("content"))
                for call in raw.get("tool_calls") or []:
                    if is_record(call) and is_record(call.get("function")):
                        fn = call["function"]
                        parts.append({"functionCall": {"name": str_of(fn.get("name")),
                                                       "args": _args_object(fn.get("arguments"))}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if raw.get("role") == "tool":
                contents.append({"role": "user", "parts": [{
                    "functionResponse": {"name": "",
                                         "response": {"content": _tool_result_text(raw.get("content"))}}}]})
                continue
            parts = _to_gemini_parts_openai(raw.get("content"))
            if parts:
                contents.append({"role": "user", "parts": parts})
        for raw in body.get("tools") or []:
            if is_record(raw) and is_record(raw.get("function")) and str_of(raw["function"].get("name")):
                fn = raw["function"]
                tools.append({"name": fn["name"], "description": str_of(fn.get("description")),
                              "parameters": fn.get("parameters") if is_record(fn.get("parameters")) else {}})
        stops = body.get("stop")
        if isinstance(stops, str):
            stops = [stops]
        max_tokens = body.get("max_tokens") if isinstance(body.get("max_tokens"), (int, float)) \
            else body.get("max_completion_tokens")
        temperature = body.get("temperature")
        top_p = body.get("top_p")
        fmt = body.get("response_format") if is_record(body.get("response_format")) else {}
        json_mode = fmt.get("type") in ("json_object", "json_schema")
    else:
        system = text_of(body.get("system"))
        if system != "":
            system_texts.append(system)
        for raw in body.get("messages") or []:
            if not is_record(raw):
                continue
            content = raw.get("content")
            if raw.get("role") == "assistant":
                parts = _to_gemini_parts_anthropic(content)
                for block in content if isinstance(content, list) else []:
                    if is_record(block) and block.get("type") == "tool_use":
                        parts.append({"functionCall": {"name": str_of(block.get("name")),
                                                       "args": block.get("input") if is_record(block.get("input")) else {}}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if isinstance(content, list):
                tool_results = [b for b in content if is_record(b) and b.get("type") == "tool_result"]
                parts = _to_gemini_parts_anthropic(content)
                if tool_results:
                    for block in tool_results:
                        parts.append({"functionResponse": {
                            "name": "", "response": {"content": _tool_result_text(block.get("content"))}}})
                if parts:
                    contents.append({"role": "user", "parts": parts})
            elif isinstance(content, str):
                parts = _to_gemini_parts_anthropic(content)
                if parts:
                    contents.append({"role": "user", "parts": parts})
        for raw in body.get("tools") or []:
            if is_record(raw) and str_of(raw.get("name")):
                tools.append({"name": raw["name"], "description": str_of(raw.get("description")),
                              "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {}})
        stops = body.get("stop_sequences")
        max_tokens = body.get("max_tokens")
        temperature = body.get("temperature")
        top_p = body.get("top_p")
        json_mode = False
    out = {"contents": contents}
    if system_texts:
        out["systemInstruction"] = {"parts": [{"text": "\n".join(system_texts)}]}
    if tools:
        out["tools"] = [{"functionDeclarations": tools}]
    config = {}
    if isinstance(max_tokens, (int, float)):
        config["maxOutputTokens"] = max_tokens
    if isinstance(temperature, (int, float)):
        config["temperature"] = temperature
    if isinstance(top_p, (int, float)):
        config["topP"] = top_p
    if isinstance(stops, list) and stops:
        config["stopSequences"] = [s for s in stops if isinstance(s, str)]
    if json_mode:
        config["responseMimeType"] = "application/json"
    if config:
        out["generationConfig"] = config
    return out


# --- responses: provider wire -> normalized answer ------------------------

def _openai_message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    return text_of(content)


def _openai_tool_calls(message):
    calls = []
    for call in message.get("tool_calls") or []:
        if not is_record(call) or not is_record(call.get("function")):
            continue
        calls.append({"id": str_of(call.get("id")) or "call_unknown",
                      "name": str_of(call["function"].get("name")),
                      "arguments": _args_object(call["function"].get("arguments"))})
    return calls


def parse_openai_json(body):
    """A Chat Completions JSON answer -> {text, tool_calls, usage, stop}."""
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    choice = choices[0] if choices and is_record(choices[0]) else {}
    message = choice.get("message") if is_record(choice.get("message")) else {}
    usage = body.get("usage") if is_record(body.get("usage")) else {}
    return {"text": _openai_message_text(message),
            "tool_calls": _openai_tool_calls(message),
            "usage": {"input": num_or_none(usage.get("prompt_tokens")) or 0,
                      "output": num_or_none(usage.get("completion_tokens")) or 0},
            "stop": str_of(choice.get("finish_reason")) or "stop"}


def parse_anthropic_json(body):
    """A Messages JSON answer -> {text, tool_calls, usage, stop}."""
    content = body.get("content") if isinstance(body.get("content"), list) else []
    text_parts = []
    calls = []
    for block in content:
        if not is_record(block):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append({"id": str_of(block.get("id")) or "toolu_unknown",
                          "name": str_of(block.get("name")),
                          "arguments": block.get("input") if is_record(block.get("input")) else {}})
    usage = body.get("usage") if is_record(body.get("usage")) else {}
    return {"text": "".join(text_parts), "tool_calls": calls,
            "usage": {"input": num_or_none(usage.get("input_tokens")) or 0,
                      "output": num_or_none(usage.get("output_tokens")) or 0},
            "stop": str_of(body.get("stop_reason")) or "end_turn"}


def parse_gemini_json(body):
    """A generateContent JSON answer -> {text, tool_calls, usage, stop}."""
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    cand = candidates[0] if candidates and is_record(candidates[0]) else {}
    content = cand.get("content") if is_record(cand.get("content")) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    texts = []
    calls = []
    for part in parts:
        if not is_record(part):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif is_record(part.get("functionCall")):
            fn = part["functionCall"]
            calls.append({"id": "call_gemini",
                          "name": str_of(fn.get("name")),
                          "arguments": fn.get("args") if is_record(fn.get("args")) else {}})
    usage = body.get("usageMetadata") if is_record(body.get("usageMetadata")) else {}
    return {"text": "".join(texts), "tool_calls": calls,
            "usage": {"input": num_or_none(usage.get("promptTokenCount")) or 0,
                      "output": num_or_none(usage.get("candidatesTokenCount")) or 0},
            "stop": str_of(cand.get("finishReason")) or "STOP"}


def parse_sse_frames(text):
    """SSE text -> [(event, data-string)] in order."""
    frames = []
    normalized = text.replace("\r\n", "\n")
    for frame in normalized.split("\n\n"):
        if frame.strip() == "":
            continue
        event = None
        data = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            frames.append((event, "\n".join(data)))
    return frames


def parse_openai_stream(text):
    """An OpenAI SSE stream -> normalized answer (text, usage, stop)."""
    pieces = []
    usage = {"input": 0, "output": 0}
    stop = "stop"
    for _event, data in parse_sse_frames(text):
        if data.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not is_record(chunk):
            continue
        if is_record(chunk.get("usage")):
            usage = {"input": num_or_none(chunk["usage"].get("prompt_tokens")) or 0,
                     "output": num_or_none(chunk["usage"].get("completion_tokens")) or 0}
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        if not choices or not is_record(choices[0]):
            continue
        choice = choices[0]
        delta = choice.get("delta") if is_record(choice.get("delta")) else {}
        if isinstance(delta.get("content"), str):
            pieces.append(delta["content"])
        if isinstance(choice.get("finish_reason"), str):
            stop = choice["finish_reason"]
    return {"text": "".join(pieces), "tool_calls": [], "usage": usage, "stop": stop}


def parse_anthropic_stream(text):
    """An Anthropic SSE stream -> normalized answer (text, usage, stop)."""
    pieces = []
    usage = {"input": 0, "output": 0}
    stop = "end_turn"
    for event, data in parse_sse_frames(text):
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not is_record(payload):
            continue
        if event == "message_start":
            message = payload.get("message") if is_record(payload.get("message")) else {}
            inner = message.get("usage") if is_record(message.get("usage")) else {}
            usage["input"] = num_or_none(inner.get("input_tokens")) or 0
        elif event == "content_block_delta":
            delta = payload.get("delta") if is_record(payload.get("delta")) else {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                pieces.append(delta["text"])
        elif event == "message_delta":
            delta = payload.get("delta") if is_record(payload.get("delta")) else {}
            if isinstance(delta.get("stop_reason"), str):
                stop = delta["stop_reason"]
            inner = payload.get("usage") if is_record(payload.get("usage")) else {}
            usage["output"] = num_or_none(inner.get("output_tokens")) or 0
    return {"text": "".join(pieces), "tool_calls": [], "usage": usage, "stop": stop}


def parse_gemini_stream(text):
    """A Gemini :streamGenerateContent SSE stream -> normalized answer."""
    pieces = []
    usage = {"input": 0, "output": 0}
    stop = "STOP"
    for _event, data in parse_sse_frames(text):
        try:
            frame = json.loads(data)
        except ValueError:
            continue
        if not is_record(frame):
            continue
        parsed = parse_gemini_json(frame)
        pieces.append(parsed["text"])
        if parsed["usage"]["input"] or parsed["usage"]["output"]:
            usage = parsed["usage"]
        candidates = frame.get("candidates") if isinstance(frame.get("candidates"), list) else []
        if candidates and is_record(candidates[0]):
            reason = str_of(candidates[0].get("finishReason"))
            if reason:
                stop = reason
    return {"text": "".join(pieces), "tool_calls": [], "usage": usage, "stop": stop}


# --- responses: normalized answer -> client wire --------------------------

def render_openai_json(answer, model):
    message = {"role": "assistant", "content": answer["text"]}
    if answer["tool_calls"]:
        message["tool_calls"] = [{
            "id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
        } for c in answer["tool_calls"]]
    stop = answer["stop"]
    if stop in ("end_turn", "STOP"):
        stop = "stop"
    elif stop == "max_tokens":
        stop = "length"
    elif stop == "tool_use":
        stop = "tool_calls"
    elif stop == "refusal":
        stop = "content_filter"
    usage = answer["usage"]
    return {"id": "chatcmpl-sim", "object": "chat.completion", "created": FIXED_CREATED,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": stop, "logprobs": None}],
            "usage": {"prompt_tokens": usage["input"], "completion_tokens": usage["output"],
                      "total_tokens": usage["input"] + usage["output"]}}


def render_anthropic_json(answer, model):
    content = []
    if answer["text"] != "":
        content.append({"type": "text", "text": answer["text"]})
    for call in answer["tool_calls"]:
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                        "input": call["arguments"]})
    if not content:
        content.append({"type": "text", "text": ""})
    stop = answer["stop"]
    if stop in ("stop", "STOP"):
        stop = "end_turn"
    elif stop == "length":
        stop = "max_tokens"
    elif stop in ("tool_calls", "function_call"):
        stop = "tool_use"
    elif stop == "content_filter":
        stop = "refusal"
    usage = answer["usage"]
    return {"id": "msg_aggregate", "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": usage["input"], "output_tokens": usage["output"]}}


def _sse_event(event, data):
    text = ("event: %s\n" % event) if event is not None else ""
    return text + "data: %s\n\n" % json.dumps(data, separators=(",", ":"))


def render_openai_stream(answer, model):
    """Normalized answer as OpenAI SSE: role chunk, text, finish, usage, [DONE]."""
    stop = answer["stop"]
    if stop in ("end_turn", "STOP"):
        stop = "stop"
    elif stop == "max_tokens":
        stop = "length"
    elif stop == "tool_use":
        stop = "tool_calls"
    elif stop == "refusal":
        stop = "content_filter"
    usage = answer["usage"]
    out = []
    chunk = lambda delta, finish: {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                                   "created": FIXED_CREATED, "model": model,
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    out.append(_sse_event(None, chunk({"role": "assistant", "content": ""}, None)))
    for piece in split_text(answer["text"]):
        out.append(_sse_event(None, chunk({"content": piece}, None)))
    out.append(_sse_event(None, chunk({}, stop)))
    out.append(_sse_event(None, {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                                 "created": FIXED_CREATED, "model": model, "choices": [],
                                 "usage": {"prompt_tokens": usage["input"],
                                           "completion_tokens": usage["output"],
                                           "total_tokens": usage["input"] + usage["output"]}}))
    out.append("data: [DONE]\n\n")
    return "".join(out)


def render_anthropic_stream(answer, model):
    """Normalized answer as Anthropic SSE in the required event order."""
    stop = answer["stop"]
    if stop in ("stop", "STOP"):
        stop = "end_turn"
    elif stop == "length":
        stop = "max_tokens"
    elif stop in ("tool_calls", "function_call"):
        stop = "tool_use"
    elif stop == "content_filter":
        stop = "refusal"
    usage = answer["usage"]
    out = []
    out.append(_sse_event("message_start", {"type": "message_start", "message": {
        "id": "msg_stream", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": usage["input"], "output_tokens": 0}}}))
    out.append(_sse_event("content_block_start", {"type": "content_block_start", "index": 0,
                                                  "content_block": {"type": "text", "text": ""}}))
    for piece in split_text(answer["text"]):
        out.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                      "delta": {"type": "text_delta", "text": piece}}))
    out.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    out.append(_sse_event("message_delta", {"type": "message_delta",
                                            "delta": {"stop_reason": stop, "stop_sequence": None},
                                            "usage": {"output_tokens": usage["output"]}}))
    out.append(_sse_event("message_stop", {"type": "message_stop"}))
    return "".join(out)


# --------------------------------------------------------------------------
# upstream calls
# --------------------------------------------------------------------------

def http_json(url, body, headers, timeout=PROVIDER_TIMEOUT_S):
    """POST JSON; returns (status, headers-dict, parsed-json-or-None, raw-text)."""
    data = json.dumps(body).encode("utf-8")
    req_headers = {"content-type": "application/json"}
    req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            parsed = None
            try:
                parsed = json.loads(raw)
            except ValueError:
                pass
            return resp.status, dict(resp.headers), parsed, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        parsed = None
        try:
            parsed = json.loads(raw)
        except ValueError:
            pass
        return exc.code, dict(exc.headers), parsed, raw
    except Exception as exc:  # noqa: BLE001 - unreachable provider is a refusal
        return 0, {}, None, str(exc)


def http_get_json(url, headers, timeout=PROBE_TIMEOUT_S):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001
        return 0, None


class UpstreamCall:
    """One attempt against one provider: normalized answer or a refusal."""

    def __init__(self, provider, model, client_api, body):
        self.provider = provider
        self.model = model
        self.ok = False
        self.status = 0
        self.retry_after = None
        self.answer = None       # normalized answer dict
        self.raw_text = None     # raw SSE bytes for same-wire passthrough
        self.stream = body.get("stream") is True
        base = join_base(ENV_URL, provider["base"])
        headers = auth_headers(provider["auth"], provider["key"])
        wire = provider["wire"]
        if wire == "openai":
            if client_api == "anthropic":
                # An Anthropic client body must be rewritten: its system
                # prompt, tools and stop_sequences live in fields the
                # OpenAI parser never reads, so pass it through unchanged
                # and the provider echoes no system, no tools, no stops.
                upstream_body = anthropic_to_openai(body, model)
            else:
                upstream_body = dict(body)
                upstream_body["model"] = model
            if self.stream:
                upstream_body["stream_options"] = {"include_usage": True}
            status, resp_headers, parsed, raw = http_json(
                base + "/v1/chat/completions", upstream_body, headers)
            self.status = status
            self.retry_after = resp_headers.get("retry-after") or resp_headers.get("Retry-After")
            if status == 200 and parsed is not None and not self.stream:
                self.ok = True
                self.answer = parse_openai_json(parsed)
            elif status == 200 and self.stream:
                self.ok = True
                self.raw_text = raw
                self.answer = parse_openai_stream(raw)
        elif wire == "anthropic":
            req_headers = dict(headers)
            req_headers["anthropic-version"] = ANTHROPIC_VERSION
            if client_api == "anthropic":
                upstream_body = dict(body)
                upstream_body["model"] = model
            else:
                upstream_body = openai_to_anthropic(body, model)
            status, resp_headers, parsed, raw = http_json(
                base + "/v1/messages", upstream_body, req_headers)
            self.status = status
            self.retry_after = resp_headers.get("retry-after") or resp_headers.get("Retry-After")
            if status == 200 and parsed is not None and not self.stream:
                self.ok = True
                self.answer = parse_anthropic_json(parsed)
            elif status == 200 and self.stream:
                self.ok = True
                self.raw_text = raw
                self.answer = parse_anthropic_stream(raw)
        else:  # gemini
            upstream_body = to_gemini(body, client_api, model)
            action = "streamGenerateContent" if self.stream else "generateContent"
            url = "%s/v1beta/models/%s:%s" % (base, model, action)
            if self.stream:
                url += "?alt=sse"
            status, resp_headers, parsed, raw = http_json(url, upstream_body, headers)
            self.status = status
            self.retry_after = resp_headers.get("retry-after") or resp_headers.get("Retry-After")
            if status == 200 and parsed is not None and not self.stream:
                self.ok = True
                self.answer = parse_gemini_json(parsed)
            elif status == 200 and self.stream:
                self.ok = True
                self.raw_text = raw
                self.answer = parse_gemini_stream(raw)


# --------------------------------------------------------------------------
# adaptive router
# --------------------------------------------------------------------------

class Health:
    """What the trial has taught us about one provider, so far."""

    def __init__(self):
        self.calls = 0
        self.successes = 0
        self.consec_refusals = 0
        self.cooldown_until = -1  # request index before which it is skipped

    def note_success(self):
        self.successes += 1
        self.consec_refusals = 0

    def note_refusal(self, req_index):
        self.consec_refusals += 1
        if self.consec_refusals == 1:
            self.cooldown_until = req_index + 1
        elif self.consec_refusals == 2:
            self.cooldown_until = req_index + 3
        else:
            self.cooldown_until = 1 << 30


class Router:
    def __init__(self, brief, probes):
        self.brief = brief
        self.candidates = []  # [{provider, model, rating, caps}]
        self.health = {}
        for probe in probes:
            if not probe["ok"]:
                continue
            provider = probe["provider"]
            self.health[provider["id"]] = Health()
            for model in probe["models"]:
                row = brief.rating_row(provider["id"], model["id"])
                if not model_free(model, row):
                    continue
                caps = capabilities_of(model, row)
                rating = row["rating_milli"] if row is not None else 0
                self.candidates.append({"provider": provider, "model": model["id"],
                                        "rating": rating, "caps": caps})

    def _json_mode_client(self, client_api, body):
        if client_api != "openai":
            return False
        fmt = body.get("response_format")
        return is_record(fmt) and fmt.get("type") in ("json_object", "json_schema")

    def order_for(self, req_index, requires, client_api, body):
        """Eligible candidates, best first, with cooled-down providers last.

        Order key: healthy before cooled-down, then rating, then fewest
        calls on that provider, then provider id, then model id.
        """
        json_mode = self._json_mode_client(client_api, body)
        eligible = []
        for cand in self.candidates:
            if not meets_requirements(requires, cand["caps"]):
                continue
            if json_mode and cand["provider"]["wire"] == "anthropic":
                continue  # its wire can never echo JSON mode
            eligible.append(cand)

        def key(cand):
            health = self.health[cand["provider"]["id"]]
            cooled = 1 if req_index < health.cooldown_until else 0
            return (cooled, -cand["rating"], health.calls,
                    cand["provider"]["id"], cand["model"])

        return sorted(eligible, key=key)

    def serve(self, req_index, requires, client_api, body):
        """Try candidates in order until one answers; returns UpstreamCall or None."""
        ordered = self.order_for(req_index, requires, client_api, body)
        for cand in ordered:
            provider = cand["provider"]
            health = self.health[provider["id"]]
            call = UpstreamCall(provider, cand["model"], client_api, body)
            health.calls += 1
            if call.ok:
                health.note_success()
                log("req %d: %s/%s answered (rating %d, calls %d)" % (
                    req_index, provider["id"], cand["model"], cand["rating"], health.calls))
                return call
            health.note_refusal(req_index)
            log("req %d: %s/%s refused HTTP %s (consec %d, cooldown until req %d)" % (
                req_index, provider["id"], cand["model"], call.status,
                health.consec_refusals, health.cooldown_until))
        return None


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

BRIEF = Brief(PROMPT_FILE)
if BRIEF.error is not None:
    log(BRIEF.error)
PROBES = []
CATALOG = {"providers": []}
ROUTER = None
MANIFEST_INDEX = 0
LOCK = threading.Lock()


def load():
    global PROBES, CATALOG, ROUTER
    if ENV_URL:
        PROBES = [probe_provider(ENV_URL, p) for p in BRIEF.providers]
        for probe in PROBES:
            if not probe["ok"] and probe["error"]:
                log(probe["error"])
    CATALOG = build_catalog(BRIEF, PROBES)
    ROUTER = Router(BRIEF, PROBES)


def served_model_ids():
    """Free model ids, deduplicated and sorted, for GET /v1/models."""
    ids = set()
    for probe in PROBES:
        if not probe["ok"]:
            continue
        for model in probe["models"]:
            row = BRIEF.rating_row(probe["provider"]["id"], model["id"])
            if model_free(model, row):
                ids.add(model["id"])
    out = []
    for mid in sorted(ids):
        provider_id = None
        display = mid
        for probe in PROBES:
            if not probe["ok"]:
                continue
            for model in probe["models"]:
                if model["id"] == mid:
                    provider_id = probe["provider"]["id"]
                    display = model["display_name"]
                    break
            if provider_id is not None:
                break
        out.append({"id": mid, "provider": provider_id or "", "display_name": display})
    return out


def requirements_from_body(body, client_api):
    requires = {}
    if client_api == "openai":
        if isinstance(body.get("tools"), list) and body["tools"]:
            requires["tools"] = True
        for message in body.get("messages") or []:
            if is_record(message) and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if is_record(block) and block.get("type") == "image_url":
                        requires["vision"] = True
        fmt = body.get("response_format")
        if is_record(fmt) and fmt.get("type") in ("json_object", "json_schema"):
            requires["json"] = True
    else:
        if isinstance(body.get("tools"), list) and body["tools"]:
            requires["tools"] = True
        for message in body.get("messages") or []:
            if is_record(message) and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if is_record(block) and block.get("type") == "image":
                        requires["vision"] = True
        # an Anthropic request carries no JSON-mode flag; the manifest decides
    return requires


def requirements_for(body, client_api):
    global MANIFEST_INDEX
    manifest = BRIEF.manifest
    entry = manifest[MANIFEST_INDEX] if MANIFEST_INDEX < len(manifest) else None
    MANIFEST_INDEX += 1
    if entry is not None:
        return entry["requires"]
    return requirements_from_body(body, client_api)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout/stderr clean of access logs
        pass

    def _send_json(self, status, body):
        text = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    def _send_text(self, status, text, content_type):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, wire, status, err_type, message):
        if wire == "anthropic":
            self._send_json(status, {"type": "error", "error": {"type": err_type, "message": message}})
        else:
            self._send_json(status, {"error": {"message": message, "type": err_type, "code": None}})

    def _read_json(self):
        length = self.headers.get("Content-Length")
        try:
            size = int(length) if length else 0
        except ValueError:
            return None
        if size <= 0 or size > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(size)
        try:
            value = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return value if is_record(value) else None

    def do_GET(self):
        with LOCK:
            self._do_get()

    def _do_get(self):
        if self.path.split("?")[0] != "/v1/models":
            self._send_error("openai", 404, "not_found", "no route GET %s" % self.path)
            return
        models = served_model_ids()
        if isinstance(self.headers.get("x-api-key"), str):
            self._send_json(200, {
                "data": [{"type": "model", "id": m["id"], "display_name": m["display_name"],
                          "created_at": FIXED_CREATED_AT} for m in models],
                "has_more": False})
        else:
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m["id"], "object": "model", "created": FIXED_CREATED,
                          "owned_by": m["provider"]} for m in models]})

    def do_POST(self):
        with LOCK:
            self._do_post()

    def _do_post(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            client_api = "openai"
        elif path == "/v1/messages":
            client_api = "anthropic"
        else:
            self._send_error("openai", 404, "not_found", "no route POST %s" % path)
            return
        body = self._read_json()
        if body is None:
            self._send_error(client_api, 400, "invalid_request_error",
                             "the request body is not one JSON object")
            return
        stream = body.get("stream") is True
        requires = requirements_for(body, client_api)
        call = ROUTER.serve(MANIFEST_INDEX - 1, requires, client_api, body)
        if call is None:
            self._send_error(client_api, 503,
                             "overloaded_error" if client_api == "anthropic" else "no_free_model",
                             "no free model on the directory meets this request")
            return
        model = call.model
        if stream:
            # Same-wire streams pass through byte for byte; cross-wire
            # streams are re-rendered from the normalized answer.
            if client_api == "openai" and call.provider["wire"] == "openai":
                self._send_text(200, call.raw_text, "text/event-stream; charset=utf-8")
            elif client_api == "anthropic" and call.provider["wire"] == "anthropic":
                self._send_text(200, call.raw_text, "text/event-stream; charset=utf-8")
            elif client_api == "openai":
                self._send_text(200, render_openai_stream(call.answer, model),
                                "text/event-stream; charset=utf-8")
            else:
                self._send_text(200, render_anthropic_stream(call.answer, model),
                                "text/event-stream; charset=utf-8")
            return
        if client_api == "openai":
            self._send_json(200, render_openai_json(call.answer, model))
        else:
            self._send_json(200, render_anthropic_json(call.answer, model))


def register(port):
    for attempt in range(5):
        try:
            status, _headers, _parsed, raw = http_json(
                ENV_URL + "/register",
                {"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": CATALOG},
                {}, timeout=5.0)
            if status == 200:
                log("registered: %s" % raw.strip()[:200])
                return
            log("register attempt %d answered HTTP %s" % (attempt + 1, status))
        except Exception as exc:  # noqa: BLE001
            log("register attempt %d failed: %s" % (attempt + 1, exc))
        time.sleep(0.5)
    raise RuntimeError("POST %s/register failed" % ENV_URL)


def poll_until_done(server):
    in_flight = False
    while True:
        if in_flight:
            time.sleep(POLL_INTERVAL_S)
            continue
        in_flight = True
        try:
            status, payload = http_get_json(ENV_URL + "/work", {}, timeout=2.0)
            if status == 200 and is_record(payload) and payload.get("done") is True:
                server.shutdown()
                return
        except Exception:  # noqa: BLE001 - the env is not reachable yet
            pass
        finally:
            in_flight = False
        time.sleep(POLL_INTERVAL_S)


def main():
    load()
    if "--list" in sys.argv:
        text = json.dumps(CATALOG, separators=(",", ":")) + "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0
    if not ENV_URL:
        log("MAKEFUN_ENV_URL is not set")
        return 1
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        raise RuntimeError("the OS picked 8080, which the env owns: restart the trial")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log("listening on 0.0.0.0:%d with %d providers" % (port, len(CATALOG["providers"])))
    register(port)
    try:
        poll_until_done(server)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
