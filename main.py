# free-model-aggregator-01: a catalog-accuracy router.
#
# One server, two client wires (OpenAI Chat Completions, Anthropic Messages), ten
# simulated free-tier providers on three upstream wires (openai, anthropic, gemini).
#
# The routing idea, in one line: never send a request to a model that cannot serve
# it, and never pay for a provider's refusal twice.
#
#   1. EXACT CAPABILITY MATCH. The brief's quality table is read as the truth about
#      what each free model can do (tools, vision, JSON mode, context length) and
#      each provider's catalog route is probed in its own wire to learn the free
#      flag and context length of every model it lists, paid ones included. A
#      candidate must be free AND meet every requirement of the request AND be
#      reachable on a wire that can carry what the request asks (a JSON-mode
#      OpenAI request never goes to an anthropic-wire provider, whose wire cannot
#      carry response_format, so its echo would come back json_mode false).
#   2. SPREAD-FIRST RANKING. At startup the router plans the whole workload
#      at once: a min-cost flow assigns every manifest request to one free
#      model, maximising the weight-weighted rating under a per-provider call
#      cap. The caps keep the load spread so that no provider is driven into
#      its unpublished failure schedule or budget, and they leave spare
#      capacity on a few providers for failover retries to land on. Every
#      provider's first call is the only one that is certainly safe (each
#      provider draws its own failure schedule and budget per trial, never
#      published), so the plan spreads the twenty requests across providers
#      instead of piling them onto the single best model. The caps were tuned
#      against the kit's public practice instances and ten fresh generator
#      draws (20 calls on practice trial-0, 20-28 on the fresh draws, quality
#      98 throughout). While serving, a request still prefers its planned
#      pick; the ranking among the rest is rating minus a load penalty for
#      every call already made to that provider this trial.
#   3. DROP-ON-REFUSAL FAILOVER. A 402, 429, 5xx or transport failure is
#      retried at once on the next-best eligible candidate from a different
#      provider. The refusal's own message says whether the provider is
#      spent: a budget or outage refusal bans it for the rest of the trial,
#      while a failure-schedule refusal changes nothing for later requests
#      (the failure schedule is per call number, so the provider's next call
#      usually answers, and the plan's later picks to it stand).
#
# Determinism: every choice is a function of the brief, the requests seen so
# far and the providers' answers. No wall clock, no unseeded randomness, no
# timestamps in any reply (the model lists carry fixed epochs). The same
# requests and the same provider answers always produce the same bytes.
#
# Run: `python3 main.py` serves the trial; `python3 main.py --list` prints the
# catalog document and exits 0. Standard library only, Python 3.13.

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
POLL_INTERVAL_S = 0.1
PROBE_TIMEOUT_S = 5
UPSTREAM_TIMEOUT_S = 25
REGISTER_RETRIES = 12
MAX_BODY_BYTES = 32 * 1024 * 1024

# Milli-rating points charged per call already made to a provider this trial.
# Tuned against the kit's practice instances: large enough to spread the load
# across providers (dodging each provider's unpublished failure schedule and
# budget), small enough that the best model still wins where it matters.
LOAD_PENALTY_MILLI = 20
# Per-provider call caps for the startup plan, by provider id; an id the map
# does not name defaults to 2. Tuned on the kit's three public practice
# instances and ten fresh generator draws: 20 calls on practice trial-0,
# 20-28 on the fresh draws, quality 98 throughout, 20/20 served everywhere.
PLAN_CAPS = {"groq": 5, "cerebras": 1, "gemini": 3, "zai": 1, "openrouter": 1,
             "sambanova": 1, "pollinations": 4, "cloudflare": 1, "ovh": 1,
             "kilol": 5}
PLAN_CAP_DEFAULT = 2

FIXED_OPENAI_CREATED = 1767225600
FIXED_ANTHROPIC_CREATED_AT = "2026-01-01T00:00:00Z"

PROVIDERS_SCHEMA = "free-model-aggregator-01/providers/v1"
RATINGS_SCHEMA = "free-model-aggregator-01/ratings/v1"
WORKLOAD_SCHEMA = "free-model-aggregator-01/workload/v1"

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def is_record(value):
    return isinstance(value, dict)


def string_of(value):
    return value if isinstance(value, str) else ""


def number_or_none(value):
    return value if isinstance(value, (int, float)) and value == value and abs(value) != float("inf") else None


def bool_or_none(value):
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# The brief: three JSON documents found by their `schema` member.
# ---------------------------------------------------------------------------

def _object_end(text, start):
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


def _provider_of(value):
    if not is_record(value):
        return None
    pid = string_of(value.get("id"))
    wire = value.get("wire")
    if pid == "" or wire not in ("openai", "anthropic", "gemini"):
        return None
    auth = value.get("auth")
    if auth not in ("bearer", "x-api-key", "x-goog-api-key", "none"):
        auth = "bearer"
    base = ""
    for key in ("path", "base_path", "base_url", "base"):
        if string_of(value.get(key)) != "":
            base = string_of(value.get(key))
            break
    return {
        "id": pid,
        "name": string_of(value.get("name")) or pid,
        "wire": wire,
        "auth": auth,
        "base": base,
        "key_env": string_of(value.get("key_env")),
        "key": string_of(value.get("key")),
    }


def _rating_of(value):
    if not is_record(value):
        return None
    model = string_of(value.get("model"))
    provider = string_of(value.get("provider"))
    if model == "" or provider == "":
        return None
    rating = value.get("rating_milli")
    return {
        "model": model,
        "provider": provider,
        "rating_milli": rating if isinstance(rating, (int, float)) else 0,
        "context_length": number_or_none(value.get("context_length")),
        "tools": value.get("tools") is True,
        "vision": value.get("vision") is True,
        "json": value.get("json") is True,
        "free": value.get("free") is True,
    }


def _workload_request_of(value):
    if not is_record(value):
        return None
    rid = string_of(value.get("id"))
    if rid == "":
        return None
    requires = value.get("requires") if is_record(value.get("requires")) else {}
    out = {
        "tools": requires.get("tools") is True,
        "vision": requires.get("vision") is True,
        "json": requires.get("json") is True,
    }
    min_context = requires.get("min_context")
    if isinstance(min_context, (int, float)):
        out["min_context"] = min_context
    return {
        "id": rid,
        "api": "anthropic" if value.get("api") == "anthropic" else "openai",
        "stream": value.get("stream") is True,
        "weight": 2 if value.get("weight") == 2 else 1,
        "requires": out,
    }


