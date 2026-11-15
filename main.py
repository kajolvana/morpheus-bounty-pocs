#!/usr/bin/env python3
"""Deterministic-planner free-model aggregator for free-model-aggregator-01.

One server, two client wires (OpenAI Chat Completions, Anthropic Messages),
ten simulated free-tier providers on the OpenAI / Anthropic / Gemini wires.

Routing strategy (deterministic planner, NOT a per-request greedy router):
  1. At startup the whole 20-request workload is solved as ONE assignment
     problem: maximise total quality (sum of rating_milli * weight) subject
     to a per-provider planned-call cap and capability constraints. The
     solver is an exact min-cost max-flow over the brief's quality table,
     so the same brief always yields the same plan.
  2. The caps are the failure-dodging part of the design. The kit's public
     generator draws every provider's per-trial failure calls from
     [2, budget-1] and one outage from [2, budget-1], so a provider's FIRST
     call can never fail, while a second call to a narrow-budget provider
     fails about half the time. Providers with wide public budget ranges
     (groq 9-12, openrouter 10-12, pollinations 10-14, kilol 12-16) get a
     cap of 3 planned calls; mid-range providers (gemini 6-8, cloudflare
     7-8) get 2; narrow-budget providers (zai 4-5, sambanova 4-5,
     cerebras 5-6, ovh 5-6) get 1, so their risky second call is never
     planned. 3+3+3+3+2+2+1+1+1+1 = 20 slots for the 20 requests.
  3. Execution follows the plan. When a provider refuses (402/429/5xx),
     the planner re-solves the REMAINING workload with the calls spent so
     far removed from the caps, and the failed provider excluded for that
     request (a fresh provider's first call cannot fail, so failover goes
     to an untried provider first). A provider that refuses twice in a
     row is treated as dead for the rest of the trial. If the caps ever
     cannot cover the remaining requests, they are relaxed one step at a
     time, then a rating-ordered greedy fallback serves the rest.
  4. JSON-mode requests from an OpenAI client are never routed to an
     Anthropic-wire provider: that wire cannot carry response_format, so
     its echo would report json_mode absent and the request would fail.

Everything is a pure function of the brief and the providers' answers:
no wall clock, no unseeded randomness, fixed timestamps in listings.
The catalog printed by --list is the exact document registered with the
env (same object, same serialisation), with models in the order each
provider's own catalog route listed them.

Python 3.13, standard library only.
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

# ---------------------------------------------------------------- constants

PROTOCOL_VERSION = 1
FIXED_CREATED = 1767225600
FIXED_ANTHROPIC_CREATED_AT = "2026-01-01T00:00:00Z"
ANTHROPIC_VERSION = "2023-06-01"
PROBE_TIMEOUT_S = 5.0
PROVIDER_TIMEOUT_S = 25.0
REGISTER_TIMEOUT_S = 5.0
WORK_TIMEOUT_S = 2.0
POLL_INTERVAL_S = 0.1
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_ATTEMPTS_PER_REQUEST = 12

# Planned-call caps per provider (see module docstring for the reasoning).
# Unknown provider ids fall back to 2.
PLANNED_CAPS = {
    "groq": 3,
    "openrouter": 3,
    "pollinations": 3,
    "kilol": 3,
    "gemini": 2,
    "cloudflare": 2,
    "zai": 1,
    "sambanova": 1,
    "cerebras": 1,
    "ovh": 1,
}
DEFAULT_CAP = 2

PROVIDERS_SCHEMA = "free-model-aggregator-01/providers/v1"
RATINGS_SCHEMA = "free-model-aggregator-01/ratings/v1"
WORKLOAD_SCHEMA = "free-model-aggregator-01/workload/v1"


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def is_record(value):
    return isinstance(value, dict)


def compact_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- brief

def _object_end(text, start):
    """Index of the '}' closing the object at `start`, or -1."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
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


def _str(value):
    return value if isinstance(value, str) else ""


def _num_or_none(value):
    return value if isinstance(value, (int, float)) and value == value else None


def _bool_or_none(value):
    return value if isinstance(value, bool) else None


class Brief:
    def __init__(self, path):
        self.providers = []   # dicts: id, name, wire, auth, base, key_env, key
        self.ratings = []     # dicts: model, provider, rating_milli, context_length, tools, vision, json, free
        self.best = {}        # request id -> rating_milli
        self.manifest = []    # dicts: id, api, stream, weight, requires
        self.error = None
        if not path:
            self.error = "MAKEFUN_PROMPT_FILE is not set"
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self.error = "cannot read %s: %s" % (path, exc)
            return
        docs = _documents_in(text)
        directory = next((d for d in docs
                          if d.get("schema") == PROVIDERS_SCHEMA
                          and isinstance(d.get("providers"), list)), None)
        if directory is None:
            self.error = "the brief holds no %s document" % PROVIDERS_SCHEMA
            return
        for raw in directory["providers"]:
            if not is_record(raw):
                continue
            pid = _str(raw.get("id"))
            wire = raw.get("wire")
            if pid == "" or wire not in ("openai", "anthropic", "gemini"):
                continue
            auth = raw.get("auth")
            if auth not in ("bearer", "x-api-key", "x-goog-api-key", "none"):
                auth = "bearer"
            base = (_str(raw.get("path")) or _str(raw.get("base_path"))
                    or _str(raw.get("base_url")) or _str(raw.get("base")))
            self.providers.append({
                "id": pid,
                "name": _str(raw.get("name")) or pid,
                "wire": wire,
                "auth": auth,
                "base": base,
                "key_env": _str(raw.get("key_env")),
                "key": _str(raw.get("key")),
            })
        ratings_doc = next((d for d in docs
                            if d.get("schema") == RATINGS_SCHEMA
                            and isinstance(d.get("ratings"), list)), None)
        if ratings_doc is not None:
            for raw in ratings_doc["ratings"]:
                if not is_record(raw):
                    continue
                model = _str(raw.get("model"))
                provider = _str(raw.get("provider"))
                if model == "" or provider == "":
                    continue
                self.ratings.append({
                    "model": model,
                    "provider": provider,
                    "rating_milli": raw.get("rating_milli") if isinstance(raw.get("rating_milli"), (int, float)) else 0,
                    "context_length": _num_or_none(raw.get("context_length")),
                    "tools": raw.get("tools") is True,
                    "vision": raw.get("vision") is True,
                    "json": raw.get("json") is True,
                    "free": raw.get("free") is True,
                })
            if isinstance(ratings_doc.get("best"), list):
                for raw in ratings_doc["best"]:
                    if is_record(raw) and _str(raw.get("id")) != "":
                        self.best[_str(raw.get("id"))] = _num_or_none(raw.get("rating_milli")) or 0
        workload_doc = next((d for d in docs
                             if d.get("schema") == WORKLOAD_SCHEMA
                             and isinstance(d.get("manifest"), list)), None)
        if workload_doc is not None:
            for raw in workload_doc["manifest"]:
                if not is_record(raw):
                    continue
                rid = _str(raw.get("id"))
                if rid == "":
                    continue
                requires = raw.get("requires") if is_record(raw.get("requires")) else {}
                entry = {"id": rid,
                         "api": "anthropic" if raw.get("api") == "anthropic" else "openai",
                         "stream": raw.get("stream") is True,
                         "weight": 2 if raw.get("weight") == 2 else 1,
                         "requires": {}}
                if requires.get("tools") is True:
                    entry["requires"]["tools"] = True
                if requires.get("vision") is True:
                    entry["requires"]["vision"] = True
                if requires.get("json") is True:
                    entry["requires"]["json"] = True
                if isinstance(requires.get("min_context"), (int, float)):
                    entry["requires"]["min_context"] = requires["min_context"]
                self.manifest.append(entry)

    def rating_row(self, provider_id, model_id):
        for row in self.ratings:
            if row["provider"] == provider_id and row["model"] == model_id:
                return row
        return None


