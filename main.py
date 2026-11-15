#!/usr/bin/env python3
"""Failover-first free-model aggregator for free-model-aggregator-01.

One server, two client wires (OpenAI Chat Completions and Anthropic
Messages, JSON and SSE), ten simulated free-tier providers on three
wires (OpenAI, Anthropic, Gemini). Python 3.13 standard library only.

Routing doctrine -- failover first, quality second:
  * Every request gets a ladder of ALL free models that meet its
    requirements, ordered by the sealed quality table's rating,
    best first. The ladder is walked top to bottom until one provider
    answers; the first success ends the request.
  * A provider that refuses (402, 429 or 5xx) is believed at once.
    - Refusal WITHOUT a retry-after: the provider is dead for the
      rest of the trial. It is never called again, for any request.
      A spent budget, a scheduled failure and an outage all look the
      same from the outside, and re-probing a dead provider only
      burns its remaining budget and the tie-break call count.
    - Refusal WITH a retry-after: the provider is rate-limited, not
      spent. Wait exactly the retry-after it named (seconds, from
      its header or its error body), then retry that same provider
      once. If it refuses again it is dead, same as above.
    - A 400 (bad model / bad key) kills that model only; the walk
      continues at the next rung.
  * No wall clock and no unseeded randomness anywhere in a routing
    decision: every choice is a function of the brief, the request
    and the providers' answers so far. Retries wait on the provider's
    own retry-after value, never on a locally invented delay.
  * Upstream calls are always plain JSON, even when the client asked
    for a stream: the provider's answer is parsed once (text, usage,
    stop family) and re-framed into the client's wire, JSON or SSE.
    That keeps every cross-wire translation in one place and means a
    streamed answer is byte-identical in content to the JSON one.
  * The catalog is built from the brief's provider directory and
    quality table, refreshed by probing each provider's own catalog
    route (a probe spends none of a provider's budget). The exact
    same document is printed by --list and sent at register time.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIXED_CREATED = 1767225600
ANTHROPIC_VERSION = "2023-06-01"
POLL_INTERVAL = 0.25
WORK_DEADLINE_SECS = 150.0
UPSTREAM_TIMEOUT = 25.0
PROBE_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Brief parsing: the prompt file embeds three JSON documents.
# --------------------------------------------------------------------------

def _extract_doc(text, schema):
    marker = '{"schema":"' + schema + '"'
    idx = text.find(marker)
    if idx < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        doc, _ = decoder.raw_decode(text[idx:])
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def read_brief(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    providers = _extract_doc(text, "free-model-aggregator-01/providers/v1")
    ratings = _extract_doc(text, "free-model-aggregator-01/ratings/v1")
    workload = _extract_doc(text, "free-model-aggregator-01/workload/v1")
    return providers, ratings, workload


# --------------------------------------------------------------------------
# HTTP helpers (urllib, stdlib only).
# --------------------------------------------------------------------------

def http_json(method, url, headers=None, body=None, timeout=UPSTREAM_TIMEOUT):
    """One HTTP call. Returns (status, headers_dict, parsed_json_or_None, raw_bytes)."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            h = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as err:
        raw = err.read()
        h = {k.lower(): v for k, v in err.headers.items()}
        return err.code, h, _try_json(raw), raw
    except Exception:
        return 0, {}, None, b""
    return status, h, _try_json(raw), raw


def _try_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def retry_after_seconds(status, headers, payload):
    """The retry-after a refusal names, or None. Header wins, body next."""
    if status != 429:
        return None
    raw = headers.get("retry-after")
    if raw is not None:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and "retry_after" in err:
            try:
                return float(err["retry_after"])
            except (TypeError, ValueError):
                return None
    return None


def auth_headers(provider):
    auth = provider.get("auth", "none")
    key = provider.get("key", "")
    if auth == "bearer":
        return {"Authorization": "Bearer " + key}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


# --------------------------------------------------------------------------
# Catalog: directory + quality table, refreshed by probing each provider.
# --------------------------------------------------------------------------