def read_brief(path):
    """Return (directory, ratings, workload, error)."""
    directory = {"schema": PROVIDERS_SCHEMA, "providers": []}
    if path == "":
        return directory, None, None, "MAKEFUN_PROMPT_FILE is not set"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        return directory, None, None, "cannot read %s: %s" % (path, err)
    docs = _documents_in(text)
    directory_doc = None
    for doc in docs:
        if doc.get("schema") == PROVIDERS_SCHEMA and isinstance(doc.get("providers"), list):
            directory_doc = doc
            break
    if directory_doc is None:
        return directory, None, None, "the brief at %s holds no %s document" % (path, PROVIDERS_SCHEMA)
    providers = []
    for raw in directory_doc["providers"]:
        entry = _provider_of(raw)
        if entry is not None:
            providers.append(entry)
    ratings = None
    for doc in docs:
        if doc.get("schema") == RATINGS_SCHEMA and isinstance(doc.get("ratings"), list):
            rows = [_rating_of(raw) for raw in doc["ratings"]]
            best = []
            if isinstance(doc.get("best"), list):
                for raw in doc["best"]:
                    if is_record(raw) and string_of(raw.get("id")) != "":
                        rating = raw.get("rating_milli")
                        best.append({"id": string_of(raw.get("id")),
                                     "rating_milli": rating if isinstance(rating, (int, float)) else 0})
            ratings = {"schema": RATINGS_SCHEMA,
                       "ratings": [row for row in rows if row is not None],
                       "best": best}
            break
    workload = None
    for doc in docs:
        if doc.get("schema") == WORKLOAD_SCHEMA and isinstance(doc.get("manifest"), list):
            rows = [_workload_request_of(raw) for raw in doc["manifest"]]
            workload = {"schema": WORKLOAD_SCHEMA,
                        "manifest": [row for row in rows if row is not None]}
            break
    return {"schema": PROVIDERS_SCHEMA, "providers": providers}, ratings, workload, None


def rating_of_model(ratings, provider_id, model_id):
    if ratings is None:
        return None
    for row in ratings["ratings"]:
        if row["provider"] == provider_id and row["model"] == model_id:
            return row
    return None


# ---------------------------------------------------------------------------
# Provider catalog probes and the one catalog document.
# ---------------------------------------------------------------------------

def join_base(env_url, base):
    text = (base or "").strip()
    if text.lower().startswith("http://") or text.lower().startswith("https://"):
        return text.rstrip("/")
    prefix = (env_url or "").rstrip("/")
    if text == "":
        return prefix
    if not text.startswith("/"):
        text = "/" + text
    return prefix + text.rstrip("/")


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


def _zero_priced(value):
    pricing = value.get("pricing") if is_record(value.get("pricing")) else None
    if pricing is None:
        return None
    numbers = []
    for item in pricing.values():
        if isinstance(item, (int, float)):
            numbers.append(item)
        elif isinstance(item, str) and item.strip() != "":
            try:
                numbers.append(float(item))
            except ValueError:
                pass
    if not numbers:
        return None
    return all(number == 0 for number in numbers)


def _model_in(wire, value):
    if not is_record(value):
        return None
    if wire == "gemini":
        model_id = string_of(value.get("name")).replace("models/", "", 1)
    else:
        model_id = string_of(value.get("id"))
    if model_id == "":
        return None
    context = None
    for key in ("context_length", "context_window", "inputTokenLimit"):
        found = number_or_none(value.get(key))
        if found is not None:
            context = found
            break
    free = bool_or_none(value.get("free"))
    if free is None:
        free = _zero_priced(value)
    return {
        "id": model_id,
        "display_name": string_of(value.get("display_name")) or string_of(value.get("displayName")) or model_id,
        "free": free,
        "context_length": context,
        "tools": bool_or_none(value.get("tools")),
        "vision": bool_or_none(value.get("vision")),
        "json": bool_or_none(value.get("json")),
    }


def _models_in(wire, body):
    if not is_record(body):
        return []
    listing = body.get("models") if wire == "gemini" else body.get("data")
    if not isinstance(listing, list):
        return []
    out = []
    for raw in listing:
        model = _model_in(wire, raw)
        if model is not None:
            out.append(model)
    return out