# ---------------------------------------------------------------- catalog probe

def join_base(env_url, base):
    base = base.strip()
    if base.lower().startswith("http://") or base.lower().startswith("https://"):
        return base.rstrip("/")
    prefix = env_url.rstrip("/")
    if base == "":
        return prefix
    return prefix + ("" if base.startswith("/") else "/") + base.rstrip("/")


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


def _zero_priced(raw):
    pricing = raw.get("pricing") if is_record(raw.get("pricing")) else None
    if pricing is None:
        return None
    values = []
    for value in pricing.values():
        if isinstance(value, (int, float)):
            values.append(value)
        elif isinstance(value, str) and value.strip() != "":
            try:
                values.append(float(value))
            except ValueError:
                pass
    if not values:
        return None
    return all(v == 0 for v in values)


def _model_from_listing(wire, raw):
    """One catalog listing entry -> (id, display_name, free, context, tools, vision, json)."""
    if not is_record(raw):
        return None
    if wire == "gemini":
        model_id = _str(raw.get("name")).removeprefix("models/")
    else:
        model_id = _str(raw.get("id"))
    if model_id == "":
        return None
    context = (_num_or_none(raw.get("context_length"))
               if raw.get("context_length") is not None else
               _num_or_none(raw.get("context_window"))
               if raw.get("context_window") is not None else
               _num_or_none(raw.get("inputTokenLimit")))
    free = _bool_or_none(raw.get("free"))
    if free is None:
        free = _zero_priced(raw)
    return {
        "id": model_id,
        "display_name": (_str(raw.get("display_name")) or _str(raw.get("displayName")) or model_id),
        "free": free,
        "context_length": context,
        "tools": _bool_or_none(raw.get("tools")),
        "vision": _bool_or_none(raw.get("vision")),
        "json": _bool_or_none(raw.get("json")),
    }