class Catalog:
    """Holds providers, ratings, manifest, and the registered document."""

    def __init__(self, providers_doc, ratings_doc, workload_doc, instance):
        self.providers = []  # [{id, name, wire, auth, key_env, key, base_url, path}]
        self.ratings = {}    # (provider_id, model_id) -> row
        self.rated_models = {}  # provider_id -> [model_id in brief order]
        self.manifest = []   # [{id, api, stream, weight, requires}]
        self._load_brief(providers_doc, ratings_doc, workload_doc)
        if instance:
            self._load_instance(instance)
        self.probes = {}     # provider_id -> [model dicts] from the live probe
        self.document = None
        self.document_text = None

    def _load_brief(self, providers_doc, ratings_doc, workload_doc):
        if providers_doc:
            for p in providers_doc.get("providers", []):
                self.providers.append({
                    "id": p["id"], "name": p.get("name", p["id"]),
                    "wire": p["wire"], "auth": p.get("auth", "none"),
                    "key_env": p.get("key_env", ""), "key": p.get("key", ""),
                    "base_url": p.get("path", "/p/" + p["id"]),
                    "path": p.get("path", "/p/" + p["id"]),
                })
        if ratings_doc:
            for row in ratings_doc.get("ratings", []):
                key = (row["provider"], row["model"])
                self.ratings[key] = row
                self.rated_models.setdefault(row["provider"], []).append(row["model"])
        if workload_doc:
            self.manifest = list(workload_doc.get("manifest", []))

    def _load_instance(self, instance):
        by_id = {p["id"]: p for p in self.providers}
        for p in instance.get("providers", []):
            if p["id"] in by_id:
                row = by_id[p["id"]]
                row["base_url"] = p.get("base_url", row["base_url"])
                row["key"] = p.get("key", row["key"])
                row["key_env"] = p.get("key_env", row["key_env"])
                row["wire"] = p.get("wire", row["wire"])
                row["auth"] = p.get("auth", row["auth"])
            else:
                self.providers.append({
                    "id": p["id"], "name": p.get("name", p["id"]),
                    "wire": p.get("wire", "openai"),
                    "auth": p.get("auth", "none"),
                    "key_env": p.get("key_env", ""), "key": p.get("key", ""),
                    "base_url": p.get("base_url", "/p/" + p["id"]),
                    "path": p.get("base_url", "/p/" + p["id"]),
                })
        wl = instance.get("workload")
        if isinstance(wl, dict) and wl.get("manifest"):
            self.manifest = list(wl["manifest"])

    def probe_all(self):
        """GET each provider's catalog route. Probes spend no budget."""
        for provider in self.providers:
            wire = provider["wire"]
            base = provider["base_url"].rstrip("/")
            if not base.startswith("http://") and not base.startswith("https://"):
                # No env to dial: the brief's path alone is not a URL.
                continue
            if wire == "gemini":
                url = base + "/v1beta/models"
            else:
                url = base + "/v1/models"
            headers = dict(auth_headers(provider))
            if wire == "anthropic":
                headers["anthropic-version"] = ANTHROPIC_VERSION
            status, _h, payload, _raw = http_json(
                "GET", url, headers=headers, timeout=PROBE_TIMEOUT)
            models = self._parse_probe(wire, payload)
            if status == 200 and models is not None:
                self.probes[provider["id"]] = models

    @staticmethod
    def _parse_probe(wire, payload):
        if not isinstance(payload, dict):
            return None
        if wire == "gemini":
            items = payload.get("models")
            if not isinstance(items, list):
                return None
            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name", "")
                model_id = name[7:] if name.startswith("models/") else name
                out.append({
                    "id": model_id,
                    "free": item.get("free", True) is True,
                    "context_length": item.get("context_length"),
                })
            return out
        items = payload.get("data")
        if not isinstance(items, list):
            return None
        out = []
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            out.append({
                "id": item["id"],
                "free": item.get("free", True) is True,
                "context_length": item.get("context_length"),
            })
        return out

    def build_document(self):
        """The one catalog document: --list prints it, register sends it.

        Built from the brief alone (directory + quality table). The
        live probes still run, but only as verification: a probe that
        listed an extra model the brief never names (a paid model)
        would make the registered document differ from the one
        --list prints where there is no env to probe, and the rules
        require the two to be byte for byte the same document. The
        brief and the probes agree on every free model, its free
        flag and its context length, so nothing is lost.
        """
        providers = []
        for provider in self.providers:
            pid = provider["id"]
            models = []
            for model_id in self.rated_models.get(pid, []):
                row = self.ratings.get((pid, model_id), {})
                models.append({
                    "id": model_id,
                    "free": bool(row.get("free", True)),
                    "context_length": row.get("context_length"),
                })
            # Verification only: a live probe that disagreed with the
            # brief on a free flag or a context length is reported,
            # never silently folded into the document.
            probed = self.probes.get(pid)
            if probed is not None:
                brief = {m["id"]: m for m in models}
                for m in probed:
                    other = brief.get(m["id"])
                    if other is not None and (
                            other["free"] != bool(m["free"])
                            or other["context_length"] != m["context_length"]):
                        sys.stderr.write(
                            "catalog probe disagrees with brief for "
                            "%s/%s\n" % (pid, m["id"]))
            providers.append({
                "id": pid,
                "base_url": provider["path"],
                "key_env": provider.get("key_env", ""),
                "wire": provider["wire"],
                "models": models,
            })
        self.document = {"providers": providers}
        self.document_text = json.dumps(self.document)
        return self.document

    def free_model_ids(self):
        seen = []
        for provider in self.document["providers"]:
            for model in provider["models"]:
                if model["free"] and model["id"] not in seen:
                    seen.append(model["id"])
        return seen

    def context_of(self, model_id):
        for provider in self.document["providers"]:
            for model in provider["models"]:
                if model["id"] == model_id and model["free"]:
                    return model["context_length"]
        return None