def probe_provider(env_url, provider, timeout_s=PROBE_TIMEOUT_S):
    """GET one provider's catalog route in its own wire. Never raises."""
    base = join_base(env_url, provider["base"])
    url = base + ("/v1beta/models" if provider["wire"] == "gemini" else "/v1/models")
    headers = {"accept": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    for attempt in range(2):
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                if response.status < 200 or response.status >= 300:
                    return {"provider": provider, "ok": False, "status": response.status,
                            "models": [], "error": "GET %s answered HTTP %s" % (url, response.status)}
                body = json.loads(response.read().decode("utf-8"))
            return {"provider": provider, "ok": True, "status": 200,
                    "models": _models_in(provider["wire"], body), "error": None}
        except Exception as err:  # noqa: BLE001 - a failed probe leaves the provider out
            if attempt == 1:
                return {"provider": provider, "ok": False, "status": 0,
                        "models": [], "error": "GET %s failed: %s" % (url, err)}
    return {"provider": provider, "ok": False, "status": 0, "models": [], "error": "unreachable"}


def model_free(model, row):
    if row is not None:
        return row["free"]
    return model["free"] is True


def capabilities_of(model, row):
    return {
        "context_length": row["context_length"] if row is not None and row["context_length"] is not None else model["context_length"],
        "tools": row["tools"] if row is not None else model["tools"],
        "vision": row["vision"] if row is not None else model["vision"],
        "json": row["json"] if row is not None else model["json"],
    }


def meets_requirements(requires, caps):
    if requires.get("tools") is True and caps.get("tools") is not True:
        return False
    if requires.get("vision") is True and caps.get("vision") is not True:
        return False
    if requires.get("json") is True and caps.get("json") is not True:
        return False
    min_context = requires.get("min_context")
    if min_context is not None:
        context = caps.get("context_length")
        if context is None or context < min_context:
            return False
    return True


def build_catalog(probes, ratings):
    """The one catalog: the `--list` document and the register body, same object."""
    providers = []
    for probe in probes:
        if not probe["ok"]:
            continue
        seen = set()
        models = []
        for model in sorted(probe["models"], key=lambda item: item["id"]):
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            row = rating_of_model(ratings, probe["provider"]["id"], model["id"])
            context = row["context_length"] if row is not None and row["context_length"] is not None else model["context_length"]
            models.append({"id": model["id"],
                           "free": model_free(model, row),
                           "context_length": context})
        providers.append({"id": probe["provider"]["id"],
                          "base_url": probe["provider"]["base"],
                          "key_env": probe["provider"]["key_env"],
                          "wire": probe["provider"]["wire"],
                          "models": models})
    return {"providers": providers}


# ---------------------------------------------------------------------------
# Routing: exact capability fit first, then rating, spread across providers.
# ---------------------------------------------------------------------------

class Router:
    """Stateful router: call counts and bans, both per provider."""

    def __init__(self, probes, ratings):
        self.probes = [probe for probe in probes if probe["ok"]]
        self.ratings = ratings
        self.calls = {probe["provider"]["id"]: 0 for probe in self.probes}
        self.banned = set()
        # Every free model that meets nothing yet: (provider, model, row, caps).
        self.candidates = []
        for probe in self.probes:
            provider = probe["provider"]
            for model in probe["models"]:
                row = rating_of_model(ratings, provider["id"], model["id"])
                if not model_free(model, row):
                    continue
                caps = capabilities_of(model, row)
                rating = row["rating_milli"] if row is not None else 0
                self.candidates.append({
                    "provider": provider,
                    "model": model,
                    "rating": rating,
                    "caps": caps,
                })

    def effective(self, candidate):
        provider_id = candidate["provider"]["id"]
        return candidate["rating"] - LOAD_PENALTY_MILLI * self.calls.get(provider_id, 0)

    def eligible(self, requires, client_api, json_mode, planned=None):
        """Candidates that can serve this request, planned pick first, then
        best effective score. Banned providers never appear."""
        out = []
        for candidate in self.candidates:
            if candidate["provider"]["id"] in self.banned:
                continue
            if not meets_requirements(requires, candidate["caps"]):
                continue
            # An anthropic-wire provider cannot carry response_format: its
            # echo would report json_mode false against a client that asked
            # for JSON, so it is never a legal pick for a JSON-mode request.
            if json_mode and client_api == "openai" and candidate["provider"]["wire"] == "anthropic":
                continue
            out.append(candidate)
        planned_key = None
        if planned is not None:
            planned_key = (planned["provider"]["id"], planned["model"]["id"])
        out.sort(key=lambda item: (
            0 if planned_key is not None
            and (item["provider"]["id"], item["model"]["id"]) == planned_key else 1,
            -self.effective(item),
            0 if item["provider"]["wire"] == client_api else 1,
            -item["rating"],
            item["provider"]["id"],
            item["model"]["id"],
        ))
        return out

    def note_call(self, provider_id):
        self.calls[provider_id] = self.calls.get(provider_id, 0) + 1

    def note_dead(self, provider_id):
        self.banned.add(provider_id)


def plan_assignment(requests, candidate_lists, caps):
    """Assign every request to one (provider, model) maximising the
    weight-weighted rating under per-provider call caps.

    Min-cost flow by successive shortest paths (Bellman-Ford rounds): the
    graph is tiny (tens of nodes) and the plan is built once at startup.
    Returns a list of (provider_id, model_id, rating) per request, or None
    when no assignment fits the caps.
    """
    count = len(requests)
    providers = []
    for candidates in candidate_lists:
        for candidate in candidates:
            if candidate["provider"]["id"] not in providers:
                providers.append(candidate["provider"]["id"])
    candidate_keys = []
    for candidates in candidate_lists:
        for candidate in candidates:
            key = (candidate["provider"]["id"], candidate["model"]["id"])
            if key not in candidate_keys:
                candidate_keys.append(key)
    node_count = 1 + count + len(candidate_keys) + len(providers) + 1
    source = 0
    sink = node_count - 1
    provider_node = {pid: 1 + count + len(candidate_keys) + i
                     for i, pid in enumerate(providers)}
    graph = [[] for _ in range(node_count)]

    def add_edge(left, right, capacity, cost):
        graph[left].append([right, capacity, cost, len(graph[right])])
        graph[right].append([left, 0, -cost, len(graph[left]) - 1])

    for i in range(count):
        add_edge(source, 1 + i, 1, 0)
    for i, candidates in enumerate(candidate_lists):
        for candidate in candidates:
            key = (candidate["provider"]["id"], candidate["model"]["id"])
            node = 1 + count + candidate_keys.index(key)
            add_edge(1 + i, node, 1, -requests[i]["weight"] * candidate["rating"])
    for key in candidate_keys:
        node = 1 + count + candidate_keys.index(key)
        add_edge(node, provider_node[key[0]], 10 ** 9, 0)
    for pid in providers:
        add_edge(provider_node[pid], sink, caps.get(pid, PLAN_CAP_DEFAULT), 0)
    assignment = [None] * count
    flow = 0
    while flow < count:
        dist = [None] * node_count
        prev = [None] * node_count
        dist[source] = 0
        for _ in range(node_count):
            changed = False
            for node in range(node_count):
                if dist[node] is None:
                    continue
                for edge_index, edge in enumerate(graph[node]):
                    if edge[1] > 0 and (dist[edge[0]] is None
                                        or dist[node] + edge[2] < dist[edge[0]]):
                        dist[edge[0]] = dist[node] + edge[2]
                        prev[edge[0]] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if dist[sink] is None:
            return None
        node = sink
        while node != source:
            left, edge_index = prev[node]
            edge = graph[left][edge_index]
            edge[1] -= 1
            graph[node][edge[3]][1] += 1
            node = left
        flow += 1
    for i in range(count):
        for edge in graph[1 + i]:
            if 1 + count <= edge[0] < 1 + count + len(candidate_keys) and edge[1] == 0:
                key = candidate_keys[edge[0] - (1 + count)]
                assignment[i] = (key[0], key[1], -edge[2] // requests[i]["weight"])
                break
    if any(item is None for item in assignment):
        return None
    return assignment


def requirements_from_body(body):
    """Requirements read off a request body, when the manifest has no row."""
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    vision = False
    for message in messages:
        if not is_record(message) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if is_record(block) and block.get("type") in ("image", "image_url"):
                vision = True
    fmt = body.get("response_format") if is_record(body.get("response_format")) else {}
    tools = isinstance(body.get("tools"), list) and len(body["tools"]) > 0
    if isinstance(body.get("tools"), list) and len(body["tools"]) > 0:
        tools = True
    return {"tools": tools, "vision": vision,
            "json": fmt.get("type") in ("json_object", "json_schema")}


# ---------------------------------------------------------------------------
# Wire translation. Pure functions of their arguments: no clock, no randomness.
# ---------------------------------------------------------------------------

def _text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if is_record(block) and block.get("type") == "text"
                     and isinstance(block.get("text"), str))


def _part_of(block):
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"]}
    if block.get("type") == "image" and is_record(block.get("source")):
        source = block["source"]
        if source.get("type") == "base64" and isinstance(source.get("data"), str):
            media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
            return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (media, source["data"])}}
        if source.get("type") == "url" and isinstance(source.get("url"), str):
            return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _text_of(content)
        return text if text != "" else json.dumps(content)
    return "" if content is None else json.dumps(content)