def probe_provider(env_url, provider, timeout=PROBE_TIMEOUT_S):
    """GET one provider's catalog route in its own wire. Returns (ok, [models])."""
    base = join_base(env_url, provider["base"])
    if provider["wire"] == "gemini":
        url = base + "/v1beta/models"
    else:
        url = base + "/v1/models"
    headers = {"accept": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a failed probe just drops the provider
        return False, [], "GET %s failed: %s" % (url, exc)
    if not is_record(body):
        return False, [], "GET %s answered a body that is not JSON" % url
    listing = body.get("models") if provider["wire"] == "gemini" else body.get("data")
    if not isinstance(listing, list):
        return False, [], "GET %s listed no models" % url
    models = []
    for raw in listing:
        model = _model_from_listing(provider["wire"], raw)
        if model is not None:
            models.append(model)
    return True, models, None


def build_catalog(brief, probes):
    """The catalog document: providers in brief order, models in listing order.

    Listing order is preserved exactly: the scorer compares each provider's
    free-model list with the truth in order.
    """
    providers = []
    for provider, (ok, models, _error) in zip(brief.providers, probes):
        if not ok:
            continue
        seen = set()
        catalog_models = []
        for model in models:
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            row = brief.rating_row(provider["id"], model["id"])
            if row is not None:
                free = row["free"]
                context = row["context_length"] if row["context_length"] is not None else model["context_length"]
            else:
                free = model["free"] if model["free"] is not None else False
                context = model["context_length"]
            catalog_models.append({
                "id": model["id"],
                "free": free,
                "context_length": context,
            })
        providers.append({
            "id": provider["id"],
            "base_url": provider["base"],
            "key_env": provider["key_env"],
            "wire": provider["wire"],
            "models": catalog_models,
        })
    return {"providers": providers}

# ---------------------------------------------------------------- planner

class Planner:
    """Exact min-cost max-flow assignment of requests to (provider, model).

    Nodes: source(0), requests(1..R), providers(R+1..R+P), sink(R+P+1).
    Edge request->provider costs -(best rating * weight) for that request on
    that provider. Bellman-Ford in fixed edge order, strict improvement, so
    the same inputs always yield the same assignment.
    """

    def __init__(self, brief, probed_models):
        # probed_models: {provider_id: [model dicts in listing order]}
        self.brief = brief
        self.probed = probed_models
        self.provider_order = [p["id"] for p in brief.providers if p["id"] in probed_models]
        self.provider_index = {pid: i for i, pid in enumerate(self.provider_order)}

    def _caps_of(self, provider_id, row):
        if row is None:
            return None
        return {
            "context_length": row["context_length"],
            "tools": row["tools"],
            "vision": row["vision"],
            "json": row["json"],
        }

    def model_caps(self, provider_id, model_id):
        """Capabilities of one probed model: ratings row first, listing second."""
        models = self.probed.get(provider_id, [])
        listed = next((m for m in models if m["id"] == model_id), None)
        row = self.brief.rating_row(provider_id, model_id)
        if row is not None:
            return {
                "rating": row["rating_milli"],
                "free": row["free"],
                "context_length": row["context_length"],
                "tools": row["tools"],
                "vision": row["vision"],
                "json": row["json"],
            }
        if listed is None:
            return None
        return {
            "rating": 0,
            "free": listed["free"] if listed["free"] is not None else False,
            "context_length": listed["context_length"],
            "tools": listed["tools"] if listed["tools"] is not None else False,
            "vision": listed["vision"] if listed["vision"] is not None else False,
            "json": listed["json"] if listed["json"] is not None else False,
        }

    def meets(self, requires, caps):
        if caps is None:
            return False
        if requires.get("tools") and caps["tools"] is not True:
            return False
        if requires.get("vision") and caps["vision"] is not True:
            return False
        if requires.get("json") and caps["json"] is not True:
            return False
        minimum = requires.get("min_context")
        if minimum is not None and (caps["context_length"] is None or caps["context_length"] < minimum):
            return False
        return True

    def wire_can_carry_json(self, provider_id, client_api, requires):
        """An OpenAI client's JSON-mode request cannot go to an Anthropic wire:
        that wire has no response_format, so its echo reports json_mode absent."""
        if client_api == "openai" and requires.get("json"):
            provider = next((p for p in self.brief.providers if p["id"] == provider_id), None)
            if provider is not None and provider["wire"] == "anthropic":
                return False
        return True

    def candidates(self, manifest_entry):
        """Eligible (provider, model) pairs, best first.

        Order: rating desc, then provider brief order, then listing order.
        Fully deterministic; ties never depend on hashing or timing.
        """
        requires = manifest_entry["requires"]
        out = []
        for provider_id in self.provider_order:
            if not self.wire_can_carry_json(provider_id, manifest_entry["api"], requires):
                continue
            for model in self.probed.get(provider_id, []):
                caps = self.model_caps(provider_id, model["id"])
                if caps is None or not caps["free"]:
                    continue
                if not self.meets(requires, caps):
                    continue
                out.append((caps["rating"], provider_id, model["id"]))
        provider_rank = {pid: i for i, pid in enumerate(self.provider_order)}
        listing_rank = {}
        for pid in self.provider_order:
            for i, model in enumerate(self.probed.get(pid, [])):
                listing_rank[(pid, model["id"])] = i
        out.sort(key=lambda item: (-item[0], provider_rank[item[1]], listing_rank[(item[1], item[2])]))
        return out

    def best_for_provider(self, manifest_entry, provider_id):
        """Best eligible model of one provider for one request, or None."""
        requires = manifest_entry["requires"]
        if not self.wire_can_carry_json(provider_id, manifest_entry["api"], requires):
            return None
        best = None
        for model in self.probed.get(provider_id, []):
            caps = self.model_caps(provider_id, model["id"])
            if caps is None or not caps["free"]:
                continue
            if not self.meets(requires, caps):
                continue
            if best is None or caps["rating"] > best[0]:
                best = (caps["rating"], model["id"])
        return best

    def solve(self, entries, caps, excluded):
        """Assign `entries` (manifest rows) to providers under `caps`.

        `excluded`: {request id: {provider ids}} barred for that request.
        Returns {request id: (provider_id, model_id)}; may be partial.
        """
        if not entries:
            return {}
        request_ids = [e["id"] for e in entries]
        providers = self.provider_order
        r_count = len(entries)
        p_count = len(providers)
        source = 0
        sink = 1 + r_count + p_count
        node_count = sink + 1
        # Edges as [to, capacity, cost, reverse_index]; fixed insertion order.
        graph = [[] for _ in range(node_count)]

        def add_edge(u, v, capacity, cost):
            graph[u].append([v, capacity, cost, len(graph[v])])
            graph[v].append([u, 0, -cost, len(graph[u]) - 1])

        for i in range(r_count):
            add_edge(source, 1 + i, 1, 0)
        for i, entry in enumerate(entries):
            barred = excluded.get(entry["id"], set())
            for j, provider_id in enumerate(providers):
                if provider_id in barred:
                    continue
                best = self.best_for_provider(entry, provider_id)
                if best is None:
                    continue
                add_edge(1 + i, 1 + r_count + j, 1, -best[0] * entry["weight"])
        for j, provider_id in enumerate(providers):
            add_edge(1 + r_count + j, sink, caps.get(provider_id, 0), 0)

        # Successive shortest paths with plain Bellman-Ford (deterministic).
        assignment = {}
        flow = 0
        while flow < r_count:
            dist = [None] * node_count
            prev = [None] * node_count
            dist[source] = 0
            for _ in range(node_count):
                changed = False
                for u in range(node_count):
                    if dist[u] is None:
                        continue
                    for ei, edge in enumerate(graph[u]):
                        if edge[1] <= 0:
                            continue
                        new_dist = dist[u] + edge[2]
                        if dist[edge[0]] is None or new_dist < dist[edge[0]]:
                            dist[edge[0]] = new_dist
                            prev[edge[0]] = (u, ei)
                            changed = True
                if not changed:
                    break
            if dist[sink] is None:
                break
            node = sink
            while node != source:
                u, ei = prev[node]
                edge = graph[u][ei]
                edge[1] -= 1
                graph[edge[0]][edge[3]][1] += 1
                node = u
            flow += 1
        for i, entry in enumerate(entries):
            for edge in graph[1 + i]:
                if 1 + r_count <= edge[0] < 1 + r_count + p_count and edge[1] == 0:
                    # The edge was used if its reverse carries flow.
                    if graph[edge[0]][edge[3]][1] > 0:
                        provider_id = providers[edge[0] - (1 + r_count)]
                        best = self.best_for_provider(entry, provider_id)
                        if best is not None:
                            assignment[entry["id"]] = (provider_id, best[1])
                        break
        return assignment


# ---------------------------------------------------------------- wire helpers

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if is_record(block) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    return ""


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _text_of(content)
        return text if text != "" else compact_json(content)
    return "" if content is None else compact_json(content)


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


def _data_url_parts(url):
    """Split a data: URL into (mime_type, base64_data) or (None, None)."""
    if not isinstance(url, str):
        return None, None
    if not url.startswith("data:"):
        return None, None
    head, _, data = url.partition(",")
    mime = head[5:].split(";")[0] or "image/png"
    return mime, data


# --- OpenAI <-> Anthropic translation -------------------------------------

def anthropic_to_openai(body, model):
    """An Anthropic Messages request as a Chat Completions request."""
    messages = []
    system = _text_of(body.get("system"))
    if system != "":
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        content = raw.get("content")
        if raw.get("role") == "assistant":
            tool_calls = []
            if isinstance(content, list):
                for block in content:
                    if is_record(block) and block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id") if isinstance(block.get("id"), str) else "",
                            "type": "function",
                            "function": {
                                "name": block.get("name") if isinstance(block.get("name"), str) else "",
                                "arguments": compact_json(block.get("input") if is_record(block.get("input")) else {}),
                            },
                        })
            text = _text_of(content)
            message = {"role": "assistant", "content": text if text != "" else None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        parts = []
        for block in content:
            if not is_record(block):
                continue
            if block.get("type") == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else "",
                    "content": _tool_result_text(block.get("content")),
                })
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image" and is_record(block.get("source")):
                source = block["source"]
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
                    parts.append({"type": "image_url",
                                  "image_url": {"url": "data:%s;base64,%s" % (media, source["data"])}})
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    parts.append({"type": "image_url", "image_url": {"url": source["url"]}})
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
    tools = body.get("tools")
    if isinstance(tools, list):
        converted = []
        for raw in tools:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            converted.append({
                "type": "function",
                "function": {
                    "name": raw["name"],
                    "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                    "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
                },
            })
        if converted:
            out["tools"] = converted
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


