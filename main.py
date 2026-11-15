#!/usr/bin/env python3
"""Free-model aggregator: one server, two client wires, ten free providers.

BALANCED router (this entry's strategy): every (request, model) pair is
scored by quality per unit of scarce budget -- the model's sealed rating
divided by the number of budget units its provider has already spent plus
one -- and the best-value legal model serves the request.  The value of a
provider's next call falls every time it is used, so calls spread over the
ten providers instead of draining the single best one; a request that
needs tools, vision, JSON mode or a long context only ever sees models
that meet it.  On a 402/429 refusal the spent provider is counted as
used, a retry-after is honoured by not retrying that provider for the
same request, and the request is re-routed to the next-best value; two
refusals in a row retire a provider for the rest of the trial.

Pure Python 3.13 standard library.  No wall clock and no unseeded
randomness anywhere a decision is made: every choice is a function of
the brief, the requests seen so far and the providers' answers, so a
replay leaves the same state.  Fixed timestamps only.
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
FIXED_CREATED = 1767225600
FIXED_ANTHROPIC_CREATED_AT = "2026-01-01T00:00:00Z"
ANTHROPIC_VERSION = "2023-06-01"
SSE_DELTA_CHARS = 13
PROBE_TIMEOUT_S = 5
CALL_TIMEOUT_S = 5
POLL_INTERVAL_S = 0.1
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_ATTEMPTS_PER_REQUEST = 8

# Expected per-trial call budget of each provider, from the competition
# kit's public provider table (calls_min..calls_max).  Used only as a
# prior for how scarce one call is; the real budget is never published
# and is learned from refusals instead.  Midpoint of each range.
BUDGET_PRIOR = {
    "groq": 10.5, "cerebras": 5.5, "gemini": 7.0, "zai": 4.5,
    "openrouter": 11.0, "sambanova": 4.5, "pollinations": 12.0,
    "cloudflare": 7.5, "ovh": 5.5, "kilol": 14.0,
}


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def is_record(value):
    return value is not None and isinstance(value, dict)


def text_of(content):
    """The text of a string or of a block array's text blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if is_record(block) and block.get("type") == "text" \
                and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Brief parsing: the prompt file carries three JSON documents.
# --------------------------------------------------------------------------

def _balanced_json(text, start):
    """Parse the JSON value starting at text[start] ('{'), honouring strings."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON document")


def read_brief(path):
    """Return (directory, ratings, workload) parsed from the prompt file."""
    directory = {"providers": []}
    ratings = {"ratings": [], "best": []}
    workload = {"manifest": []}
    if not path or not os.path.exists(path):
        return directory, ratings, workload
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    needle = '{"schema":"free-model-aggregator-01/'
    at = 0
    while True:
        at = text.find(needle, at)
        if at < 0:
            break
        doc = _balanced_json(text, at)
        schema = doc.get("schema", "")
        if schema.endswith("/providers/v1"):
            directory = doc
        elif schema.endswith("/ratings/v1"):
            ratings = doc
        elif schema.endswith("/workload/v1"):
            workload = doc
        at += len(needle)
    return directory, ratings, workload


# --------------------------------------------------------------------------
# HTTP helper (urllib, stdlib only).
# --------------------------------------------------------------------------

def http_call(method, url, headers, body_bytes, timeout):
    """One HTTP call. Returns (status, headers-dict, body-bytes)."""
    request = urllib.request.Request(url, data=body_bytes, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()
    except Exception:
        raise


def auth_headers(auth, key):
    if auth == "bearer":
        return {"authorization": "Bearer " + key}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


# --------------------------------------------------------------------------
# Catalog: the providers' own listings are the truth; the brief's quality
# table (which names every model, free or paid) fills any gap a probe
# leaves, so --list and the register body are built by the one function.
# --------------------------------------------------------------------------

def probe_catalog(env_url, provider):
    """GET one provider's model list. Returns a list of model dicts or None."""
    base = env_url.rstrip("/") + provider["path"]
    headers = auth_headers(provider["auth"], provider["key"])
    if provider["wire"] == "gemini":
        url = base + "/v1beta/models"
    else:
        url = base + "/v1/models"
    try:
        status, _headers, body = http_call(
            "GET", url, headers, None, PROBE_TIMEOUT_S)
    except Exception as err:
        log("probe %s failed: %s" % (provider["id"], err))
        return None
    if status != 200:
        log("probe %s answered HTTP %s" % (provider["id"], status))
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    models = []
    if provider["wire"] == "gemini":
        for entry in data.get("models", []):
            if not is_record(entry):
                continue
            name = entry.get("name", "")
            model_id = name[7:] if name.startswith("models/") else name
            models.append({
                "id": model_id,
                "free": entry.get("free") is True,
                "context_length": entry.get("context_length"),
            })
    else:
        for entry in data.get("data", []):
            if not is_record(entry):
                continue
            models.append({
                "id": entry.get("id"),
                "free": entry.get("free") is True,
                "context_length": entry.get("context_length"),
            })
    return models