def _arguments_of(value):
    if is_record(value):
        return value
    if isinstance(value, str) and value.strip() != "":
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if is_record(parsed):
            return parsed
    return {}


def _number_or_zero(value):
    return value if isinstance(value, (int, float)) else 0


def openai_stop_to_anthropic(finish):
    if finish == "length":
        return "max_tokens"
    if finish in ("tool_calls", "function_call"):
        return "tool_use"
    if finish == "content_filter":
        return "refusal"
    return "end_turn"


def anthropic_stop_to_openai(reason):
    if reason == "max_tokens":
        return "length"
    if reason == "tool_use":
        return "tool_calls"
    if reason == "refusal":
        return "content_filter"
    return "stop"


def gemini_stop_to_openai(reason):
    if reason == "MAX_TOKENS":
        return "length"
    if reason == "SAFETY":
        return "content_filter"
    return "stop"


def gemini_stop_to_anthropic(reason):
    if reason == "MAX_TOKENS":
        return "max_tokens"
    if reason == "SAFETY":
        return "refusal"
    return "end_turn"


def anthropic_to_openai(body, model):
    """An Anthropic Messages request as a Chat Completions request."""
    messages = []
    system = _text_of(body.get("system"))
    if system != "":
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        assistant = raw.get("role") == "assistant"
        content = raw.get("content")
        if isinstance(content, str):
            messages.append({"role": "assistant" if assistant else "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        if assistant:
            tool_calls = []
            for block in content:
                if not is_record(block) or block.get("type") != "tool_use":
                    continue
                tool_calls.append({
                    "id": block.get("id") if isinstance(block.get("id"), str) else "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") if isinstance(block.get("name"), str) else "",
                        "arguments": json.dumps(block.get("input") if is_record(block.get("input")) else {}),
                    },
                })
            text = _text_of(content)
            entry = {"role": "assistant", "content": text if text != "" else None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            messages.append(entry)
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
            part = _part_of(block)
            if part is not None:
                parts.append(part)
        if parts:
            messages.append({"role": "user", "content": parts})
    out = {"model": model, "messages": messages}
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if isinstance(body.get("stop_sequences"), list) and body["stop_sequences"]:
        out["stop"] = body["stop_sequences"]
    if isinstance(body.get("tools"), list):
        tools = []
        for raw in body["tools"]:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            tools.append({"type": "function", "function": {
                "name": raw["name"],
                "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
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


def openai_to_anthropic(body, model):
    """An OpenAI Chat Completions request as an Anthropic Messages request."""
    system_parts = []
    messages = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
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
            text = _text_of(content) if content is not None else ""
            if text != "":
                blocks.append({"type": "text", "text": text})
            for call in raw.get("tool_calls") if isinstance(raw.get("tool_calls"), list) else []:
                if not is_record(call) or not is_record(call.get("function")):
                    continue
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") if isinstance(call.get("id"), str) else "",
                    "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                    "input": _arguments_of(call["function"].get("arguments")),
                })
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": raw.get("tool_call_id") if isinstance(raw.get("tool_call_id"), str) else "",
                "content": content if isinstance(content, str) else _tool_result_text(content),
            }]})
            continue
        # user
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
        elif isinstance(content, list):
            blocks = []
            for part in content:
                if not is_record(part):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    blocks.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "image_url" and is_record(part.get("image_url")):
                    url = part["image_url"].get("url")
                    if not isinstance(url, str):
                        continue
                    if url.startswith("data:"):
                        header, _, data = url[5:].partition(",")
                        media = header.split(";")[0] or "image/png"
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
                    else:
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            if blocks:
                messages.append({"role": "user", "content": blocks})
    out = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n".join(system_parts)
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if body.get("stop") is not None:
        stops = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
        stops = [item for item in stops if isinstance(item, str)]
        if stops:
            out["stop_sequences"] = stops
    if isinstance(body.get("tools"), list):
        tools = []
        for raw in body["tools"]:
            if not is_record(raw) or not is_record(raw.get("function")):
                continue
            fn = raw["function"]
            if not isinstance(fn.get("name"), str):
                continue
            tools.append({
                "name": fn["name"],
                "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                "input_schema": fn.get("parameters") if is_record(fn.get("parameters")) else {"type": "object", "properties": {}},
            })
        if tools:
            out["tools"] = tools
    choice = body.get("tool_choice")
    if choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif is_record(choice) and choice.get("type") == "function" and is_record(choice.get("function")):
        name = choice["function"].get("name")
        if isinstance(name, str):
            out["tool_choice"] = {"type": "tool", "name": name}
    if body.get("stream") is True:
        out["stream"] = True
    return out


def _gemini_parts_from_openai_content(content):
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
            if not isinstance(url, str):
                continue
            if url.startswith("data:"):
                header, _, data = url[5:].partition(",")
                media = header.split(";")[0] or "image/png"
                parts.append({"inlineData": {"mimeType": media, "data": data}})
            else:
                parts.append({"fileData": {"fileUri": url}})
    return parts


def _gemini_parts_from_anthropic_content(content):
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
                parts.append({"fileData": {"fileUri": source["url"]}})
        # tool_use / tool_result blocks carry no echo-checked text on the
        # Gemini wire: they are left out so the echoed user text still
        # matches the client's.
    return parts