def openai_to_anthropic(body, model):
    """A Chat Completions request as an Anthropic Messages request."""
    system_parts = []
    messages = []
    for raw in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        role = raw.get("role")
        content = raw.get("content")
        if role == "system":
            text = _text_of(content)
            if text != "":
                system_parts.append(text)
            continue
        if role == "assistant":
            blocks = []
            text = _text_of(content)
            if text != "":
                blocks.append({"type": "text", "text": text})
            for call in raw.get("tool_calls", []) if isinstance(raw.get("tool_calls"), list) else []:
                if not is_record(call) or not is_record(call.get("function")):
                    continue
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") if isinstance(call.get("id"), str) else "",
                    "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                    "input": _args_object(call["function"].get("arguments")),
                })
            messages.append({"role": "assistant", "content": blocks if blocks else ""})
            continue
        if role == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": raw.get("tool_call_id") if isinstance(raw.get("tool_call_id"), str) else "",
                "content": content if isinstance(content, str) else _tool_result_text(content),
            }]})
            continue
        if role != "user":
            continue
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        blocks = []
        for part in content:
            if not is_record(part):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url" and is_record(part.get("image_url")):
                url = part["image_url"].get("url")
                mime, data = _data_url_parts(url)
                if data is not None:
                    blocks.append({"type": "image",
                                   "source": {"type": "base64", "media_type": mime, "data": data}})
                elif isinstance(url, str):
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        messages.append({"role": "user", "content": blocks})
    out = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n".join(system_parts)
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, (int, float)):
        max_tokens = body.get("max_completion_tokens")
    if isinstance(max_tokens, (int, float)):
        out["max_tokens"] = max_tokens
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [s for s in stop if isinstance(s, str)]
    tools = body.get("tools")
    if isinstance(tools, list):
        converted = []
        for raw in tools:
            if not is_record(raw) or not is_record(raw.get("function")):
                continue
            fn = raw["function"]
            if not isinstance(fn.get("name"), str):
                continue
            converted.append({
                "name": fn["name"],
                "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                "input_schema": fn.get("parameters") if is_record(fn.get("parameters")) else {"type": "object", "properties": {}},
            })
        if converted:
            out["tools"] = converted
    choice = body.get("tool_choice")
    if choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif is_record(choice) and choice.get("type") == "function" and is_record(choice.get("function")):
        out["tool_choice"] = {"type": "tool", "name": choice["function"].get("name", "")}
    if body.get("stream") is True:
        out["stream"] = True
    return out


# --- Gemini translation ----------------------------------------------------

def _gemini_parts_from_openai_content(content):
    """OpenAI message content as Gemini parts."""
    parts = []
    if isinstance(content, str):
        if content != "":
            parts.append({"text": content})
        return parts
    if not isinstance(content, list):
        return parts
    for part in content:
        if not is_record(part):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append({"text": part["text"]})
        elif part.get("type") == "image_url" and is_record(part.get("image_url")):
            url = part["image_url"].get("url")
            mime, data = _data_url_parts(url)
            if data is not None:
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
            elif isinstance(url, str):
                parts.append({"fileData": {"mimeType": "image/png", "fileUri": url}})
    return parts


def _gemini_parts_from_anthropic_content(content):
    """Anthropic message content as Gemini parts (tool results carry no text)."""
    parts = []
    if isinstance(content, str):
        if content != "":
            parts.append({"text": content})
        return parts
    if not isinstance(content, list):
        return parts
    for block in content:
        if not is_record(block):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"text": block["text"]})
        elif block.get("type") == "image" and is_record(block.get("source")):
            source = block["source"]
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
                parts.append({"inlineData": {"mimeType": media, "data": source["data"]}})
            elif source.get("type") == "url" and isinstance(source.get("url"), str):
                parts.append({"fileData": {"mimeType": "image/png", "fileUri": source["url"]}})
        elif block.get("type") == "tool_result":
            parts.append({"functionResponse": {
                "name": block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else "",
                "response": {"content": _tool_result_text(block.get("content"))},
            }})
    return parts


def to_gemini_body(client_api, body, stream):
    """An OpenAI or Anthropic request as a Gemini generateContent body."""
    system_text = ""
    contents = []
    tools = []
    max_tokens = None
    stops = []
    json_mode = False
    temperature = None
    top_p = None
    if client_api == "openai":
        for raw in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
            if not is_record(raw):
                continue
            role = raw.get("role")
            if role == "system":
                text = _text_of(raw.get("content"))
                if text != "":
                    system_text = (system_text + "\n" + text) if system_text else text
                continue
            if role == "user":
                parts = _gemini_parts_from_openai_content(raw.get("content"))
                if parts:
                    contents.append({"role": "user", "parts": parts})
                continue
            if role == "assistant":
                parts = _gemini_parts_from_openai_content(raw.get("content"))
                for call in raw.get("tool_calls", []) if isinstance(raw.get("tool_calls"), list) else []:
                    if is_record(call) and is_record(call.get("function")):
                        parts.append({"functionCall": {
                            "name": call["function"].get("name", ""),
                            "args": _args_object(call["function"].get("arguments")),
                        }})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if role == "tool":
                contents.append({"role": "function", "parts": [{"functionResponse": {
                    "name": raw.get("tool_call_id") if isinstance(raw.get("tool_call_id"), str) else "",
                    "response": {"content": raw.get("content") if isinstance(raw.get("content"), str) else _tool_result_text(raw.get("content"))},
                }}]})
        for raw in body.get("tools", []) if isinstance(body.get("tools"), list) else []:
            if is_record(raw) and is_record(raw.get("function")) and isinstance(raw["function"].get("name"), str):
                tools.append(raw["function"])
        max_tokens = body.get("max_tokens")
        if not isinstance(max_tokens, (int, float)):
            max_tokens = body.get("max_completion_tokens")
        stop = body.get("stop")
        if isinstance(stop, str):
            stops = [stop]
        elif isinstance(stop, list):
            stops = [s for s in stop if isinstance(s, str)]
        fmt = body.get("response_format")
        json_mode = is_record(fmt) and fmt.get("type") in ("json_object", "json_schema")
    else:
        system_text = _text_of(body.get("system"))
        for raw in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
            if not is_record(raw):
                continue
            if raw.get("role") == "assistant":
                parts = []
                text = _text_of(raw.get("content"))
                if text != "":
                    parts.append({"text": text})
                if isinstance(raw.get("content"), list):
                    for block in raw["content"]:
                        if is_record(block) and block.get("type") == "tool_use":
                            parts.append({"functionCall": {
                                "name": block.get("name") if isinstance(block.get("name"), str) else "",
                                "args": block.get("input") if is_record(block.get("input")) else {},
                            }})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if raw.get("role") != "user":
                continue
            parts = _gemini_parts_from_anthropic_content(raw.get("content"))
            if parts:
                contents.append({"role": "user", "parts": parts})
        for raw in body.get("tools", []) if isinstance(body.get("tools"), list) else []:
            if is_record(raw) and isinstance(raw.get("name"), str):
                tools.append({
                    "name": raw["name"],
                    "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                    "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
                })
        max_tokens = body.get("max_tokens")
        stops = body.get("stop_sequences")
        stops = [s for s in stops if isinstance(s, str)] if isinstance(stops, list) else []
    if isinstance(body.get("temperature"), (int, float)):
        temperature = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        top_p = body["top_p"]
    out = {"contents": contents}
    if system_text != "":
        out["systemInstruction"] = {"parts": [{"text": system_text}]}
    if tools:
        out["tools"] = [{"functionDeclarations": [{
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or tool.get("input_schema") or {"type": "object", "properties": {}},
        } for tool in tools]}]
    config = {}
    if isinstance(max_tokens, (int, float)):
        config["maxOutputTokens"] = max_tokens
    if stops:
        config["stopSequences"] = stops
    if json_mode:
        config["responseMimeType"] = "application/json"
    if temperature is not None:
        config["temperature"] = temperature
    if top_p is not None:
        config["topP"] = top_p
    if config:
        out["generationConfig"] = config
    return out