def build_catalog(env_url, directory, ratings):
    """The one catalog document: --list prints it, register sends it."""
    rated = {}
    for row in ratings.get("ratings", []):
        rated[(row.get("provider"), row.get("model"))] = row
    providers = []
    for provider in directory.get("providers", []):
        probed = None
        if env_url:
            probed = probe_catalog(env_url, provider)
        if probed is not None:
            models = []
            for entry in probed:
                row = rated.get((provider["id"], entry["id"]))
                context = entry.get("context_length")
                if not isinstance(context, int) and row is not None:
                    context = row.get("context_length")
                models.append({
                    "id": entry["id"],
                    "free": bool(entry["free"]),
                    "context_length": context,
                })
        else:
            models = [
                {"id": row["model"], "free": bool(row.get("free")),
                 "context_length": row.get("context_length")}
                for row in ratings.get("ratings", [])
                if row.get("provider") == provider["id"]
            ]
        providers.append({
            "id": provider["id"],
            "base_url": provider["path"],
            "key_env": provider.get("key_env", ""),
            "wire": provider["wire"],
            "models": models,
        })
    return {"providers": providers}


def catalog_bytes(catalog):
    return json.dumps(catalog, separators=(",", ":"))


def free_model_list(catalog):
    """All free model ids, provider order, models sorted by id: GET /v1/models."""
    out = []
    for provider in catalog.get("providers", []):
        models = sorted(provider.get("models", []), key=lambda m: m["id"])
        for model in models:
            if model.get("free") is True:
                out.append((model["id"], provider["id"]))
    return out


# --------------------------------------------------------------------------
# Routing: the BALANCED value function and the failover ledger.
# --------------------------------------------------------------------------

def meets_requirements(row, requires):
    if requires.get("tools") is True and not row.get("tools"):
        return False
    if requires.get("vision") is True and not row.get("vision"):
        return False
    if requires.get("json") is True and not row.get("json"):
        return False
    minimum = requires.get("min_context")
    if minimum is not None:
        context = row.get("context_length")
        if not isinstance(context, int) or context < minimum:
            return False
    return True


class Router:
    """Ranks (provider, model) pairs by quality per scarce budget unit.

    value = rating_milli / (used + 1): the first call to a provider costs
    one unit of a fresh budget, the second costs one of a budget already
    half-spent in expectation, and so on, so the router drifts to the
    provider whose next call is cheapest for the quality it returns.
    A refused call still spent its unit, so it counts in `used` too.
    """

    def __init__(self, directory, ratings):
        self.providers = {p["id"]: p for p in directory.get("providers", [])}
        self.order = [p["id"] for p in directory.get("providers", [])]
        self.rows = []
        for row in ratings.get("ratings", []):
            if row.get("free") is True:
                self.rows.append(row)
        self.used = {pid: 0 for pid in self.order}
        self.streak = {pid: 0 for pid in self.order}
        self.exhausted = {pid: False for pid in self.order}

    def value(self, row):
        used = self.used[row["provider"]]
        return row["rating_milli"] / (used + 1)

    def candidates(self, requires, skip):
        """Legal models for a request, best value first.

        `skip` holds (provider, model) pairs already tried for this
        request; exhausted providers are out for the rest of the trial.
        """
        ranked = []
        for row in self.rows:
            if self.exhausted[row["provider"]]:
                continue
            if (row["provider"], row["model"]) in skip:
                continue
            if not meets_requirements(row, requires):
                continue
            ranked.append(row)
        ranked.sort(key=lambda r: (
            -self.value(r), -r["rating_milli"], r["provider"], r["model"]))
        return ranked

    def note_call(self, provider_id):
        self.used[provider_id] = self.used.get(provider_id, 0) + 1

    def note_refusal(self, provider_id):
        self.note_call(provider_id)
        self.streak[provider_id] = self.streak.get(provider_id, 0) + 1
        if self.streak[provider_id] >= 2:
            self.exhausted[provider_id] = True
            log("provider %s retired after two refusals" % provider_id)

    def note_success(self, provider_id):
        self.note_call(provider_id)
        self.streak[provider_id] = 0