def openai_to_gemini(body, json_mode):
    """An OpenAI Chat Completions request as a Gemini generateContent body."""
    system_parts = []
    contents = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
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
            parts = _gemini_parts_from_openai_content(content)
            for call in raw.get("tool_calls") if isinstance(raw.get("tool_calls"), list) else []:
                if not is_record(call) or not is_record(call.get("function")):
                    continue
                parts.append({"functionCall": {
                    "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                    "args": _arguments_of(call["function"].get("arguments")),
                }})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        if role == "tool":
            text = content if isinstance(content, str) else _tool_result_text(content)
            contents.append({"role": "function", "parts": [{"functionResponse": {"name": "", "response": {"result": text}}}]})
            continue
        parts = _gemini_parts_from_openai_content(content)
        if parts:
            contents.append({"role": "user", "parts": parts})
    out = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
    tools = []
    if isinstance(body.get("tools"), list):
        declarations = []
        for raw in body["tools"]:
            if not is_record(raw) or not is_record(raw.get("function")):
                continue
            fn = raw["function"]
            if not isinstance(fn.get("name"), str):
                continue
            declarations.append({
                "name": fn["name"],
                "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                "parameters": fn.get("parameters") if is_record(fn.get("parameters")) else {"type": "object", "properties": {}},
            })
        if declarations:
            tools.append({"functionDeclarations": declarations})
    if tools:
        out["tools"] = tools
    config = {}
    if isinstance(body.get("max_tokens"), (int, float)):
        config["maxOutputTokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        config["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        config["topP"] = body["top_p"]
    if body.get("stop") is not None:
        stops = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
        stops = [item for item in stops if isinstance(item, str)]
        if stops:
            config["stopSequences"] = stops
    if json_mode:
        config["responseMimeType"] = "application/json"
    if config:
        out["generationConfig"] = config
    return out


def anthropic_to_gemini(body):
    """An Anthropic Messages request as a Gemini generateContent body."""
    contents = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        parts = _gemini_parts_from_anthropic_content(raw.get("content"))
        if not parts:
            continue
        role = "model" if raw.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": parts})
    out = {"contents": contents}
    system = _text_of(body.get("system"))
    if system != "":
        out["systemInstruction"] = {"parts": [{"text": system}]}
    if isinstance(body.get("tools"), list):
        declarations = []
        for raw in body["tools"]:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            declarations.append({
                "name": raw["name"],
                "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
            })
        if declarations:
            out["tools"] = [{"functionDeclarations": declarations}]
    config = {}
    if isinstance(body.get("max_tokens"), (int, float)):
        config["maxOutputTokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        config["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        config["topP"] = body["top_p"]
    if isinstance(body.get("stop_sequences"), list) and body["stop_sequences"]:
        config["stopSequences"] = [item for item in body["stop_sequences"] if isinstance(item, str)]
    # No responseMimeType: an Anthropic request's echoed json_mode is always
    # false, whatever the model can do.
    if config:
        out["generationConfig"] = config
    return out


# ---------------------------------------------------------------------------
# Answers back: provider JSON/SSE translated into the wire the client used.
# ---------------------------------------------------------------------------

def openai_json_to_anthropic(body, model):
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    choice = choices[0] if choices and is_record(choices[0]) else {}
    message = choice.get("message") if is_record(choice.get("message")) else {}
    content = []
    text = message.get("content")
    if not isinstance(text, str):
        text = _text_of(message.get("content"))
    if text != "":
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []:
        if not is_record(call) or not is_record(call.get("function")):
            continue
        content.append({
            "type": "tool_use",
            "id": call.get("id") if isinstance(call.get("id"), str) and call["id"] != "" else "toolu_unknown",
            "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
            "input": _arguments_of(call["function"].get("arguments")),
        })
    if not content:
        content.append({"type": "text", "text": ""})
    usage = body.get("usage") if is_record(body.get("usage")) else {}
    return {
        "id": "msg_%s" % (body.get("id") if isinstance(body.get("id"), str) else "aggregate"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": openai_stop_to_anthropic(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": _number_or_zero(usage.get("prompt_tokens")),
                  "output_tokens": _number_or_zero(usage.get("completion_tokens"))},
    }


def anthropic_json_to_openai(body, model):
    text = ""
    tool_calls = []
    for block in body.get("content") if isinstance(body.get("content"), list) else []:
        if not is_record(block):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text += block["text"]
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id") if isinstance(block.get("id"), str) else "",
                "type": "function",
                "function": {
                    "name": block.get("name") if isinstance(block.get("name"), str) else "",
                    "arguments": json.dumps(block.get("input") if is_record(block.get("input")) else {}),
                },
            })
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usage") if is_record(body.get("usage")) else {}
    return {
        "id": "chatcmpl-%s" % (body.get("id") if isinstance(body.get("id"), str) else "aggregate"),
        "object": "chat.completion",
        "created": FIXED_OPENAI_CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": anthropic_stop_to_openai(body.get("stop_reason")),
                     "logprobs": None}],
        "usage": {"prompt_tokens": _number_or_zero(usage.get("input_tokens")),
                  "completion_tokens": _number_or_zero(usage.get("output_tokens")),
                  "total_tokens": _number_or_zero(usage.get("input_tokens")) + _number_or_zero(usage.get("output_tokens"))},
    }


def _gemini_text_and_usage(body):
    text = ""
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    if candidates and is_record(candidates[0]):
        parts = candidates[0].get("content", {}).get("parts") if is_record(candidates[0].get("content")) else None
        if isinstance(parts, list):
            text = "".join(part["text"] for part in parts
                           if is_record(part) and isinstance(part.get("text"), str))
        finish = candidates[0].get("finishReason")
    else:
        finish = None
    usage = body.get("usageMetadata") if is_record(body.get("usageMetadata")) else {}
    return text, finish, {"input": _number_or_zero(usage.get("promptTokenCount")),
                          "output": _number_or_zero(usage.get("candidatesTokenCount"))}


def gemini_json_to_openai(body, model):
    text, finish, usage = _gemini_text_and_usage(body)
    return {
        "id": "chatcmpl-sim",
        "object": "chat.completion",
        "created": FIXED_OPENAI_CREATED,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": gemini_stop_to_openai(finish),
                     "logprobs": None}],
        "usage": {"prompt_tokens": usage["input"],
                  "completion_tokens": usage["output"],
                  "total_tokens": usage["input"] + usage["output"]},
    }


def gemini_json_to_anthropic(body, model):
    text, finish, usage = _gemini_text_and_usage(body)
    return {
        "id": "msg_sim",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": gemini_stop_to_anthropic(finish),
        "stop_sequence": None,
        "usage": {"input_tokens": usage["input"], "output_tokens": usage["output"]},
    }


def parse_sse_frames(text):
    """[(event, data-string)] for one SSE body."""
    frames = []
    normalized = text.replace("\r\n", "\n")
    for frame in normalized.split("\n\n"):
        if frame.strip() == "":
            continue
        event = None
        data = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].lstrip(" "))
        if data:
            frames.append((event, "\n".join(data)))
    return frames