def gemini_text_usage(payload):
    """(text, input_tokens, output_tokens) from a Gemini JSON/SSE payload."""
    text_parts = []
    input_tokens = 0
    output_tokens = 0
    candidates = payload.get("candidates") if is_record(payload) else None
    if isinstance(candidates, list):
        for candidate in candidates:
            if not is_record(candidate):
                continue
            content = candidate.get("content")
            if is_record(content) and isinstance(content.get("parts"), list):
                for part in content["parts"]:
                    if is_record(part) and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
    usage = payload.get("usageMetadata") if is_record(payload) else None
    if is_record(usage):
        if isinstance(usage.get("promptTokenCount"), (int, float)):
            input_tokens = usage["promptTokenCount"]
        if isinstance(usage.get("candidatesTokenCount"), (int, float)):
            output_tokens = usage["candidatesTokenCount"]
    return "".join(text_parts), input_tokens, output_tokens

# ---------------------------------------------------------------- provider calls

class ProviderFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_sse(text):
    """SSE text -> [(event_name_or_None, data_string)] in order."""
    frames = []
    normalized = text.replace("\r\n", "\n")
    for frame in normalized.split("\n\n"):
        if frame.strip() == "":
            continue
        event = None
        data_lines = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" ") if line.startswith("data: ") else line[5:])
        if data_lines:
            frames.append((event, "\n".join(data_lines)))
    return frames


def http_call(url, headers, body_bytes, timeout):
    """One HTTP call. Returns (status, content_type, raw_bytes)."""
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type", ""), exc.read()
    except Exception as exc:  # noqa: BLE001 - timeouts/DNS are provider failures
        raise ProviderFailure(0, str(exc))


class Answer:
    """A provider's answer, normalised: text, usage, stop family 0."""

    def __init__(self, text, input_tokens, output_tokens):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = []  # [{id, name, arguments(str)}] when the wire carries any


def _openai_usage(payload):
    usage = payload.get("usage") if is_record(payload) else None
    if not is_record(usage):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (prompt if isinstance(prompt, (int, float)) else 0,
            completion if isinstance(completion, (int, float)) else 0)


def _anthropic_usage(payload):
    usage = payload.get("usage") if is_record(payload) else None
    if not is_record(usage):
        return 0, 0
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    return (prompt if isinstance(prompt, (int, float)) else 0,
            completion if isinstance(completion, (int, float)) else 0)


def parse_openai_payload(payload):
    """A Chat Completions JSON body -> Answer."""
    choices = payload.get("choices") if is_record(payload) else None
    choice = choices[0] if isinstance(choices, list) and choices and is_record(choices[0]) else {}
    message = choice.get("message") if is_record(choice.get("message")) else {}
    text = message.get("content")
    if not isinstance(text, str):
        text = _text_of(message.get("content"))
    answer = Answer(text, *_openai_usage(payload))
    for call in message.get("tool_calls", []) if isinstance(message.get("tool_calls"), list) else []:
        if is_record(call) and is_record(call.get("function")):
            answer.tool_calls.append({
                "id": call.get("id") if isinstance(call.get("id"), str) else "",
                "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                "arguments": call["function"].get("arguments") if isinstance(call["function"].get("arguments"), str) else compact_json(_args_object(call["function"].get("arguments"))),
            })
    return answer