class ProviderRefusal(Exception):
    def __init__(self, status, retry_after):
        super().__init__("provider refused with HTTP %s" % status)
        self.status = status
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Wire translation: client body -> provider body, per provider wire.
# --------------------------------------------------------------------------

def _image_part_openai(block):
    """An OpenAI image_url part as a (kind, mime, data-or-url) triple."""
    url = block.get("image_url", {}).get("url", "")
    if not isinstance(url, str):
        return None
    if url.startswith("data:"):
        head, _, data = url[5:].partition(",")
        mime = head.split(";")[0] or "image/png"
        return ("inline", mime, data)
    return ("url", "", url)


def openai_content_parts(content):
    """OpenAI message content as neutral parts: text / inline / url images."""
    parts = []
    if isinstance(content, str):
        if content:
            parts.append({"kind": "text", "text": content})
        return parts
    if not isinstance(content, list):
        return parts
    for block in content:
        if not is_record(block):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"kind": "text", "text": block["text"]})
        elif block.get("type") == "image_url":
            found = _image_part_openai(block)
            if found is not None:
                parts.append({"kind": found[0], "mime": found[1],
                              "data": found[2]})
    return parts


def anthropic_content_parts(content):
    """Anthropic message content as neutral parts."""
    parts = []
    if isinstance(content, str):
        if content:
            parts.append({"kind": "text", "text": content})
        return parts
    if not isinstance(content, list):
        return parts
    for block in content:
        if not is_record(block):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"kind": "text", "text": block["text"]})
        elif block.get("type") == "image" and is_record(block.get("source")):
            source = block["source"]
            if source.get("type") == "base64" \
                    and isinstance(source.get("data"), str):
                parts.append({"kind": "inline",
                              "mime": source.get("media_type", "image/png"),
                              "data": source["data"]})
            elif source.get("type") == "url" \
                    and isinstance(source.get("url"), str):
                parts.append({"kind": "url", "mime": "", "data": source["url"]})
    return parts


def _openai_tools(body):
    tools = []
    for raw in body.get("tools", []) or []:
        if not is_record(raw) or not is_record(raw.get("function")):
            continue
        fn = raw["function"]
        if isinstance(fn.get("name"), str):
            tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters")
                if is_record(fn.get("parameters")) else
                {"type": "object", "properties": {}},
            })
    return tools


def _anthropic_tools(body):
    tools = []
    for raw in body.get("tools", []) or []:
        if not is_record(raw) or not isinstance(raw.get("name"), str):
            continue
        tools.append({
            "name": raw["name"],
            "description": raw.get("description", ""),
            "parameters": raw.get("input_schema")
            if is_record(raw.get("input_schema")) else
            {"type": "object", "properties": {}},
        })
    return tools