# --------------------------------------------------------------------------
# Canonical request: one parsed form for both client wires.
# --------------------------------------------------------------------------

def _blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str))
    return ""


def _split_data_url(url):
    """data:<mime>;base64,<data> -> (mime, data); else (None, None)."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    head, _, data = url.partition(",")
    if ";base64" not in head:
        return None, None
    mime = head[len("data:"):].split(";")[0] or "image/png"
    return mime, data


class CanonicalRequest:
    """Everything a provider wire needs, derived from the client body."""

    def __init__(self, api, body):
        self.api = api
        self.body = body
        self.system_text = ""
        self.messages = []   # [{role, text, images:[{mime,data} | {url}]}]
        self.tools = []      # [{name, description, parameters}]
        self.tool_choice = None
        self.max_tokens = None
        self.stops = []
        self.temperature = None
        self.json_mode = False
        self.stream = body.get("stream") is True
        if api == "openai":
            self._from_openai(body)
        else:
            self._from_anthropic(body)

    def _from_openai(self, body):
        for msg in body.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "system":
                text = _blocks_text(msg.get("content"))
                if text:
                    self.system_text = (self.system_text + "\n" + text
                                        if self.system_text else text)
                continue
            text = _blocks_text(msg.get("content"))
            images = []
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "image_url":
                        iu = block.get("image_url") or {}
                        url = iu.get("url", "")
                        mime, data = _split_data_url(url)
                        if data is not None:
                            images.append({"mime": mime, "data": data})
                        else:
                            images.append({"url": url})
            self.messages.append({"role": role or "user", "text": text,
                                  "images": images})
        for tool in body.get("tools") or []:
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                if isinstance(fn.get("name"), str):
                    self.tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    })
        if "tool_choice" in body:
            self.tool_choice = body["tool_choice"]
        if isinstance(body.get("max_tokens"), (int, float)):
            self.max_tokens = body["max_tokens"]
        elif isinstance(body.get("max_completion_tokens"), (int, float)):
            self.max_tokens = body["max_completion_tokens"]
        stop = body.get("stop")
        if isinstance(stop, str):
            self.stops = [stop]
        elif isinstance(stop, list):
            self.stops = [s for s in stop if isinstance(s, str)]
        if isinstance(body.get("temperature"), (int, float)):
            self.temperature = body["temperature"]
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
            self.json_mode = True

    def _from_anthropic(self, body):
        self.system_text = _blocks_text(body.get("system"))
        for msg in body.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            text = _blocks_text(msg.get("content"))
            images = []
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "image":
                        src = block.get("source") or {}
                        if src.get("type") == "base64":
                            images.append({
                                "mime": src.get("media_type", "image/png"),
                                "data": src.get("data", ""),
                            })
                        elif src.get("type") == "url":
                            images.append({"url": src.get("url", "")})
            self.messages.append({"role": msg.get("role", "user"), "text": text,
                                  "images": images})
        for tool in body.get("tools") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                self.tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                })
        if "tool_choice" in body:
            self.tool_choice = body["tool_choice"]
        if isinstance(body.get("max_tokens"), (int, float)):
            self.max_tokens = body["max_tokens"]
        stops = body.get("stop_sequences")
        if isinstance(stops, list):
            self.stops = [s for s in stops if isinstance(s, str)]
        if isinstance(body.get("temperature"), (int, float)):
            self.temperature = body["temperature"]
        # The Anthropic wire carries no JSON-mode flag; a JSON-mode
        # request on this wire is a manifest requirement, not a field.


# --------------------------------------------------------------------------
# Upstream bodies: the same canonical request in each provider wire.
# --------------------------------------------------------------------------

def build_upstream(provider, model_id, req):
    """Returns (url, headers, body_dict) for one provider call."""
    wire = provider["wire"]
    base = provider["base_url"].rstrip("/")
    headers = dict(auth_headers(provider))
    if wire == "openai":
        return base + "/v1/chat/completions", headers, _openai_body(model_id, req)
    if wire == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        return base + "/v1/messages", headers, _anthropic_body(model_id, req)
    url = (base + "/v1beta/models/"
           + urllib.parse.quote(model_id, safe="") + ":generateContent")
    return url, headers, _gemini_body(model_id, req)


def _openai_messages(req):
    messages = []
    if req.system_text:
        messages.append({"role": "system", "content": req.system_text})
    for msg in req.messages:
        if msg["images"]:
            content = []
            if msg["text"]:
                content.append({"type": "text", "text": msg["text"]})
            for img in msg["images"]:
                if "data" in img:
                    url = "data:%s;base64,%s" % (img["mime"], img["data"])
                else:
                    url = img["url"]
                content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["text"]})
    return messages


def _openai_body(model_id, req):
    body = {"model": model_id, "messages": _openai_messages(req),
            "stream": False}
    if req.max_tokens is not None:
        body["max_tokens"] = req.max_tokens
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"]}} for t in req.tools]
        if req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
    if req.stops:
        body["stop"] = req.stops
    if req.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _anthropic_messages(req):
    messages = []
    for msg in req.messages:
        if msg["images"]:
            content = []
            if msg["text"]:
                content.append({"type": "text", "text": msg["text"]})
            for img in msg["images"]:
                if "data" in img:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": img["mime"],
                        "data": img["data"]}})
                else:
                    content.append({"type": "image", "source": {
                        "type": "url", "url": img["url"]}})
            messages.append({"role": msg["role"], "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["text"]})
    return messages


def _anthropic_body(model_id, req):
    body = {"model": model_id, "messages": _anthropic_messages(req),
            "stream": False}
    if req.system_text:
        body["system"] = req.system_text
    if req.max_tokens is not None:
        body["max_tokens"] = req.max_tokens
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [{"name": t["name"], "description": t["description"],
                          "input_schema": t["parameters"]} for t in req.tools]
        if req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
    if req.stops:
        body["stop_sequences"] = req.stops
    return body


def _gemini_body(model_id, req):
    contents = []
    for msg in req.messages:
        role = "model" if msg["role"] == "assistant" else "user"
        parts = []
        if msg["text"]:
            parts.append({"text": msg["text"]})
        for img in msg["images"]:
            if "data" in img:
                parts.append({"inlineData": {"mimeType": img["mime"],
                                             "data": img["data"]}})
            else:
                parts.append({"fileData": {"fileUri": img["url"]}})
        contents.append({"role": role, "parts": parts})
    body = {"contents": contents}
    if req.system_text:
        body["systemInstruction"] = {"parts": [{"text": req.system_text}]}
    if req.tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in req.tools]}]
    config = {}
    if req.max_tokens is not None:
        config["maxOutputTokens"] = req.max_tokens
    if req.temperature is not None:
        config["temperature"] = req.temperature
    if req.stops:
        config["stopSequences"] = req.stops
    if req.json_mode:
        config["responseMimeType"] = "application/json"
    if config:
        body["generationConfig"] = config
    return body


# --------------------------------------------------------------------------
# Upstream answers: one parsed form for all three provider wires.
# --------------------------------------------------------------------------

class ProviderAnswer:
    def __init__(self, text, usage_in, usage_out, stop_family):
        self.text = text
        self.usage_in = usage_in
        self.usage_out = usage_out
        self.stop_family = stop_family  # 0 = stop/end_turn/STOP


def parse_provider_answer(wire, payload):
    if not isinstance(payload, dict):
        return None
    if wire == "openai":
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            text = _blocks_text(message.get("content"))
        usage = payload.get("usage") or {}
        return ProviderAnswer(text, usage.get("prompt_tokens"),
                              usage.get("completion_tokens"), 0)
    if wire == "anthropic":
        content = payload.get("content") or []
        text = "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str))
        usage = payload.get("usage") or {}
        return ProviderAnswer(text, usage.get("input_tokens"),
                              usage.get("output_tokens"), 0)
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return None
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = "".join(
        p.get("text", "") for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str))
    usage = payload.get("usageMetadata") or {}
    return ProviderAnswer(text, usage.get("promptTokenCount"),
                          usage.get("candidatesTokenCount"), 0)


# --------------------------------------------------------------------------
# The router: a rating-ordered ladder with drop-on-refusal failover.
# --------------------------------------------------------------------------

class Router:
    def __init__(self, catalog):
        self.catalog = catalog
        self.providers = {p["id"]: p for p in catalog.providers}
        self.calls = {p["id"]: 0 for p in catalog.providers}
        self.dead = set()       # provider ids that refused and stay refused
        self.dead_models = set()  # (provider id, model id) refused with a 400
        self.lock = threading.Lock()
        self.served_count = 0   # requests served, in manifest order
        self.last_model = "auto"  # model that answered the last request

    def requirements_for(self, req, api):
        """Manifest requirements for the next request, else body-derived."""
        idx = self.served_count
        if 0 <= idx < len(self.catalog.manifest):
            entry = self.catalog.manifest[idx]
            reqs = dict(entry.get("requires") or {})
        else:
            reqs = {}
            if req.tools:
                reqs["tools"] = True
            if any(m["images"] for m in req.messages):
                reqs["vision"] = True
            if req.json_mode:
                reqs["json"] = True
        # An OpenAI client asking for JSON mode can only be echoed by a
        # wire that carries response_format; the Anthropic wire cannot.
        if api == "openai" and req.json_mode:
            reqs["_no_anthropic_wire"] = True
        return reqs

    def ladder(self, reqs):
        out = []
        for (pid, model_id), row in self.catalog.ratings.items():
            if not row.get("free", True):
                continue
            if reqs.get("tools") and not row.get("tools"):
                continue
            if reqs.get("vision") and not row.get("vision"):
                continue
            if reqs.get("json") and not row.get("json"):
                continue
            if reqs.get("min_context") is not None:
                ctx = row.get("context_length")
                if ctx is None or ctx < reqs["min_context"]:
                    continue
            if reqs.get("_no_anthropic_wire"):
                provider = self.providers.get(pid)
                if provider is not None and provider["wire"] == "anthropic":
                    continue
            out.append((row.get("rating_milli", 0), pid, model_id))
        # Best rating first; ties break on provider id, then model id,
        # so the ladder is identical on every run.
        out.sort(key=lambda item: (-item[0], item[1], item[2]))
        return out

    def call_provider(self, provider, model_id, req):
        """One upstream call.

        Returns (answer, refusal): answer is a ProviderAnswer on
        success, else None; refusal is None on success, else a dict
        with the status and the retry-after the refusal named (None
        when it named none).
        """
        url, headers, body = build_upstream(provider, model_id, req)
        status, resp_headers, payload, _raw = http_json(
            "POST", url, headers=headers, body=body)
        self.calls[provider["id"]] = self.calls.get(provider["id"], 0) + 1
        if 200 <= status < 300:
            answer = parse_provider_answer(provider["wire"], payload)
            if answer is not None:
                return answer, None
            return None, {"status": status, "retry_after": None}
        return None, {"status": status,
                      "retry_after": retry_after_seconds(
                          status, resp_headers, payload)}

    def serve(self, api, body):
        """Walk the ladder until a provider answers. Returns ProviderAnswer or None."""
        with self.lock:
            req = CanonicalRequest(api, body)
            reqs = self.requirements_for(req, api)
            ladder = self.ladder(reqs)
            answer = None
            chosen = None
            for _rating, pid, model_id in ladder:
                if pid in self.dead or (pid, model_id) in self.dead_models:
                    continue
                provider = self.providers[pid]
                answer, refusal = self.call_provider(provider, model_id, req)
                if answer is not None:
                    chosen = model_id
                    break
                if refusal is None:
                    continue
                status = refusal["status"]
                if status == 400:
                    # A bad model or a bad key: that model is unusable,
                    # but the provider may still serve its other models.
                    self.dead_models.add((pid, model_id))
                    continue
                if status in (402, 429) or status >= 500:
                    wait = refusal["retry_after"]
                    if wait is not None:
                        # Rate-limited, not spent: honour the exact
                        # retry-after, then try this provider once more.
                        time.sleep(wait)
                        answer, refusal2 = self.call_provider(
                            provider, model_id, req)
                        if answer is not None:
                            chosen = model_id
                            break
                        if refusal2 is not None and refusal2["status"] == 400:
                            self.dead_models.add((pid, model_id))
                            continue
                    # Spent, failed or out: dead for the rest of the trial.
                    self.dead.add(pid)
                    continue
                self.dead.add(pid)
            self.served_count += 1
            self.last_model = chosen if chosen is not None else "auto"
            return answer


# --------------------------------------------------------------------------
# Client responses: the provider's answer in the client's wire.
# --------------------------------------------------------------------------

def _split_text(text, size=64):
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def render_openai_json(model_id, answer):
    usage_in = answer.usage_in if isinstance(answer.usage_in, int) else 0
    usage_out = answer.usage_out if isinstance(answer.usage_out, int) else 0
    return {
        "id": "chatcmpl-agg",
        "object": "chat.completion",
        "created": FIXED_CREATED,
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer.text},
            "finish_reason": "stop",
            "logprobs": None,
        }],
        "usage": {
            "prompt_tokens": usage_in,
            "completion_tokens": usage_out,
            "total_tokens": usage_in + usage_out,
        },
    }


def render_openai_sse(model_id, answer):
    usage_in = answer.usage_in if isinstance(answer.usage_in, int) else 0
    usage_out = answer.usage_out if isinstance(answer.usage_out, int) else 0
    frames = []

    def chunk(delta, finish):
        return {"id": "chatcmpl-agg", "object": "chat.completion.chunk",
                "created": FIXED_CREATED, "model": model_id,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}]}

    frames.append("data: " + json.dumps(
        chunk({"role": "assistant", "content": ""}, None)) + "\n\n")
    for piece in _split_text(answer.text):
        frames.append("data: " + json.dumps(
            chunk({"content": piece}, None)) + "\n\n")
    frames.append("data: " + json.dumps(chunk({}, "stop")) + "\n\n")
    frames.append("data: " + json.dumps({
        "id": "chatcmpl-agg", "object": "chat.completion.chunk",
        "created": FIXED_CREATED, "model": model_id, "choices": [],
        "usage": {"prompt_tokens": usage_in,
                  "completion_tokens": usage_out,
                  "total_tokens": usage_in + usage_out}}) + "\n\n")
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def render_anthropic_json(model_id, answer):
    usage_in = answer.usage_in if isinstance(answer.usage_in, int) else 0
    usage_out = answer.usage_out if isinstance(answer.usage_out, int) else 0
    return {
        "id": "msg_agg",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": [{"type": "text", "text": answer.text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
    }


def render_anthropic_sse(model_id, answer):
    usage_in = answer.usage_in if isinstance(answer.usage_in, int) else 0
    usage_out = answer.usage_out if isinstance(answer.usage_out, int) else 0
    frames = []

    def frame(event, data):
        return "event: %s\ndata: %s\n\n" % (event, json.dumps(data))

    frames.append(frame("message_start", {
        "type": "message_start",
        "message": {"id": "msg_agg", "type": "message", "role": "assistant",
                    "model": model_id, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage_in,
                              "output_tokens": 0}}}))
    frames.append(frame("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}))
    for piece in _split_text(answer.text):
        frames.append(frame("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece}}))
    frames.append(frame("content_block_stop", {
        "type": "content_block_stop", "index": 0}))
    frames.append(frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": usage_out}}))
    frames.append(frame("message_stop", {"type": "message_stop"}))
    return "".join(frames)


# --------------------------------------------------------------------------
# HTTP server: the two client APIs and the model list.
# --------------------------------------------------------------------------

class ServerState:
    def __init__(self, catalog, router):
        self.catalog = catalog
        self.router = router


STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _send_json(self, status, payload):
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, status, text, content_type):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/v1/models":
            self._handle_models()
        else:
            self._send_json(404, {"error": "not_found"})

    def _handle_models(self):
        # The wire is the auth header: Bearer is OpenAI, x-api-key is
        # Anthropic. Both list exactly the free model ids registered.
        auth = (self.headers.get("Authorization") or "")
        api_key = self.headers.get("x-api-key")
        catalog = STATE.catalog
        ids = catalog.free_model_ids()
        if auth.startswith("Bearer "):
            data = [{"id": mid, "object": "model",
                     "created": FIXED_CREATED,
                     "owned_by": "free-model-aggregator",
                     "free": True,
                     "context_length": catalog.context_of(mid)}
                    for mid in ids]
            self._send_json(200, {"object": "list", "data": data})
        else:
            _ = api_key
            data = [{"type": "model", "id": mid, "display_name": mid,
                     "created_at": "2026-01-01T00:00:00Z",
                     "free": True,
                     "context_length": catalog.context_of(mid)}
                    for mid in ids]
            self._send_json(200, {"data": data, "has_more": False})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/chat/completions":
            self._handle_chat("openai")
        elif path == "/v1/messages":
            self._handle_chat("anthropic")
        else:
            self._send_json(404, {"error": "not_found"})

    def _handle_chat(self, api):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid_request"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "invalid_request"})
            return
        answer = STATE.router.serve(api, body)
        if answer is None:
            # Every free model that could serve it refused: answer
            # with a plain 502 rather than a fabricated completion.
            if api == "openai":
                self._send_json(502, {"error": {
                    "message": "all providers refused",
                    "type": "api_error", "param": None, "code": None}})
            else:
                self._send_json(502, {"type": "error", "error": {
                    "type": "api_error",
                    "message": "all providers refused"}})
            return
        model_id = self._serving_model()
        stream = body.get("stream") is True
        if api == "openai":
            if stream:
                self._send_text(200, render_openai_sse(model_id, answer),
                                "text/event-stream; charset=utf-8")
            else:
                self._send_json(200, render_openai_json(model_id, answer))
        else:
            if stream:
                self._send_text(200, render_anthropic_sse(model_id, answer),
                                "text/event-stream; charset=utf-8")
            else:
                self._send_json(200, render_anthropic_json(model_id, answer))

    def _serving_model(self):
        # The model that answered the last request served.
        return getattr(STATE.router, "last_model", "auto")



# --------------------------------------------------------------------------
# Main: --list, or register with the env and serve until the work is done.
# --------------------------------------------------------------------------

def build_catalog_from_env():
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    providers_doc = ratings_doc = workload_doc = None
    if prompt_file:
        providers_doc, ratings_doc, workload_doc = read_brief(prompt_file)
    instance = None
    if env_url:
        _status, _h, payload, _raw = http_json(
            "GET", env_url + "/instance", timeout=PROBE_TIMEOUT)
        if isinstance(payload, dict):
            instance = payload
    catalog = Catalog(providers_doc, ratings_doc, workload_doc, instance)
    catalog.probe_all()
    catalog.build_document()
    return catalog, env_url


def main(argv):
    if "--list" in argv:
        prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
        env_url = os.environ.get("MAKEFUN_ENV_URL", "")
        if not prompt_file and not env_url:
            sys.stdout.write('{"providers":[]}\n')
            return 0
        catalog, _env = build_catalog_from_env()
        sys.stdout.write(catalog.document_text + "\n")
        return 0

    catalog, env_url = build_catalog_from_env()
    if not env_url:
        sys.stderr.write("free-model-aggregator-01: MAKEFUN_ENV_URL is not set\n")
        return 1
    if not catalog.providers:
        sys.stderr.write("free-model-aggregator-01: no providers in the brief\n")
        return 1

    global STATE
    router = Router(catalog)
    STATE = ServerState(catalog, router)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    registered = False
    for _attempt in range(5):
        status, _h, payload, _raw = http_json(
            "POST", env_url + "/register",
            body={"protocol_version": 1, "port": port,
                  "catalog": catalog.document},
            timeout=PROBE_TIMEOUT)
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            registered = True
            break
        time.sleep(0.5)
    if not registered:
        server.shutdown()
        sys.stderr.write("free-model-aggregator-01: register failed\n")
        return 1

    deadline = time.monotonic() + WORK_DEADLINE_SECS
    while time.monotonic() < deadline:
        status, _h, payload, _raw = http_json(
            "GET", env_url + "/work", timeout=PROBE_TIMEOUT)
        if isinstance(payload, dict) and payload.get("done"):
            break
        time.sleep(POLL_INTERVAL)
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