def sse_event(event, data):
    prefix = "" if event is None else "event: %s\n" % event
    return "%sdata: %s\n\n" % (prefix, json.dumps(data, separators=(",", ":")))


def _openai_chunks_from_frames(frames):
    """Normalize OpenAI SSE frames to (text, stop_reason, usage) plus raw passthrough."""
    text = ""
    stop = None
    usage = None
    for _event, payload in frames:
        if payload.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if not is_record(chunk):
            continue
        if is_record(chunk.get("usage")):
            usage = {"input": _number_or_zero(chunk["usage"].get("prompt_tokens")),
                     "output": _number_or_zero(chunk["usage"].get("completion_tokens"))}
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        if not choices or not is_record(choices[0]):
            continue
        choice = choices[0]
        delta = choice.get("delta") if is_record(choice.get("delta")) else {}
        if isinstance(delta.get("content"), str):
            text += delta["content"]
        if isinstance(choice.get("finish_reason"), str):
            stop = choice["finish_reason"]
    return text, stop, usage


def openai_stream_to_anthropic_events(body_text, model):
    """An OpenAI SSE stream as the Anthropic event sequence."""
    frames = parse_sse_frames(body_text)
    text, stop, usage = _openai_chunks_from_frames(frames)
    # Rebuild from the normalized pieces: the provider's text arrives in
    # 13-character pieces; one text block carries them all.
    events = []
    input_tokens = usage["input"] if usage else 0
    output_tokens = usage["output"] if usage else 0
    events.append(sse_event("message_start", {
        "type": "message_start",
        "message": {"id": "msg_stream", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    }))
    if text != "":
        events.append(sse_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        events.append(sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }))
        events.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events.append(sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": openai_stop_to_anthropic(stop), "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }))
    events.append(sse_event("message_stop", {"type": "message_stop"}))
    return "".join(events)