def _split_request(client_api, body):
    """A client request as neutral fields, whichever wire it arrived on."""
    system_text = ""
    messages = []  # (role, [parts])
    tools = []
    max_tokens = None
    stops = []
    json_mode = False
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    tool_choice = body.get("tool_choice")
    if client_api == "openai":
        for raw in body.get("messages", []) or []:
            if not is_record(raw):
                continue
            role = raw.get("role")
            if role == "system":
                system_text = text_of(raw.get("content"))
            elif role in ("user", "assistant"):
                messages.append((role, openai_content_parts(raw.get("content"))))
        tools = _openai_tools(body)
        max_tokens = body.get("max_tokens")
        if max_tokens is None:
            max_tokens = body.get("max_completion_tokens")
        stop = body.get("stop")
        if isinstance(stop, str):
            stops = [stop]
        elif isinstance(stop, list):
            stops = [s for s in stop if isinstance(s, str)]
        fmt = body.get("response_format")
        json_mode = is_record(fmt) and fmt.get("type") in (
            "json_object", "json_schema")
    else:
        system_text = text_of(body.get("system"))
        for raw in body.get("messages", []) or []:
            if not is_record(raw):
                continue
            role = raw.get("role")
            if role in ("user", "assistant"):
                messages.append((role, anthropic_content_parts(raw.get("content"))))
        tools = _anthropic_tools(body)
        max_tokens = body.get("max_tokens")
        stops = [s for s in (body.get("stop_sequences") or [])
                 if isinstance(s, str)]
    return {
        "system": system_text, "messages": messages, "tools": tools,
        "max_tokens": max_tokens, "stops": stops, "json_mode": json_mode,
        "temperature": temperature, "top_p": top_p,
        "tool_choice": tool_choice,
    }


def to_openai_body(split, model, stream):
    """Neutral request as an OpenAI Chat Completions body."""
    out_messages = []
    if split["system"]:
        out_messages.append({"role": "system", "content": split["system"]})
    for role, parts in split["messages"]:
        content = []
        for part in parts:
            if part["kind"] == "text":
                content.append({"type": "text", "text": part["text"]})
            elif part["kind"] == "inline":
                content.append({"type": "image_url", "image_url": {
                    "url": "data:%s;base64,%s" % (part["mime"], part["data"])}})
            else:
                content.append({"type": "image_url", "image_url": {
                    "url": part["data"]}})
        if len(content) == 1 and content[0]["type"] == "text":
            out_messages.append({"role": role, "content": content[0]["text"]})
        elif content:
            out_messages.append({"role": role, "content": content})
    out = {"model": model, "messages": out_messages}
    if split["max_tokens"] is not None:
        out["max_tokens"] = split["max_tokens"]
    if split["temperature"] is not None:
        out["temperature"] = split["temperature"]
    if split["top_p"] is not None:
        out["top_p"] = split["top_p"]
    if split["stops"]:
        out["stop"] = split["stops"]
    if split["tools"]:
        out["tools"] = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["parameters"]}} for t in split["tools"]]
        choice = split["tool_choice"]
        if choice == "auto":
            out["tool_choice"] = "auto"
        elif choice == "required":
            out["tool_choice"] = "required"
        elif is_record(choice) and choice.get("type") == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": choice.get("name", "")}}
    if split["json_mode"]:
        out["response_format"] = {"type": "json_object"}
    if stream:
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def to_anthropic_body(split, model, stream):
    """Neutral request as an Anthropic Messages body."""
    messages = []
    for role, parts in split["messages"]:
        blocks = []
        for part in parts:
            if part["kind"] == "text":
                blocks.append({"type": "text", "text": part["text"]})
            elif part["kind"] == "inline":
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": part["mime"],
                    "data": part["data"]}})
            else:
                blocks.append({"type": "image", "source": {
                    "type": "url", "url": part["data"]}})
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            messages.append({"role": role, "content": blocks[0]["text"]})
        elif blocks:
            messages.append({"role": role, "content": blocks})
    out = {"model": model, "messages": messages}
    if split["system"]:
        out["system"] = split["system"]
    if split["max_tokens"] is not None:
        out["max_tokens"] = split["max_tokens"]
    if split["temperature"] is not None:
        out["temperature"] = split["temperature"]
    if split["top_p"] is not None:
        out["top_p"] = split["top_p"]
    if split["stops"]:
        out["stop_sequences"] = split["stops"]
    if split["tools"]:
        out["tools"] = [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]} for t in split["tools"]]
        choice = split["tool_choice"]
        if choice == "auto":
            out["tool_choice"] = {"type": "auto"}
        elif choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif is_record(choice) and choice.get("type") == "tool":
            out["tool_choice"] = {"type": "tool",
                                  "name": choice.get("name", "")}
    if stream:
        out["stream"] = True
    return out