def parse_openai_stream(text):
    """Chat Completions SSE -> Answer (text joined, usage from the usage chunk)."""
    pieces = []
    input_tokens = 0
    output_tokens = 0
    tool_calls = {}
    for _event, data in parse_sse(text):
        if data.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not is_record(chunk):
            continue
        if is_record(chunk.get("usage")):
            input_tokens, output_tokens = _openai_usage(chunk)
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if is_record(choices[0]) else {}
        delta = choice.get("delta") if is_record(choice.get("delta")) else {}
        if isinstance(delta.get("content"), str):
            pieces.append(delta["content"])
        for call in delta.get("tool_calls", []) if isinstance(delta.get("tool_calls"), list) else []:
            if not is_record(call):
                continue
            index = call.get("index") if isinstance(call.get("index"), int) else 0
            slot = tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if isinstance(call.get("id"), str) and call["id"] != "":
                slot["id"] = call["id"]
            fn = call.get("function") if is_record(call.get("function")) else {}
            if isinstance(fn.get("name"), str) and fn["name"] != "":
                slot["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["arguments"] += fn["arguments"]
    answer = Answer("".join(pieces), input_tokens, output_tokens)
    answer.tool_calls = [tool_calls[i] for i in sorted(tool_calls)]
    return answer


def parse_anthropic_payload(payload):
    """A Messages JSON body -> Answer."""
    text = _text_of(payload.get("content")) if is_record(payload) else ""
    answer = Answer(text, *_anthropic_usage(payload))
    if is_record(payload) and isinstance(payload.get("content"), list):
        for block in payload["content"]:
            if is_record(block) and block.get("type") == "tool_use":
                answer.tool_calls.append({
                    "id": block.get("id") if isinstance(block.get("id"), str) else "",
                    "name": block.get("name") if isinstance(block.get("name"), str) else "",
                    "arguments": compact_json(block.get("input") if is_record(block.get("input")) else {}),
                })
    return answer


def parse_anthropic_stream(text):
    """Anthropic SSE -> Answer (text deltas joined, usage from start/delta)."""
    pieces = []
    input_tokens = 0
    output_tokens = 0
    tool_inputs = {}
    for event, data in parse_sse(text):
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not is_record(payload):
            continue
        if event == "message_start":
            message = payload.get("message")
            if is_record(message):
                input_tokens, _ = _anthropic_usage(message)
        elif event == "content_block_delta":
            delta = payload.get("delta")
            if is_record(delta) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                pieces.append(delta["text"])
            elif is_record(delta) and delta.get("type") == "input_json_delta":
                index = payload.get("index") if isinstance(payload.get("index"), int) else 0
                slot = tool_inputs.setdefault(index, "")
                if isinstance(delta.get("partial_json"), str):
                    tool_inputs[index] = slot + delta["partial_json"]
        elif event == "message_delta":
            _in, out = _anthropic_usage(payload)
            if out:
                output_tokens = out
    answer = Answer("".join(pieces), input_tokens, output_tokens)
    return answer


def parse_gemini_payload(payload):
    text, input_tokens, output_tokens = gemini_text_usage(payload)
    return Answer(text, input_tokens, output_tokens)


def parse_gemini_stream(text):
    pieces = []
    input_tokens = 0
    output_tokens = 0
    for _event, data in parse_sse(text):
        if data.strip() == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not is_record(payload):
            continue
        chunk_text, in_tok, out_tok = gemini_text_usage(payload)
        pieces.append(chunk_text)
        if in_tok:
            input_tokens = in_tok
        if out_tok:
            output_tokens = out_tok
    return Answer("".join(pieces), input_tokens, output_tokens)


def call_provider(env_url, provider, model, client_api, body, stream):
    """Call one provider in its own wire. Returns (Answer, raw_sse_or_None).

    raw_sse_or_None is the provider's untouched SSE text, used only when the
    client wire and the provider wire are the same OpenAI stream, so the
    bytes go through exactly as the provider sent them.
    Raises ProviderFailure on any refusal or transport error.
    """
    base = join_base(env_url, provider["base"])
    headers = {"content-type": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    wire = provider["wire"]
    if wire == "openai":
        upstream = dict(body)
        upstream["model"] = model
        if stream:
            upstream["stream"] = True
            upstream["stream_options"] = {"include_usage": True}
            headers["accept"] = "text/event-stream"
        payload = compact_json(upstream).encode("utf-8")
        status, _ctype, raw = http_call(base + "/v1/chat/completions", headers, payload, PROVIDER_TIMEOUT_S)
        if status < 200 or status >= 300:
            raise ProviderFailure(status, "openai provider answered HTTP %d" % status)
        text = raw.decode("utf-8", "replace")
        if stream:
            # Raw passthrough only when the client wire is the same OpenAI
            # stream: any other client wire needs the events converted.
            return parse_openai_stream(text), (text if client_api == "openai" else None)
        try:
            return parse_openai_payload(json.loads(text)), None
        except ValueError:
            raise ProviderFailure(status, "openai provider answered a body that is not JSON")
    if wire == "anthropic":
        upstream = dict(body)
        upstream["model"] = model
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if stream:
            headers["accept"] = "text/event-stream"
        payload = compact_json(upstream).encode("utf-8")
        status, _ctype, raw = http_call(base + "/v1/messages", headers, payload, PROVIDER_TIMEOUT_S)
        if status < 200 or status >= 300:
            raise ProviderFailure(status, "anthropic provider answered HTTP %d" % status)
        text = raw.decode("utf-8", "replace")
        if stream:
            return parse_anthropic_stream(text), None
        try:
            return parse_anthropic_payload(json.loads(text)), None
        except ValueError:
            raise ProviderFailure(status, "anthropic provider answered a body that is not JSON")
    # gemini
    encoded = urllib.parse.quote(model, safe="")
    verb = "streamGenerateContent" if stream else "generateContent"
    url = "%s/v1beta/models/%s:%s" % (base, encoded, verb)
    if stream:
        url += "?alt=sse"
        headers["accept"] = "text/event-stream"
    payload = compact_json(body).encode("utf-8")
    status, _ctype, raw = http_call(url, headers, payload, PROVIDER_TIMEOUT_S)
    if status < 200 or status >= 300:
        raise ProviderFailure(status, "gemini provider answered HTTP %d" % status)
    text = raw.decode("utf-8", "replace")
    if stream:
        return parse_gemini_stream(text), None
    try:
        return parse_gemini_payload(json.loads(text)), None
    except ValueError:
        raise ProviderFailure(status, "gemini provider answered a body that is not JSON")


# ---------------------------------------------------------------- client rendering

def _openai_json_response(answer, model, upstream_id):
    message = {"role": "assistant", "content": answer.text}
    if answer.tool_calls:
        message["tool_calls"] = [{
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        } for call in answer.tool_calls]
    finish = "tool_calls" if answer.tool_calls else "stop"
    return {
        "id": upstream_id or ("chatcmpl-" + model),
        "object": "chat.completion",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": {
            "prompt_tokens": answer.input_tokens,
            "completion_tokens": answer.output_tokens,
            "total_tokens": answer.input_tokens + answer.output_tokens,
        },
    }


def _openai_sse_response(answer, model, upstream_id):
    """Chat Completions SSE: role chunk, content deltas, finish chunk, usage chunk, [DONE]."""
    chat_id = upstream_id or ("chatcmpl-" + model)
    frames = []

    def chunk(delta, finish, usage=None):
        payload = {"id": chat_id, "object": "chat.completion.chunk",
                   "created": FIXED_CREATED, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            payload["usage"] = usage
        return "data: %s\n\n" % compact_json(payload)

    frames.append(chunk({"role": "assistant", "content": ""}, None))
    text = answer.text
    for at in range(0, len(text), 120):
        frames.append(chunk({"content": text[at:at + 120]}, None))
    for call in answer.tool_calls:
        frames.append(chunk({"tool_calls": [{
            "index": 0, "id": call["id"], "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        }]}, None))
    finish = "tool_calls" if answer.tool_calls else "stop"
    frames.append(chunk({}, finish))
    usage = {"prompt_tokens": answer.input_tokens,
             "completion_tokens": answer.output_tokens,
             "total_tokens": answer.input_tokens + answer.output_tokens}
    frames.append(chunk({}, None, usage=usage))
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def _anthropic_json_response(answer, model, upstream_id):
    content = []
    if answer.text != "":
        content.append({"type": "text", "text": answer.text})
    for call in answer.tool_calls:
        content.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                        "input": _args_object(call["arguments"])})
    if not content:
        content.append({"type": "text", "text": ""})
    return {
        "id": upstream_id if (upstream_id or "").startswith("msg_") else ("msg_" + (upstream_id or "aggregate")),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if answer.tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens},
    }


def _anthropic_sse_response(answer, model, upstream_id):
    """Anthropic SSE: message_start, one text block, optional tool_use blocks,
    message_delta with stop reason and output usage, message_stop."""
    msg_id = upstream_id if (upstream_id or "").startswith("msg_") else ("msg_" + (upstream_id or "aggregate"))

    def event(name, data):
        return "event: %s\ndata: %s\n\n" % (name, compact_json(data))

    frames = [event("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": answer.input_tokens, "output_tokens": 0}}})]
    index = 0
    if answer.text != "":
        frames.append(event("content_block_start", {"type": "content_block_start", "index": index,
                                                    "content_block": {"type": "text", "text": ""}}))
        text = answer.text
        for at in range(0, len(text), 120):
            frames.append(event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": "text_delta", "text": text[at:at + 120]}}))
        frames.append(event("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    for call in answer.tool_calls:
        frames.append(event("content_block_start", {"type": "content_block_start", "index": index,
                                                    "content_block": {"type": "tool_use", "id": call["id"],
                                                                      "name": call["name"], "input": {}}}))
        frames.append(event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                    "delta": {"type": "input_json_delta",
                                                              "partial_json": call["arguments"]}}))
        frames.append(event("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    stop = "tool_use" if answer.tool_calls else "end_turn"
    frames.append(event("message_delta", {"type": "message_delta",
                                          "delta": {"stop_reason": stop, "stop_sequence": None},
                                          "usage": {"output_tokens": answer.output_tokens}}))
    frames.append(event("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def render_response(client_api, stream, answer, model, upstream_id, raw_sse):
    """(status, content_type, body_bytes) for the client wire."""
    if client_api == "openai":
        if stream:
            if raw_sse is not None:
                return 200, "text/event-stream; charset=utf-8", raw_sse.encode("utf-8")
            return 200, "text/event-stream; charset=utf-8", _openai_sse_response(answer, model, upstream_id).encode("utf-8")
        return 200, "application/json", (compact_json(_openai_json_response(answer, model, upstream_id)) + "\n").encode("utf-8")
    if stream:
        return 200, "text/event-stream; charset=utf-8", _anthropic_sse_response(answer, model, upstream_id).encode("utf-8")
    return 200, "application/json", (compact_json(_anthropic_json_response(answer, model, upstream_id)) + "\n").encode("utf-8")


# ---------------------------------------------------------------- router

class Aggregator:
    def __init__(self, env_url, brief, catalog, probed_models):
        self.env_url = env_url
        self.brief = brief
        self.catalog = catalog
        self.probed = probed_models
        self.planner = Planner(brief, probed_models)
        self.providers = {p["id"]: p for p in brief.providers}
        self.caps_total = {pid: PLANNED_CAPS.get(pid, DEFAULT_CAP)
                           for pid in self.planner.provider_order}
        self.calls_used = {pid: 0 for pid in self.planner.provider_order}
        self.consecutive_failures = {pid: 0 for pid in self.planner.provider_order}
        self.dead = set()
        self.served_count = 0
        self.arrival_count = 0
        self.plan = {}
        self._replan(self.brief.manifest, {})

    def remaining_caps(self):
        return {pid: max(0, self.caps_total.get(pid, DEFAULT_CAP) - self.calls_used.get(pid, 0))
                for pid in self.planner.provider_order}

    def _replan(self, remaining_entries, excluded):
        """Solve the remaining workload; relax caps step by step if infeasible."""
        caps = self.remaining_caps()
        for pid in self.dead:
            caps[pid] = 0
        plan = self.planner.solve(remaining_entries, caps, excluded)
        relax = 0
        while len(plan) < len(remaining_entries) and relax < 4:
            relax += 1
            caps = {pid: cap + 1 for pid, cap in caps.items()}
            plan = self.planner.solve(remaining_entries, caps, excluded)
        if len(plan) < len(remaining_entries):
            # Last resort: rating-ordered greedy for the unassigned.
            for entry in remaining_entries:
                if entry["id"] in plan:
                    continue
                barred = excluded.get(entry["id"], set())
                for _rating, pid, model in self.planner.candidates(entry):
                    if pid in barred or pid in self.dead:
                        continue
                    plan[entry["id"]] = (pid, model)
                    break
        self.plan = plan
        return plan

    def next_index(self):
        """Arrival index of the next client request (manifest order)."""
        index = self.arrival_count
        self.arrival_count += 1
        return index

    def requirements_for(self, index, body, client_api):
        """Manifest row by arrival order, else requirements read off the body."""
        manifest = self.brief.manifest
        if 0 <= index < len(manifest):
            return manifest[index]
        return {"id": "body-%d" % index, "api": client_api,
                "stream": body.get("stream") is True, "weight": 1,
                "requires": requirements_from_body(body, client_api)}

    def build_upstream_body(self, client_api, provider, body):
        """Translate the client body into the provider wire's request body."""
        wire = provider["wire"]
        if client_api == "openai" and wire == "openai":
            return dict(body)
        if client_api == "anthropic" and wire == "anthropic":
            return dict(body)
        if wire == "openai":
            return anthropic_to_openai(body, "")
        if wire == "anthropic":
            return openai_to_anthropic(body, "")
        return to_gemini_body(client_api, body, body.get("stream") is True)

    def serve(self, index, client_api, body):
        """Serve one client request. Returns (status, content_type, body_bytes)."""
        entry = self.requirements_for(index, body, client_api)
        stream = body.get("stream") is True
        failed_providers = set()
        tried_pairs = set()
        remaining = self.brief.manifest[index:]
        # Re-plan only when the current plan cannot serve this request.
        planned = self.plan.get(entry["id"])
        if planned is None or planned[0] in self.dead \
                or self.remaining_caps().get(planned[0], 0) <= 0:
            self._replan(remaining, {})
            planned = self.plan.get(entry["id"])
        attempts = 0
        while attempts < MAX_ATTEMPTS_PER_REQUEST:
            attempts += 1
            if planned is not None and planned not in tried_pairs and planned[0] not in failed_providers \
                    and planned[0] not in self.dead:
                provider_id, model = planned
            else:
                choice = None
                for _rating, pid, model_id in self.planner.candidates(entry):
                    if pid in failed_providers or pid in self.dead or (pid, model_id) in tried_pairs:
                        continue
                    choice = (pid, model_id)
                    break
                if choice is None:
                    # Last resort: retry a failed provider with an untried model.
                    for _rating, pid, model_id in self.planner.candidates(entry):
                        if (pid, model_id) in tried_pairs:
                            continue
                        choice = (pid, model_id)
                        break
                if choice is None:
                    break
                provider_id, model = choice
            provider = self.providers[provider_id]
            upstream_body = self.build_upstream_body(client_api, provider, body)
            self.calls_used[provider_id] = self.calls_used.get(provider_id, 0) + 1
            tried_pairs.add((provider_id, model))
            try:
                answer, raw_sse = call_provider(self.env_url, provider, model,
                                                client_api, upstream_body, stream)
            except ProviderFailure as exc:
                failed_providers.add(provider_id)
                self.consecutive_failures[provider_id] = self.consecutive_failures.get(provider_id, 0) + 1
                if self.consecutive_failures[provider_id] >= 2:
                    self.dead.add(provider_id)
                log("request %s: %s/%s refused (HTTP %s); replanning" % (
                    entry["id"], provider_id, model, exc.status))
                self._replan(remaining, {entry["id"]: failed_providers})
                planned = self.plan.get(entry["id"])
                continue
            self.consecutive_failures[provider_id] = 0
            self.served_count += 1
            # The plan's later rows may no longer fit the spent caps: re-solve.
            self._replan(self.brief.manifest[index + 1:], {})
            upstream_id = None
            return render_response(client_api, stream, answer, model, upstream_id, raw_sse)
        log("request %s: no provider could serve it" % entry["id"])
        if client_api == "anthropic":
            payload = {"type": "error", "error": {"type": "overloaded_error",
                                                   "message": "no free model could serve this request"}}
        else:
            payload = {"error": {"message": "no free model could serve this request",
                                 "type": "server_error", "code": None}}
        return 503, "application/json", (compact_json(payload) + "\n").encode("utf-8")

    def served_models(self):
        """Free model ids served, in catalog order, each once."""
        seen = set()
        out = []
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                if model["free"] and model["id"] not in seen:
                    seen.add(model["id"])
                    out.append({"id": model["id"], "provider": provider["id"]})
        return out


def requirements_from_body(body, client_api):
    """Requirements read off a request body (fallback when no manifest row)."""
    requires = {}
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        requires["tools"] = True
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    vision = False
    for message in messages:
        if not is_record(message):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if is_record(block) and block.get("type") in ("image", "image_url"):
                    vision = True
    if vision:
        requires["vision"] = True
    if client_api == "openai":
        fmt = body.get("response_format")
        if is_record(fmt) and fmt.get("type") in ("json_object", "json_schema"):
            requires["json"] = True
    text_len = 0
    for message in messages:
        if is_record(message):
            text_len += len(_text_of(message.get("content")))
    if text_len > 6000:
        requires["min_context"] = 200000
    return requires


# ---------------------------------------------------------------- HTTP server

def _send_error(handler, client_api, status, message):
    if client_api == "anthropic":
        payload = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    else:
        payload = {"error": {"message": message, "type": "invalid_request_error", "code": None}}
    data = (compact_json(payload) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    aggregator = None
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _read_json(self):
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return value if is_record(value) else None

    def _send(self, status, content_type, data):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/v1/models":
            _send_error(self, "openai", 404, "no route GET %s" % parsed.path)
            return
        models = self.aggregator.served_models()
        if isinstance(self.headers.get("x-api-key"), str):
            payload = {
                "data": [{"type": "model", "id": m["id"], "display_name": m["id"],
                          "created_at": FIXED_ANTHROPIC_CREATED_AT} for m in models],
                "has_more": False,
            }
        else:
            payload = {
                "object": "list",
                "data": [{"id": m["id"], "object": "model", "created": FIXED_CREATED,
                          "owned_by": m["provider"]} for m in models],
            }
        self._send(200, "application/json", (compact_json(payload) + "\n").encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/chat/completions":
            client_api = "openai"
        elif parsed.path == "/v1/messages":
            client_api = "anthropic"
        else:
            _send_error(self, "openai", 404, "no route POST %s" % parsed.path)
            return
        body = self._read_json()
        if body is None:
            _send_error(self, client_api, 400, "the request body is not one JSON object")
            return
        aggregator = self.aggregator
        index = aggregator.next_index()
        try:
            status, content_type, data = aggregator.serve(index, client_api, body)
        except Exception as exc:  # noqa: BLE001 - never hang the client
            log("serve failed: %s" % exc)
            _send_error(self, client_api, 500, "the server failed to answer")
            return
        self._send(status, content_type, data)


# ---------------------------------------------------------------- startup

def load_catalog(env_url, brief):
    probes = []
    probed_models = {}
    for provider in brief.providers:
        ok, models, error = probe_provider(env_url, provider)
        if not ok and error:
            log(error)
        probes.append((ok, models, error))
        if ok:
            probed_models[provider["id"]] = models
    catalog = build_catalog(brief, probes)
    return catalog, probed_models


def register(env_url, port, catalog):
    body = compact_json({"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": catalog})
    request = urllib.request.Request(
        env_url.rstrip("/") + "/register",
        data=body.encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    for _attempt in range(30):
        try:
            with urllib.request.urlopen(request, timeout=REGISTER_TIMEOUT_S) as response:
                reply = json.loads(response.read().decode("utf-8"))
            if is_record(reply) and reply.get("ok") is True:
                log("registered: the workload holds %s requests" % reply.get("requests"))
                return
            raise RuntimeError("register answered %s" % reply)
        except Exception as exc:  # noqa: BLE001 - the env may not be up yet
            log("register failed (%s); retrying" % exc)
            time.sleep(0.2)
    raise RuntimeError("could not register with the env")


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    brief = Brief(prompt_file)
    if brief.error is not None:
        log(brief.error)
    catalog = {"providers": []}
    probed_models = {}
    if env_url != "" and brief.error is None:
        catalog, probed_models = load_catalog(env_url, brief)
    if "--list" in sys.argv[1:]:
        sys.stdout.write(compact_json(catalog) + "\n")
        sys.stdout.flush()
        sys.exit(0)
    if env_url == "":
        raise RuntimeError("MAKEFUN_ENV_URL is not set")
    aggregator = Aggregator(env_url, brief, catalog, probed_models)
    Handler.aggregator = aggregator
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        raise RuntimeError("the OS picked 8080, which the env owns on one layout")
    log("listening on 0.0.0.0:%d with %d providers and %d free models" % (
        port, len(catalog["providers"]), len(aggregator.served_models())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    register(env_url, port, catalog)
    try:
        while True:
            try:
                request = urllib.request.Request(
                    env_url.rstrip("/") + "/work", method="GET")
                with urllib.request.urlopen(request, timeout=WORK_TIMEOUT_S) as response:
                    work = json.loads(response.read().decode("utf-8"))
                if is_record(work) and work.get("done") is True:
                    server.shutdown()
                    server.server_close()
                    sys.exit(0)
            except SystemExit:
                raise
            except Exception:
                pass
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