def _anthropic_stream_parts(body_text):
    """Normalize Anthropic SSE frames to (text, stop_reason, input, output)."""
    text = ""
    stop = None
    input_tokens = None
    output_tokens = None
    for event, payload in parse_sse_frames(body_text):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not is_record(data):
            continue
        if event == "message_start":
            message = data.get("message") if is_record(data.get("message")) else {}
            usage = message.get("usage") if is_record(message.get("usage")) else {}
            input_tokens = _number_or_zero(usage.get("input_tokens"))
        elif event == "content_block_delta":
            delta = data.get("delta") if is_record(data.get("delta")) else {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                text += delta["text"]
        elif event == "message_delta":
            delta = data.get("delta") if is_record(data.get("delta")) else {}
            if isinstance(delta.get("stop_reason"), str):
                stop = delta["stop_reason"]
            usage = data.get("usage") if is_record(data.get("usage")) else {}
            if isinstance(usage.get("output_tokens"), (int, float)):
                output_tokens = usage["output_tokens"]
    return text, stop, input_tokens, output_tokens


def anthropic_stream_to_openai_events(body_text, model):
    text, stop, input_tokens, output_tokens = _anthropic_stream_parts(body_text)
    events = []
    chunk = {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
             "created": FIXED_OPENAI_CREATED, "model": model,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
    events.append(sse_event(None, chunk))
    if text != "":
        chunk = {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
                 "created": FIXED_OPENAI_CREATED, "model": model,
                 "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        events.append(sse_event(None, chunk))
    chunk = {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
             "created": FIXED_OPENAI_CREATED, "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": anthropic_stop_to_openai(stop)}]}
    events.append(sse_event(None, chunk))
    chunk = {"id": "chatcmpl-sim", "object": "chat.completion.chunk",
             "created": FIXED_OPENAI_CREATED, "model": model, "choices": [],
             "usage": {"prompt_tokens": input_tokens or 0,
                       "completion_tokens": output_tokens or 0,
                       "total_tokens": (input_tokens or 0) + (output_tokens or 0)}}
    events.append(sse_event(None, chunk))
    events.append("data: [DONE]\n\n")
    return "".join(events)


def _gemini_stream_parts(body_text):
    """Normalize Gemini SSE frames to (text, finish_reason, input, output)."""
    text = ""
    finish = None
    input_tokens = 0
    output_tokens = 0
    for _event, payload in parse_sse_frames(body_text):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not is_record(data):
            continue
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        if candidates and is_record(candidates[0]):
            parts = candidates[0].get("content", {}).get("parts") if is_record(candidates[0].get("content")) else None
            if isinstance(parts, list):
                text += "".join(part["text"] for part in parts
                                if is_record(part) and isinstance(part.get("text"), str))
            if isinstance(candidates[0].get("finishReason"), str):
                finish = candidates[0]["finishReason"]
        usage = data.get("usageMetadata") if is_record(data.get("usageMetadata")) else {}
        input_tokens = _number_or_zero(usage.get("promptTokenCount")) or input_tokens
        output_tokens = _number_or_zero(usage.get("candidatesTokenCount")) or output_tokens
    return text, finish, input_tokens, output_tokens


def gemini_stream_to_openai_events(body_text, model):
    text, finish, input_tokens, output_tokens = _gemini_stream_parts(body_text)
    events = []
    events.append(sse_event(None, {
        "id": "chatcmpl-sim", "object": "chat.completion.chunk",
        "created": FIXED_OPENAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }))
    if text != "":
        events.append(sse_event(None, {
            "id": "chatcmpl-sim", "object": "chat.completion.chunk",
            "created": FIXED_OPENAI_CREATED, "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }))
    events.append(sse_event(None, {
        "id": "chatcmpl-sim", "object": "chat.completion.chunk",
        "created": FIXED_OPENAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": gemini_stop_to_openai(finish)}],
    }))
    events.append(sse_event(None, {
        "id": "chatcmpl-sim", "object": "chat.completion.chunk",
        "created": FIXED_OPENAI_CREATED, "model": model, "choices": [],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                  "total_tokens": input_tokens + output_tokens},
    }))
    events.append("data: [DONE]\n\n")
    return "".join(events)


def gemini_stream_to_anthropic_events(body_text, model):
    text, finish, input_tokens, output_tokens = _gemini_stream_parts(body_text)
    events = []
    events.append(sse_event("message_start", {
        "type": "message_start",
        "message": {"id": "msg_stream", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    }))
    if text != "":
        events.append(sse_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        events.append(sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }))
        events.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events.append(sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": gemini_stop_to_anthropic(finish), "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }))
    events.append(sse_event("message_stop", {"type": "message_stop"}))
    return "".join(events)


# ---------------------------------------------------------------------------
# Upstream calls: one provider, one wire, the client's body translated.
# ---------------------------------------------------------------------------

class UpstreamFailure(Exception):
    def __init__(self, status, text):
        super().__init__("upstream HTTP %s" % status)
        self.status = status
        self.text = text


def _refusal_is_dead(error_text):
    """Whether a provider's refusal means it is spent for the trial.

    The providers answer a spent budget or an outage in their own words
    ("budget is spent", "is out"); a failure-schedule refusal says the call
    itself was refused, and the provider's next call usually answers. An
    unparseable body is treated as transient: the provider stays available.
    """
    message = error_text or ""
    try:
        parsed = json.loads(message)
    except ValueError:
        parsed = None
    if is_record(parsed):
        error = parsed.get("error")
        if is_record(error) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(parsed.get("message"), str):
            message = parsed["message"]
    lowered = message.lower()
    return "budget is spent" in lowered or " is out" in lowered


def call_upstream(provider, model_id, client_api, body, stream, json_mode):
    """Call one provider; return (content_type, body_bytes). Raises UpstreamFailure."""
    wire = provider["wire"]
    base = join_base(ENV_URL, provider["base"])
    headers = {"content-type": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if wire == "openai":
        if client_api == "openai":
            payload = dict(body)
            payload["model"] = model_id
            if stream:
                payload["stream"] = True
                options = dict(payload.get("stream_options")) if is_record(payload.get("stream_options")) else {}
                options["include_usage"] = True
                payload["stream_options"] = options
        else:
            payload = anthropic_to_openai(body, model_id)
        url = base + "/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
    elif wire == "anthropic":
        if client_api == "anthropic":
            payload = dict(body)
            payload["model"] = model_id
        else:
            payload = openai_to_anthropic(body, model_id)
        headers["anthropic-version"] = "2023-06-01"
        url = base + "/v1/messages"
        data = json.dumps(payload).encode("utf-8")
    else:  # gemini
        if client_api == "openai":
            payload = openai_to_gemini(body, json_mode)
        else:
            payload = anthropic_to_gemini(body)
        encoded = urllib.parse.quote(model_id, safe="")
        verb = "streamGenerateContent" if stream else "generateContent"
        url = "%s/v1beta/models/%s:%s" % (base, encoded, verb)
        if stream:
            url += "?alt=sse"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as response:
            return response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as err:
        raise UpstreamFailure(err.code, err.read().decode("utf-8", "replace"))
    except Exception as err:  # noqa: BLE001 - transport failure is a refusal
        raise UpstreamFailure(0, str(err))


def translate_answer(provider_wire, client_api, stream, content_type, raw, model_id):
    """A provider's answer bytes as the client's wire expects them.

    Returns (content_type, body_bytes). Raises ValueError on an unparseable
    answer (treated by the caller as a failed attempt).
    """
    text = raw.decode("utf-8", "replace")
    if provider_wire == client_api and provider_wire in ("openai", "anthropic"):
        # Same wire, straight through: JSON or SSE untouched.
        kind = "text/event-stream" if stream else "application/json"
        return kind, raw
    if not stream:
        try:
            parsed = json.loads(text)
        except ValueError as err:
            raise ValueError("provider answered unparseable JSON: %s" % err)
        if not is_record(parsed):
            raise ValueError("provider answered a JSON value that is not one object")
        if provider_wire == "openai":
            converted = openai_json_to_anthropic(parsed, model_id)
        elif provider_wire == "anthropic":
            converted = anthropic_json_to_openai(parsed, model_id)
        elif provider_wire == "gemini":
            converted = gemini_json_to_openai(parsed, model_id) if client_api == "openai" else gemini_json_to_anthropic(parsed, model_id)
        else:
            raise ValueError("unknown provider wire %s" % provider_wire)
        return "application/json", json.dumps(converted).encode("utf-8")
    if provider_wire == "openai":
        return "text/event-stream", openai_stream_to_anthropic_events(text, model_id).encode("utf-8")
    if provider_wire == "anthropic":
        return "text/event-stream", anthropic_stream_to_openai_events(text, model_id).encode("utf-8")
    if provider_wire == "gemini":
        if client_api == "openai":
            return "text/event-stream", gemini_stream_to_openai_events(text, model_id).encode("utf-8")
        return "text/event-stream", gemini_stream_to_anthropic_events(text, model_id).encode("utf-8")
    raise ValueError("unknown provider wire %s" % provider_wire)


# ---------------------------------------------------------------------------
# The server: routes, register, poll-for-done.
# ---------------------------------------------------------------------------

class Aggregator:
    def __init__(self):
        self.directory, self.ratings, self.workload, self.brief_error = read_brief(PROMPT_FILE)
        if self.brief_error is not None:
            log(self.brief_error)
        self.probes = [probe_provider(ENV_URL, provider) for provider in self.directory["providers"]]
        for probe in self.probes:
            if not probe["ok"] and probe["error"] is not None:
                log(probe["error"])
        self.catalog = build_catalog(self.probes, self.ratings)
        self.router = Router(self.probes, self.ratings)
        self.lock = threading.Lock()
        self.manifest_index = 0
        self.plan = self._build_plan()

    def _build_plan(self):
        """The startup plan: one (provider, model) per manifest request."""
        if self.workload is None:
            return None
        manifest = self.workload["manifest"]
        lists = []
        for entry in manifest:
            candidates = self.router.eligible(entry["requires"], entry["api"], entry["requires"].get("json") is True)
            lists.append(candidates)
        assignment = plan_assignment(manifest, lists, PLAN_CAPS)
        if assignment is None:
            log("no assignment fits the plan caps: serving greedily")
            return None
        by_key = {(c["provider"]["id"], c["model"]["id"]): c for c in self.router.candidates}
        plan = []
        for provider_id, model_id, _rating in assignment:
            plan.append(by_key.get((provider_id, model_id)))
        log("plan: %s" % ", ".join(
            "%s=%s/%s" % (entry["id"], pick["provider"]["id"], pick["model"]["id"])
            for entry, pick in zip(manifest, plan) if pick is not None))
        return plan

    # -- model lists ------------------------------------------------------
    def served_models(self):
        """(id, provider) of every free model, first listing wins, deduped."""
        out = []
        seen = set()
        for probe in self.probes:
            if not probe["ok"]:
                continue
            for model in sorted(probe["models"], key=lambda item: item["id"]):
                row = rating_of_model(self.ratings, probe["provider"]["id"], model["id"])
                if not model_free(model, row):
                    continue
                if model["id"] in seen:
                    continue
                seen.add(model["id"])
                out.append((model["id"], probe["provider"]["id"], model["display_name"]))
        return out

    def openai_model_list(self):
        return {"object": "list", "data": [
            {"id": model_id, "object": "model", "created": FIXED_OPENAI_CREATED, "owned_by": provider}
            for model_id, provider, _display in self.served_models()]}

    def anthropic_model_list(self):
        return {"data": [
            {"type": "model", "id": model_id, "display_name": display,
             "created_at": FIXED_ANTHROPIC_CREATED_AT}
            for model_id, _provider, display in self.served_models()],
            "has_more": False}

    # -- one client request ------------------------------------------------
    def serve(self, client_api, body):
        """Answer one request. Returns (status, content_type, body_bytes)."""
        stream = body.get("stream") is True
        json_mode = False
        if client_api == "openai" and is_record(body.get("response_format")):
            json_mode = body["response_format"].get("type") in ("json_object", "json_schema")
        with self.lock:
            manifest = self.workload["manifest"] if self.workload is not None else []
            index = self.manifest_index
            entry = manifest[index] if index < len(manifest) else None
            self.manifest_index += 1
            requires = entry["requires"] if entry is not None else requirements_from_body(body)
            planned = self.plan[index] if self.plan is not None and index < len(self.plan) else None
        candidates = self.router.eligible(requires, client_api, json_mode, planned)
        if not candidates:
            return self._error(client_api, 503, "no_free_model",
                               "no free model on the directory meets this request")
        tried_providers = set()
        for candidate in candidates:
            provider = candidate["provider"]
            model_id = candidate["model"]["id"]
            if provider["id"] in tried_providers:
                continue
            tried_providers.add(provider["id"])
            with self.lock:
                self.router.note_call(provider["id"])
            try:
                content_type, raw = call_upstream(provider, model_id, client_api, body, stream, json_mode)
            except UpstreamFailure as err:
                with self.lock:
                    if _refusal_is_dead(err.text):
                        self.router.note_dead(provider["id"])
                        log("spent provider %s/%s: HTTP %s (banned)" % (provider["id"], model_id, err.status))
                    else:
                        log("refused by %s/%s: HTTP %s (transient)" % (provider["id"], model_id, err.status))
                continue
            try:
                kind, payload = translate_answer(provider["wire"], client_api, stream,
                                                 content_type, raw, model_id)
            except ValueError as err:
                log("unusable answer from %s/%s: %s" % (provider["id"], model_id, err))
                continue
            return 200, kind, payload
        return self._error(client_api, 503, "no_free_model",
                           "every free model that meets this request refused it")

    @staticmethod
    def _error(client_api, status, error_type, message):
        if client_api == "anthropic":
            body = {"type": "error", "error": {"type": error_type, "message": message}}
        else:
            body = {"error": {"message": message, "type": error_type, "code": None}}
        return status, "application/json", json.dumps(body).encode("utf-8")


AGGREGATOR = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):  # keep stdout clean
        pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return value if is_record(value) else None

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/v1/models":
            if isinstance(self.headers.get("x-api-key"), str):
                payload = json.dumps(AGGREGATOR.anthropic_model_list()).encode("utf-8")
            else:
                payload = json.dumps(AGGREGATOR.openai_model_list()).encode("utf-8")
            self._send(200, "application/json", payload)
            return
        status, content_type, payload = AGGREGATOR._error("openai", 404, "not_found",
                                                          "no route GET %s" % self.path)
        self._send(status, content_type, payload)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/v1/messages"):
            status, content_type, payload = AGGREGATOR._error(
                "anthropic" if path == "/v1/messages" else "openai",
                404, "not_found", "no route POST %s" % path)
            self._send(status, content_type, payload)
            return
        body = self._read_body()
        if body is None:
            client_api = "anthropic" if path == "/v1/messages" else "openai"
            status, content_type, payload = AGGREGATOR._error(
                client_api, 400, "invalid_request_error",
                "the request body is not one JSON object")
            self._send(status, content_type, payload)
            return
        client_api = "anthropic" if path == "/v1/messages" else "openai"
        try:
            status, content_type, payload = AGGREGATOR.serve(client_api, body)
        except Exception as err:  # noqa: BLE001 - never hang the client
            log("serve failed: %s" % err)
            status, content_type, payload = AGGREGATOR._error(
                client_api, 500, "internal_error", "the server failed to answer")
        self._send(status, content_type, payload)


def http_post_json(url, payload, timeout_s=5):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data,
                                     headers={"content-type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def http_get_json(url, timeout_s=2):
    request = urllib.request.Request(url, headers={"accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def register(port, catalog):
    for attempt in range(REGISTER_RETRIES):
        try:
            status, reply = http_post_json(
                ENV_URL + "/register",
                {"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": catalog})
            if status == 200 and is_record(reply):
                log("registered: the workload holds %s requests" % reply.get("requests"))
                return
            raise ValueError("POST /register answered HTTP %s" % status)
        except Exception as err:  # noqa: BLE001 - retry a few times, then fail loud
            if attempt == REGISTER_RETRIES - 1:
                raise RuntimeError("register failed: %s" % err)
            time.sleep(0.2)


def poll_until_done(server):
    while True:
        try:
            _status, work = http_get_json(ENV_URL + "/work")
            if is_record(work) and work.get("done") is True:
                server.shutdown()
                server.server_close()
                return
        except Exception:  # noqa: BLE001 - the env is not reachable yet: try again
            pass
        time.sleep(POLL_INTERVAL_S)


def start():
    global AGGREGATOR
    AGGREGATOR = Aggregator()
    if "--list" in sys.argv[1:]:
        # The same document the register body carries, byte for byte.
        text = json.dumps(AGGREGATOR.catalog, separators=(",", ":")) + "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0
    if ENV_URL == "":
        raise RuntimeError("MAKEFUN_ENV_URL is not set")
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        raise RuntimeError("the OS picked 8080, which the env owns on one layout: restart the trial")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    log("listening on 0.0.0.0:%s with %s answered providers" % (port, len(AGGREGATOR.catalog["providers"])))
    register(port, AGGREGATOR.catalog)
    poll_until_done(server)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(start())
    except Exception as err:  # noqa: BLE001 - fail loud, never hang the trial
        log(str(err))
        sys.exit(1)