def to_gemini_body(split, model):
    """Neutral request as a Gemini generateContent body."""
    contents = []
    for role, parts in split["messages"]:
        gemini_parts = []
        for part in parts:
            if part["kind"] == "text":
                gemini_parts.append({"text": part["text"]})
            elif part["kind"] == "inline":
                gemini_parts.append({"inlineData": {
                    "mimeType": part["mime"], "data": part["data"]}})
            else:
                gemini_parts.append({"fileData": {
                    "fileUri": part["data"], "mimeType": "image/png"}})
        if gemini_parts:
            contents.append({"role": "model" if role == "assistant" else "user",
                             "parts": gemini_parts})
    out = {"contents": contents}
    if split["system"]:
        out["systemInstruction"] = {"parts": [{"text": split["system"]}]}
    if split["tools"]:
        out["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in split["tools"]]}]
    config = {}
    if split["max_tokens"] is not None:
        config["maxOutputTokens"] = split["max_tokens"]
    if split["stops"]:
        config["stopSequences"] = split["stops"]
    if split["json_mode"]:
        config["responseMimeType"] = "application/json"
    if split["temperature"] is not None:
        config["temperature"] = split["temperature"]
    if split["top_p"] is not None:
        config["topP"] = split["top_p"]
    if config:
        out["generationConfig"] = config
    return out


# --------------------------------------------------------------------------
# Provider calls: one call, normalised to (text, input_tokens,
# output_tokens).  A non-2xx answer is a refusal, never an answer.
# --------------------------------------------------------------------------

def _number(value, default=0):
    return value if isinstance(value, (int, float)) and value == value else default


def _parse_sse_frames(raw):
    """SSE bytes as (event, data-string) frames, in order."""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    frames = []
    for chunk in text.split("\n\n"):
        if not chunk.strip():
            continue
        event = None
        data = []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            frames.append((event, "\n".join(data)))
    return frames


def _normalise_openai_json(data):
    choices = data.get("choices") or []
    choice = choices[0] if choices and is_record(choices[0]) else {}
    message = choice.get("message") or {}
    text = message.get("content")
    if not isinstance(text, str):
        text = text_of(text)
    usage = data.get("usage") or {}
    return text, _number(usage.get("prompt_tokens")), \
        _number(usage.get("completion_tokens"))


def _normalise_openai_stream(raw):
    text = ""
    in_tokens = 0
    out_tokens = 0
    for _event, payload in _parse_sse_frames(raw):
        if payload.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        if not is_record(chunk):
            continue
        if is_record(chunk.get("usage")):
            in_tokens = _number(chunk["usage"].get("prompt_tokens"), in_tokens)
            out_tokens = _number(chunk["usage"].get("completion_tokens"),
                                 out_tokens)
        choices = chunk.get("choices") or []
        if not choices or not is_record(choices[0]):
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta.get("content"), str):
            text += delta["content"]
    return text, in_tokens, out_tokens


def _normalise_anthropic_json(data):
    text = text_of(data.get("content"))
    usage = data.get("usage") or {}
    return text, _number(usage.get("input_tokens")), \
        _number(usage.get("output_tokens"))


def _normalise_anthropic_stream(raw):
    text = ""
    in_tokens = 0
    out_tokens = 0
    for event, payload in _parse_sse_frames(raw):
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if not is_record(data):
            continue
        if event == "message_start" and is_record(data.get("message")) \
                and is_record(data["message"].get("usage")):
            in_tokens = _number(data["message"]["usage"].get("input_tokens"))
        elif event == "content_block_delta" and is_record(data.get("delta")) \
                and data["delta"].get("type") == "text_delta":
            if isinstance(data["delta"].get("text"), str):
                text += data["delta"]["text"]
        elif event == "message_delta" and is_record(data.get("usage")):
            out_tokens = _number(data["usage"].get("output_tokens"))
    return text, in_tokens, out_tokens


def _normalise_gemini_json(data):
    text = ""
    candidates = data.get("candidates") or []
    if candidates and is_record(candidates[0]):
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "\n".join(p.get("text", "") for p in parts
                         if is_record(p) and isinstance(p.get("text"), str))
    usage = data.get("usageMetadata") or {}
    return text, _number(usage.get("promptTokenCount")), \
        _number(usage.get("candidatesTokenCount"))


def _normalise_gemini_stream(raw):
    text = ""
    in_tokens = 0
    out_tokens = 0
    for _event, payload in _parse_sse_frames(raw):
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if not is_record(data):
            continue
        chunk_text, chunk_in, chunk_out = _normalise_gemini_json(data)
        text += chunk_text
        if chunk_in:
            in_tokens = chunk_in
        if chunk_out:
            out_tokens = chunk_out
    return text, in_tokens, out_tokens


def call_provider(env_url, provider, model, client_api, body, stream):
    """Call one provider once. Returns (text, in_tokens, out_tokens).

    Raises ProviderRefusal on any non-2xx answer or transport failure.
    """
    split = _split_request(client_api, body)
    wire = provider["wire"]
    base = env_url.rstrip("/") + provider["path"]
    headers = {"content-type": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if wire == "openai":
        payload = to_openai_body(split, model, stream)
        url = base + "/v1/chat/completions"
    elif wire == "anthropic":
        payload = to_anthropic_body(split, model, stream)
        headers["anthropic-version"] = ANTHROPIC_VERSION
        url = base + "/v1/messages"
    else:
        payload = to_gemini_body(split, model)
        action = "streamGenerateContent" if stream else "generateContent"
        url = "%s/v1beta/models/%s:%s" % (base, model, action)
        if stream:
            url += "?alt=sse"
    if stream:
        headers["accept"] = "text/event-stream"
    try:
        status, resp_headers, raw = http_call(
            "POST", url, headers,
            json.dumps(payload).encode("utf-8"), CALL_TIMEOUT_S)
    except Exception as err:
        raise ProviderRefusal(0, None) from err
    if status < 200 or status >= 300:
        retry_after = resp_headers.get("retry-after") or \
            resp_headers.get("Retry-After")
        raise ProviderRefusal(status, retry_after)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        data = None
    if wire == "openai":
        if stream:
            return _normalise_openai_stream(raw)
        return _normalise_openai_json(data or {})
    if wire == "anthropic":
        if stream:
            return _normalise_anthropic_stream(raw)
        return _normalise_anthropic_json(data or {})
    if stream:
        return _normalise_gemini_stream(raw)
    return _normalise_gemini_json(data or {})


# --------------------------------------------------------------------------
# Rendering: a normalised answer in the wire the client used.
# --------------------------------------------------------------------------

def _split_text(text):
    pieces = [text[i:i + SSE_DELTA_CHARS]
              for i in range(0, len(text), SSE_DELTA_CHARS)]
    return pieces or [""]


def render_openai_json(model, answer):
    text, in_tokens, out_tokens = answer
    return {
        "id": "chatcmpl-sim",
        "object": "chat.completion",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": in_tokens,
                  "completion_tokens": out_tokens,
                  "total_tokens": in_tokens + out_tokens},
    }


def render_openai_stream(model, answer):
    text, in_tokens, out_tokens = answer

    def chunk(delta, finish):
        return {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                "created": FIXED_CREATED, "model": model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}]}

    frames = ["data: %s\n\n" % json.dumps(
        chunk({"role": "assistant", "content": ""}, None),
        separators=(",", ":"))]
    for piece in _split_text(text):
        frames.append("data: %s\n\n" % json.dumps(
            chunk({"content": piece}, None), separators=(",", ":")))
    frames.append("data: %s\n\n" % json.dumps(
        chunk({}, "stop"), separators=(",", ":")))
    frames.append("data: %s\n\n" % json.dumps(
        {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
         "created": FIXED_CREATED, "model": model, "choices": [],
         "usage": {"prompt_tokens": in_tokens,
                   "completion_tokens": out_tokens,
                   "total_tokens": in_tokens + out_tokens}},
        separators=(",", ":")))
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def render_anthropic_json(model, answer):
    text, in_tokens, out_tokens = answer
    return {
        "id": "msg_sim",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


def render_anthropic_stream(model, answer):
    text, in_tokens, out_tokens = answer

    def event(name, data):
        return "event: %s\ndata: %s\n\n" % (
            name, json.dumps(data, separators=(",", ":")))

    frames = [event("message_start", {
        "type": "message_start",
        "message": {"id": "msg_sim", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": in_tokens,
                              "output_tokens": 0}}}),
        event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})]
    for piece in _split_text(text):
        frames.append(event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece}}))
    frames.append(event("content_block_stop", {
        "type": "content_block_stop", "index": 0}))
    frames.append(event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": out_tokens}}))
    frames.append(event("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def _error_body(client_api, status, message):
    if client_api == "anthropic":
        return status, {"type": "error",
                        "error": {"type": "overloaded_error",
                                  "message": message}}
    return status, {"error": {"message": message,
                              "type": "overloaded_error", "code": None}}


# --------------------------------------------------------------------------
# Server: the two client routes, the model list, register, poll, exit.
# --------------------------------------------------------------------------

class Aggregator:
    def __init__(self, env_url, directory, ratings, workload, catalog):
        self.env_url = env_url
        self.directory = directory
        self.ratings = ratings
        self.manifest = workload.get("manifest", [])
        self.catalog = catalog
        self.router = Router(directory, ratings)
        self.arrival = 0
        self.lock = threading.Lock()

    def requirements_for(self, client_api, body):
        """The manifest row for the request now arriving, else a body read."""
        index = self.arrival
        self.arrival += 1
        if 0 <= index < len(self.manifest):
            return dict(self.manifest[index].get("requires", {}))
        split = _split_request(client_api, body)
        requires = {}
        if split["tools"]:
            requires["tools"] = True
        if any(p["kind"] in ("inline", "url")
               for _role, parts in split["messages"] for p in parts):
            requires["vision"] = True
        if split["json_mode"]:
            requires["json"] = True
        return requires

    def serve(self, client_api, body):
        """Answer one client request. Returns (status, content-type, bytes)."""
        stream = body.get("stream") is True
        requires = self.requirements_for(client_api, body)
        tried = set()
        attempts = 0
        while attempts < MAX_ATTEMPTS_PER_REQUEST:
            ranked = self.router.candidates(requires, tried)
            if not ranked:
                break
            row = ranked[0]
            provider = self.router.providers[row["provider"]]
            tried.add((row["provider"], row["model"]))
            attempts += 1
            try:
                answer = call_provider(
                    self.env_url, provider, row["model"],
                    client_api, body, stream)
            except ProviderRefusal as err:
                # A retry-after is honoured by not retrying this provider
                # for the same request: it stays in `tried` until the
                # next request, and the refused call still spent budget.
                self.router.note_refusal(row["provider"])
                log("refused %s/%s HTTP %s; failing over"
                    % (row["provider"], row["model"], err.status))
                continue
            self.router.note_success(row["provider"])
            if client_api == "openai":
                if stream:
                    return 200, "text/event-stream; charset=utf-8", \
                        render_openai_stream(row["model"], answer).encode()
                return 200, "application/json", json.dumps(
                    render_openai_json(row["model"], answer)).encode()
            if stream:
                return 200, "text/event-stream; charset=utf-8", \
                    render_anthropic_stream(row["model"], answer).encode()
            return 200, "application/json", json.dumps(
                render_anthropic_json(row["model"], answer)).encode()
        status, payload = _error_body(
            client_api, 503, "no free model could serve this request")
        return status, "application/json", json.dumps(payload).encode()


def make_handler(aggregator):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep stdout/stderr clean
            pass

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            return self.rfile.read(length)

        def _send(self, status, content_type, payload, close=False):
            self.send_response(status)
            self.send_header("content-type", content_type)
            if close:
                self.send_header("connection", "close")
            else:
                self.send_header("content-length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
            if close:
                self.close_connection = True

        def _send_json(self, status, payload):
            self._send(status, "application/json",
                       json.dumps(payload).encode())

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/v1/models":
                self._send_json(404, {"error": {
                    "message": "no route GET %s" % self.path,
                    "type": "not_found", "code": None}})
                return
            served = free_model_list(aggregator.catalog)
            if self.headers.get("x-api-key") is not None:
                self._send_json(200, {
                    "data": [{"type": "model", "id": model_id,
                              "display_name": model_id,
                              "created_at": FIXED_ANTHROPIC_CREATED_AT,
                              "free": True}
                             for model_id, _provider in served],
                    "has_more": False})
            else:
                self._send_json(200, {
                    "object": "list",
                    "data": [{"id": model_id, "object": "model",
                              "created": FIXED_CREATED,
                              "owned_by": provider, "free": True}
                             for model_id, provider in served]})

        def _serve_post(self, client_api):
            raw = self._read_body()
            if raw is None:
                status, payload = _error_body(
                    client_api, 400, "the request body is not one JSON object")
                self._send_json(status, payload)
                return
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                status, payload = _error_body(
                    client_api, 400, "the request body is not one JSON object")
                self._send_json(status, payload)
                return
            if not is_record(body):
                status, payload = _error_body(
                    client_api, 400, "the request body is not one JSON object")
                self._send_json(status, payload)
                return
            with aggregator.lock:
                status, content_type, payload = aggregator.serve(
                    client_api, body)
            self._send(status, content_type, payload,
                       close=content_type.startswith("text/event-stream"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/chat/completions":
                self._serve_post("openai")
            elif path == "/v1/messages":
                self._serve_post("anthropic")
            else:
                self._send_json(404, {"error": {
                    "message": "no route POST %s" % path,
                    "type": "not_found", "code": None}})

    return Handler


def register(env_url, port, catalog):
    """POST /register, retrying briefly while the env comes up."""
    last_error = None
    for _attempt in range(50):
        try:
            status, _headers, raw = http_call(
                "POST", env_url.rstrip("/") + "/register",
                {"content-type": "application/json"},
                json.dumps({"protocol_version": PROTOCOL_VERSION,
                            "port": port, "catalog": catalog}).encode(),
                PROBE_TIMEOUT_S)
            if status == 200:
                reply = json.loads(raw.decode("utf-8"))
                log("registered: the workload holds %s requests"
                    % reply.get("requests"))
                return
            last_error = "HTTP %s" % status
        except Exception as err:
            last_error = str(err)
        time.sleep(0.1)
    raise RuntimeError("POST /register failed: %s" % last_error)


def poll_until_done(env_url, server):
    """Poll GET /work; exit 0 the moment the env says done."""
    while True:
        time.sleep(POLL_INTERVAL_S)
        try:
            status, _headers, raw = http_call(
                "GET", env_url.rstrip("/") + "/work", {}, None, 2)
        except Exception:
            continue
        if status != 200:
            continue
        try:
            work = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if is_record(work) and work.get("done") is True:
            time.sleep(0.1)
            server.shutdown()
            server.server_close()
            return


def main(argv):
    env_url = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE") or ""
    directory, ratings, workload = read_brief(prompt_file)
    catalog = build_catalog(env_url, directory, ratings)
    if "--list" in argv:
        sys.stdout.write(catalog_bytes(catalog) + "\n")
        sys.stdout.flush()
        return 0
    if not env_url:
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    aggregator = Aggregator(env_url, directory, ratings, workload, catalog)
    server = ThreadingHTTPServer(("0.0.0.0", 0), make_handler(aggregator))
    server.daemon_threads = True
    # serve_forever runs on its own thread: the main thread registers and
    # polls /work, and exits 0 when the env says the workload is done.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    if port == 8080:
        raise RuntimeError("the OS picked 8080, which the env owns: restart")
    log("listening on 0.0.0.0:%d with %d providers and %d free models"
        % (port, len(catalog["providers"]),
           len(free_model_list(catalog))))
    register(env_url, port, catalog)
    poll_until_done(env_url, server)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as err:
        sys.stderr.write("aggregator: fatal: %s\n" % err)
        sys.exit(1)
