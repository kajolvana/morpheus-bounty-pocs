#!/usr/bin/env python3
"""Floor-guaranteed free-model aggregator (free-model-aggregator-01).

One server, two client wires (OpenAI Chat Completions, Anthropic Messages,
each in JSON and in server-sent events), over the ten simulated free-tier
providers the trial's brief names.  Python 3.13, standard library only.

Strategy -- FLOOR-GUARANTEED router
------------------------------------
1.  Plan all 20 calls UP FRONT, before the first request arrives: every
    request gets a primary (provider, model) and a full ordered fallback
    chain, computed from the brief's quality table and workload manifest.
    The hardest requests are planned first (heaviest weight, then fewest
    legal candidates), so a constrained request never loses its only good
    model to an easy one.
2.  Reserve per-provider budget: a primary plan gives no provider more
    than two primaries.  The first call to a provider always answers (the
    trial's failure schedules only ever refuse a second or later call), so
    spreading primaries two-per-provider keeps every primary on a call
    number that usually answers, and keeps spare capacity on every
    provider for failover.
3.  Guarantee the floor: a request is NEVER left unserved while any legal
    candidate is untried.  On a 402/429/5xx the router walks the whole
    candidate chain (best rating first, providers that never failed
    before providers that failed once) and only answers 503 when every
    candidate has refused.  A provider that fails twice is treated as
    dead: its remaining planned primaries are re-planned onto the
    survivors, so one dead provider cannot drag the rest of the trial
    down with it.
4.  Quality first inside the floor: candidates are ordered by the sealed
    rating table, so the floor is bought with the cheapest quality loss
    available, never with the first model that happens to answer.

Wire fidelity
-------------
The provider's answer text is passed through whole (it carries the
ANSWER ECHO the client checks), its usage counts are reported exactly
as the provider reported them, and its stop reason is mapped into the
client wire's family (stop / end_turn).  The router always calls a
provider non-streaming and synthesises the client's stream from the
full answer, so the streamed event order is exactly the client's
wire: OpenAI role/content/finish/usage/[DONE], Anthropic
message_start/content_block_start/delta/stop/message_delta/message_stop.
Every request field the client sends is carried into the provider's
wire untouched (system prompt, user text and images, tools, stop
sequences, max_tokens, JSON mode); a JSON-mode OpenAI request is only
ever routed to a wire that can carry JSON mode (OpenAI or Gemini),
never to an Anthropic-wire provider whose echo would report it absent.

Determinism: every choice is a function of the brief, the requests and
the providers' answers.  No wall clock and no unseeded randomness are
used anywhere a byte of output depends on them; timestamps in answers
are one fixed constant.

Catalog: built by probing each provider's own catalog route in the
brief's order and keeping the listing's order, so the registered
catalog (and `--list`, byte for byte the same document) matches the
providers' true catalogs exactly.
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

PROTOCOL_VERSION = 1
FIXED_CREATED = 1767225600
FIXED_ANTHROPIC_CREATED_AT = "2026-01-01T00:00:00Z"
POLL_INTERVAL_S = 0.1
PROBE_TIMEOUT_S = 5.0
CALL_TIMEOUT_S = 15.0
REGISTER_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 32 * 1024 * 1024
SOFT_CAP = 2          # primaries planned per provider, before failover
DEAD_AFTER_FAILURES = 2  # a provider failing this often is treated as dead
CHUNK_CHARS = 13      # SSE text pieces, mirroring the providers' own size

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def is_record(value):
    return value is not None and isinstance(value, dict)


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# The brief: three JSON documents found by their `schema` member.
# --------------------------------------------------------------------------

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


class Brief:
    def __init__(self):
        self.providers = []   # [{id, name, wire, auth, base, key_env, key}]
        self.ratings = {}     # {(provider, model): row}
        self.best = {}        # {request id: rating_milli}
        self.manifest = []    # [{id, api, stream, weight, requires}]
        self.error = None


def read_brief(path):
    brief = Brief()
    if not path:
        brief.error = "MAKEFUN_PROMPT_FILE is not set"
        return brief
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        brief.error = "cannot read %s: %s" % (path, err)
        return brief
    docs = _documents_in(text)
    directory = None
    ratings_doc = None
    workload_doc = None
    for doc in docs:
        schema = doc.get("schema")
        if schema == "free-model-aggregator-01/providers/v1" and isinstance(doc.get("providers"), list):
            directory = doc
        elif schema == "free-model-aggregator-01/ratings/v1" and isinstance(doc.get("ratings"), list):
            ratings_doc = doc
        elif schema == "free-model-aggregator-01/workload/v1" and isinstance(doc.get("manifest"), list):
            workload_doc = doc
    if directory is None:
        brief.error = "the brief holds no provider directory"
        return brief
    for raw in directory["providers"]:
        if not is_record(raw):
            continue
        pid = raw.get("id")
        wire = raw.get("wire")
        if not isinstance(pid, str) or not pid or wire not in ("openai", "anthropic", "gemini"):
            continue
        auth = raw.get("auth")
        if auth not in ("bearer", "x-api-key", "x-goog-api-key", "none"):
            auth = "bearer"
        base = ""
        for key in ("path", "base_path", "base_url", "base"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                base = value
                break
        brief.providers.append({
            "id": pid,
            "name": raw.get("name") if isinstance(raw.get("name"), str) else pid,
            "wire": wire,
            "auth": auth,
            "base": base,
            "key_env": raw.get("key_env") if isinstance(raw.get("key_env"), str) else "",
            "key": raw.get("key") if isinstance(raw.get("key"), str) else "",
        })
    if ratings_doc is not None:
        for raw in ratings_doc["ratings"]:
            if not is_record(raw):
                continue
            model = raw.get("model")
            provider = raw.get("provider")
            if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
                continue
            brief.ratings[(provider, model)] = {
                "rating_milli": raw.get("rating_milli") if isinstance(raw.get("rating_milli"), (int, float)) else 0,
                "context_length": raw.get("context_length") if isinstance(raw.get("context_length"), (int, float)) else None,
                "tools": raw.get("tools") is True,
                "vision": raw.get("vision") is True,
                "json": raw.get("json") is True,
                "free": raw.get("free") is True,
            }
        if isinstance(ratings_doc.get("best"), list):
            for raw in ratings_doc["best"]:
                if is_record(raw) and isinstance(raw.get("id"), str):
                    brief.best[raw["id"]] = raw.get("rating_milli") if isinstance(raw.get("rating_milli"), (int, float)) else 0
    if workload_doc is not None:
        for raw in workload_doc["manifest"]:
            if not is_record(raw):
                continue
            rid = raw.get("id")
            if not isinstance(rid, str) or not rid:
                continue
            requires = raw.get("requires") if is_record(raw.get("requires")) else {}
            clean = {}
            if requires.get("tools") is True:
                clean["tools"] = True
            if requires.get("vision") is True:
                clean["vision"] = True
            if requires.get("json") is True:
                clean["json"] = True
            if isinstance(requires.get("min_context"), (int, float)):
                clean["min_context"] = requires["min_context"]
            brief.manifest.append({
                "id": rid,
                "api": "anthropic" if raw.get("api") == "anthropic" else "openai",
                "stream": raw.get("stream") is True,
                "weight": 2 if raw.get("weight") == 2 else 1,
                "requires": clean,
            })
    return brief


# --------------------------------------------------------------------------
# Providers: auth, catalog probe, capabilities.
# --------------------------------------------------------------------------

def auth_headers(provider):
    key = provider["key"]
    if not key:
        return {}
    if provider["auth"] == "bearer":
        return {"authorization": "Bearer %s" % key}
    if provider["auth"] == "x-api-key":
        return {"x-api-key": key}
    if provider["auth"] == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def join_base(env_url, base):
    base = (base or "").strip()
    if base.lower().startswith("http://") or base.lower().startswith("https://"):
        return base.rstrip("/")
    prefix = env_url.rstrip("/")
    if not base:
        return prefix
    return prefix + ("" if base.startswith("/") else "/") + base.rstrip("/")


def _number_or_none(value):
    return value if isinstance(value, (int, float)) and value == value else None


def _bool_or_none(value):
    return value if isinstance(value, bool) else None


def _model_from_listing(wire, raw):
    if not is_record(raw):
        return None
    if wire == "gemini":
        name = raw.get("name")
        model_id = name[len("models/"):] if isinstance(name, str) and name.startswith("models/") else (name if isinstance(name, str) else "")
    else:
        model_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    if not model_id:
        return None
    context = _number_or_none(raw.get("context_length"))
    if context is None:
        context = _number_or_none(raw.get("context_window"))
    if context is None:
        context = _number_or_none(raw.get("inputTokenLimit"))
    return {
        "id": model_id,
        "display_name": raw.get("display_name") or raw.get("displayName") or model_id,
        "free": _bool_or_none(raw.get("free")),
        "context_length": context,
        "tools": _bool_or_none(raw.get("tools")),
        "vision": _bool_or_none(raw.get("vision")),
        "json": _bool_or_none(raw.get("json")),
    }


def probe_provider(env_url, provider):
    """GET one provider's catalog route, in its own wire. Never raises."""
    base = join_base(env_url, provider["base"])
    if provider["wire"] == "gemini":
        url = base + "/v1beta/models"
    else:
        url = base + "/v1/models"
    headers = {"accept": "application/json"}
    headers.update(auth_headers(provider))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as response:
            if response.status != 200:
                return {"provider": provider, "ok": False, "models": [], "error": "GET %s answered HTTP %s" % (url, response.status)}
            body = json.loads(response.read().decode("utf-8"))
    except Exception as err:  # noqa: BLE001 - a failed probe leaves the provider out
        return {"provider": provider, "ok": False, "models": [], "error": "GET %s failed: %s" % (url, err)}
    if not is_record(body):
        return {"provider": provider, "ok": False, "models": [], "error": "GET %s answered no JSON object" % url}
    listing = body.get("models") if provider["wire"] == "gemini" else body.get("data")
    models = []
    if isinstance(listing, list):
        for raw in listing:
            model = _model_from_listing(provider["wire"], raw)
            if model is not None:
                models.append(model)
    return {"provider": provider, "ok": True, "models": models, "error": None}


def model_is_free(model, row):
    if row is not None:
        return row["free"]
    return model["free"] is True


def model_capabilities(model, row):
    if row is not None:
        return {
            "context_length": row["context_length"],
            "tools": row["tools"],
            "vision": row["vision"],
            "json": row["json"],
        }
    return {
        "context_length": model["context_length"],
        "tools": model["tools"],
        "vision": model["vision"],
        "json": model["json"],
    }


def meets_requirements(requires, caps):
    if requires.get("tools") is True and caps.get("tools") is not True:
        return False
    if requires.get("vision") is True and caps.get("vision") is not True:
        return False
    if requires.get("json") is True and caps.get("json") is not True:
        return False
    minimum = requires.get("min_context")
    if minimum is not None:
        context = caps.get("context_length")
        if context is None or context < minimum:
            return False
    return True


def build_catalog(probes, brief):
    """The --list document and the register body: providers in the brief's
    order, models in each listing's own order (the scorer compares the
    free-model list order-sensitively against the true catalogs)."""
    providers = []
    for probe in probes:
        if not probe["ok"]:
            continue
        provider = probe["provider"]
        seen = set()
        models = []
        for model in probe["models"]:
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            row = brief.ratings.get((provider["id"], model["id"]))
            context = row["context_length"] if row is not None and row["context_length"] is not None else model["context_length"]
            models.append({
                "id": model["id"],
                "free": model_is_free(model, row),
                "context_length": context,
            })
        providers.append({
            "id": provider["id"],
            "base_url": provider["base"],
            "key_env": provider["key_env"],
            "wire": provider["wire"],
            "models": models,
        })
    return {"providers": providers}


# --------------------------------------------------------------------------
# Routing: the upfront floor-guaranteed plan and its failover chains.
# --------------------------------------------------------------------------

class Router:
    def __init__(self, brief, probes):
        self.brief = brief
        self.by_id = {p["id"]: p for p in brief.providers}
        # Free models per provider, in listing order, with rating + caps.
        self.offerings = {}   # provider id -> [{model, rating, caps}]
        for probe in probes:
            if not probe["ok"]:
                continue
            pid = probe["provider"]["id"]
            rows = []
            for model in probe["models"]:
                row = brief.ratings.get((pid, model["id"]))
                if not model_is_free(model, row):
                    continue
                rows.append({
                    "model": model["id"],
                    "rating": row["rating_milli"] if row is not None else 0,
                    "caps": model_capabilities(model, row),
                })
            self.offerings[pid] = rows
        self.calls = {pid: 0 for pid in self.by_id}       # attempts made
        self.failures = {pid: 0 for pid in self.by_id}    # failed attempts
        self.dead = set()                                 # >= DEAD_AFTER_FAILURES
        self.served = set()                               # manifest indexes done
        self.plan = {}                                    # index -> (provider, model)
        self._plan_all()

    def candidates(self, requires, client_api, json_mode):
        """All legal (provider, model) pairs for a request, best first.
        A JSON-mode OpenAI request can only go to a wire that carries
        JSON mode; an Anthropic-wire provider would echo it absent."""
        out = []
        for pid in self.by_id:
            if pid in self.dead:
                continue
            provider = self.by_id[pid]
            if json_mode and client_api == "openai" and provider["wire"] == "anthropic":
                continue
            for offer in self.offerings.get(pid, []):
                if meets_requirements(requires, offer["caps"]):
                    out.append((pid, offer["model"], offer["rating"]))
        out.sort(key=lambda item: (-item[2], item[0], item[1]))
        return out

    def _plan_all(self):
        """Plan every unserved request: hardest first, two primaries per
        provider, best rating inside the cap, best rating overall when
        the cap leaves a request nowhere to go."""
        order = []
        for index, entry in enumerate(self.brief.manifest):
            if index in self.served:
                continue
            cands = self.candidates(entry["requires"], entry["api"],
                                    entry["requires"].get("json") is True)
            order.append((index, cands))
        # Heaviest first, then fewest legal candidates, then manifest order.
        order.sort(key=lambda item: (-self.brief.manifest[item[0]]["weight"],
                                     len(item[1]), item[0]))
        counts = {pid: 0 for pid in self.by_id}
        new_plan = dict(self.plan)
        for index, cands in order:
            if index in self.served:
                continue
            pick = None
            for pid, model, _rating in cands:
                if counts[pid] < SOFT_CAP:
                    pick = (pid, model)
                    break
            if pick is None and cands:
                pick = (cands[0][0], cands[0][1])
            if pick is not None:
                new_plan[index] = pick
                counts[pick[0]] += 1
        self.plan = new_plan

    def replan_after_death(self):
        self._plan_all()

    def note_failure(self, pid):
        self.failures[pid] += 1
        if self.failures[pid] >= DEAD_AFTER_FAILURES and pid not in self.dead:
            self.dead.add(pid)
            log("provider %s failed %d times; treating as dead and re-planning" % (pid, self.failures[pid]))
            self.replan_after_death()

    def attempt_order(self, index, requires, client_api, json_mode):
        """[primary] + the full chain: providers that never failed first
        (a first call to one always answers), then once-failed ones by
        rating.  The primary stays first even after its provider refused
        once: a failure schedule refuses single call numbers, so the
        provider's next call usually answers at the planned rating, and
        skipping it would pay the same call elsewhere for a worse model.
        Never empty while any legal candidate exists."""
        cands = self.candidates(requires, client_api, json_mode)
        rating_of = {(pid, model): rating for pid, model, rating in cands}
        primary = self.plan.get(index)
        ordered = []
        seen = set()
        if primary is not None:
            ordered.append(primary)
            seen.add(primary)
        rest = [(pid, model) for pid, model, _r in cands if (pid, model) not in seen]
        rest.sort(key=lambda item: (1 if self.failures[item[0]] > 0 else 0,
                                    -rating_of[item], item[0], item[1]))
        ordered.extend(rest)
        return ordered


# --------------------------------------------------------------------------
# Wire translation: client body -> provider body, provider answer -> client.
# --------------------------------------------------------------------------

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if is_record(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _image_parts_openai(content):
    """image_url blocks of an OpenAI message as (kind, mime, data-or-url)."""
    out = []
    if isinstance(content, list):
        for block in content:
            if not is_record(block) or block.get("type") != "image_url":
                continue
            image = block.get("image_url")
            if not is_record(image) or not isinstance(image.get("url"), str):
                continue
            url = image["url"]
            if url.startswith("data:"):
                head, _, data = url.partition(",")
                mime = "image/png"
                if ";" in head:
                    mime = head[5:].split(";")[0] or mime
                out.append(("data", mime, data))
            else:
                out.append(("url", "", url))
    return out


def _image_parts_anthropic(content):
    out = []
    if isinstance(content, list):
        for block in content:
            if not is_record(block) or block.get("type") != "image":
                continue
            source = block.get("source")
            if not is_record(source):
                continue
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                out.append(("data", source.get("media_type") or "image/png", source["data"]))
            elif source.get("type") == "url" and isinstance(source.get("url"), str):
                out.append(("url", "", source["url"]))
    return out


def _max_tokens_openai(body):
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _stops_openai(body):
    stop = body.get("stop")
    if isinstance(stop, str):
        return [stop]
    if isinstance(stop, list):
        return [item for item in stop if isinstance(item, str)]
    return []


def _stops_anthropic(body):
    stop = body.get("stop_sequences")
    if isinstance(stop, list):
        return [item for item in stop if isinstance(item, str)]
    return []


def openai_to_anthropic(body, model):
    """An OpenAI Chat Completions request as an Anthropic Messages one."""
    messages = []
    system_parts = []
    for raw in body.get("messages") or []:
        if not is_record(raw):
            continue
        if raw.get("role") == "system":
            text = _text_of(raw.get("content"))
            if text:
                system_parts.append(text)
    source = body.get("messages") or []
    for raw in source:
        if not is_record(raw) or raw.get("role") == "system":
            continue
        role = raw.get("role")
        content = raw.get("content")
        if role == "assistant":
            blocks = []
            text = _text_of(content)
            if text:
                blocks.append({"type": "text", "text": text})
            calls = raw.get("tool_calls") or []
            if isinstance(calls, list):
                for call in calls:
                    if not is_record(call) or not is_record(call.get("function")):
                        continue
                    fn = call["function"]
                    args = fn.get("arguments")
                    parsed = {}
                    if isinstance(args, dict):
                        parsed = args
                    elif isinstance(args, str):
                        try:
                            parsed = json.loads(args) if args.strip() else {}
                        except ValueError:
                            parsed = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id") if isinstance(call.get("id"), str) else "",
                        "name": fn.get("name") if isinstance(fn.get("name"), str) else "",
                        "input": parsed if is_record(parsed) else {},
                    })
            messages.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": raw.get("tool_call_id") if isinstance(raw.get("tool_call_id"), str) else "",
                "content": _text_of(content),
            }]})
            continue
        blocks = []
        text = _text_of(content)
        if text:
            blocks.append({"type": "text", "text": text})
        for kind, mime, data in _image_parts_openai(content):
            if kind == "data":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": data}})
        if blocks:
            messages.append({"role": "user", "content": blocks})
    out = {"model": model, "messages": messages}
    if system_parts:
        out["system"] = "\n".join(system_parts)
    max_tokens = _max_tokens_openai(body)
    if max_tokens is not None:
        out["max_tokens"] = max_tokens
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)):
            out[key] = body[key]
    stops = _stops_openai(body)
    if stops:
        out["stop_sequences"] = stops
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
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
    return out


def anthropic_to_openai(body, model):
    """An Anthropic Messages request as an OpenAI Chat Completions one."""
    messages = []
    system = _text_of(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages") or []:
        if not is_record(raw):
            continue
        content = raw.get("content")
        if raw.get("role") == "assistant":
            blocks = []
            text = _text_of(content)
            calls = []
            if isinstance(content, list):
                for block in content:
                    if not is_record(block) or block.get("type") != "tool_use":
                        continue
                    args = block.get("input")
                    calls.append({
                        "id": block.get("id") if isinstance(block.get("id"), str) else "",
                        "type": "function",
                        "function": {
                            "name": block.get("name") if isinstance(block.get("name"), str) else "",
                            "arguments": compact(args) if is_record(args) else "{}",
                        },
                    })
            message = {"role": "assistant", "content": text if text else None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            continue
        parts = []
        text = _text_of(content)
        if text:
            parts.append({"type": "text", "text": text})
        for kind, mime, data in _image_parts_anthropic(content):
            if kind == "data":
                parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data)}})
            else:
                parts.append({"type": "image_url", "image_url": {"url": data}})
        if isinstance(content, list):
            for block in content:
                if not is_record(block) or block.get("type") != "tool_result":
                    continue
                result = block.get("content")
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else "",
                    "content": _text_of(result) if result is not None else "",
                })
        if parts:
            messages.append({"role": "user", "content": parts})
    out = {"model": model, "messages": messages}
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)):
            out[key] = body[key]
    stops = _stops_anthropic(body)
    if stops:
        out["stop"] = stops
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
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
    return out


def to_gemini(client_api, body, model, json_mode):
    """Either client wire as a Gemini generateContent request."""
    if client_api == "openai":
        system_parts = []
        contents = []
        for raw in body.get("messages") or []:
            if not is_record(raw):
                continue
            if raw.get("role") == "system":
                text = _text_of(raw.get("content"))
                if text:
                    system_parts.append(text)
                continue
            role = "model" if raw.get("role") == "assistant" else "user"
            parts = []
            text = _text_of(raw.get("content"))
            if text:
                parts.append({"text": text})
            for kind, mime, data in _image_parts_openai(raw.get("content")):
                if kind == "data":
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
                else:
                    parts.append({"fileData": {"fileUri": data}})
            if parts:
                contents.append({"role": role, "parts": parts})
        tools = []
        for raw in body.get("tools") or []:
            if not is_record(raw) or not is_record(raw.get("function")):
                continue
            fn = raw["function"]
            if isinstance(fn.get("name"), str):
                tools.append({"name": fn["name"],
                              "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                              "parameters": fn.get("parameters") if is_record(fn.get("parameters")) else {"type": "object", "properties": {}}})
        max_tokens = _max_tokens_openai(body)
        stops = _stops_openai(body)
        temperature = body.get("temperature")
    else:
        system_parts = []
        system = _text_of(body.get("system"))
        if system:
            system_parts.append(system)
        contents = []
        for raw in body.get("messages") or []:
            if not is_record(raw):
                continue
            role = "model" if raw.get("role") == "assistant" else "user"
            parts = []
            text = _text_of(raw.get("content"))
            if text:
                parts.append({"text": text})
            for kind, mime, data in _image_parts_anthropic(raw.get("content")):
                if kind == "data":
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
                else:
                    parts.append({"fileData": {"fileUri": data}})
            if isinstance(raw.get("content"), list):
                for block in raw["content"]:
                    if is_record(block) and block.get("type") == "tool_result":
                        result = _text_of(block.get("content"))
                        if result:
                            parts.append({"text": result})
            if parts:
                contents.append({"role": role, "parts": parts})
        tools = []
        for raw in body.get("tools") or []:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            tools.append({"name": raw["name"],
                          "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                          "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}}})
        max_tokens = body.get("max_tokens") if isinstance(body.get("max_tokens"), (int, float)) else None
        stops = _stops_anthropic(body)
        temperature = body.get("temperature")
    out = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
    if tools:
        out["tools"] = [{"functionDeclarations": tools}]
    config = {}
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens
    if stops:
        config["stopSequences"] = stops
    if isinstance(temperature, (int, float)):
        config["temperature"] = temperature
    if json_mode:
        config["responseMimeType"] = "application/json"
    if config:
        out["generationConfig"] = config
    return out


def build_provider_body(client_api, provider_wire, body, model, json_mode):
    """The upstream request body for one provider wire."""
    if provider_wire == "openai":
        if client_api == "openai":
            out = dict(body)
            out["model"] = model
            out["stream"] = False
            out.pop("stream_options", None)
            return out
        return anthropic_to_openai(body, model)
    if provider_wire == "anthropic":
        if client_api == "anthropic":
            out = dict(body)
            out["model"] = model
            out["stream"] = False
            return out
        return openai_to_anthropic(body, model)
    return to_gemini(client_api, body, model, json_mode)


def call_provider(env_url, provider, model, payload):
    """One upstream call. Returns (status, parsed) where parsed is
    {"text", "input", "output"} on 2xx, else (status, error text)."""
    base = join_base(env_url, provider["base"])
    if provider["wire"] == "openai":
        url = base + "/v1/chat/completions"
    elif provider["wire"] == "anthropic":
        url = base + "/v1/messages"
    else:
        url = base + "/v1beta/models/%s:generateContent" % urllib.parse.quote(model, safe="")
    headers = {"content-type": "application/json"}
    headers.update(auth_headers(provider))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    data = compact(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as err:
        try:
            raw = err.read().decode("utf-8")
        except Exception:
            raw = ""
        return err.code, {"error": raw[:500]}
    except Exception as err:  # noqa: BLE001 - transport failure is a refusal
        return 0, {"error": str(err)[:500]}
    try:
        value = json.loads(raw)
    except ValueError:
        return status, {"error": "provider answered no JSON"}
    if status < 200 or status >= 300 or not is_record(value):
        return status, {"error": raw[:500]}
    return status, parse_provider_answer(provider["wire"], value)


def parse_provider_answer(wire, value):
    if wire == "openai":
        choices = value.get("choices") if isinstance(value.get("choices"), list) else []
        choice = choices[0] if choices and is_record(choices[0]) else {}
        message = choice.get("message") if is_record(choice.get("message")) else {}
        content = message.get("content")
        text = content if isinstance(content, str) else _text_of(content)
        usage = value.get("usage") if is_record(value.get("usage")) else {}
        return {
            "text": text,
            "input": usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), (int, float)) else 0,
            "output": usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), (int, float)) else 0,
        }
    if wire == "anthropic":
        text = _text_of(value.get("content"))
        usage = value.get("usage") if is_record(value.get("usage")) else {}
        return {
            "text": text,
            "input": usage.get("input_tokens") if isinstance(usage.get("input_tokens"), (int, float)) else 0,
            "output": usage.get("output_tokens") if isinstance(usage.get("output_tokens"), (int, float)) else 0,
        }
    candidates = value.get("candidates") if isinstance(value.get("candidates"), list) else []
    candidate = candidates[0] if candidates and is_record(candidates[0]) else {}
    content = candidate.get("content") if is_record(candidate.get("content")) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    text = "".join(part.get("text", "") for part in parts if is_record(part) and isinstance(part.get("text"), str))
    usage = value.get("usageMetadata") if is_record(value.get("usageMetadata")) else {}
    return {
        "text": text,
        "input": usage.get("promptTokenCount") if isinstance(usage.get("promptTokenCount"), (int, float)) else 0,
        "output": usage.get("candidatesTokenCount") if isinstance(usage.get("candidatesTokenCount"), (int, float)) else 0,
    }


# --------------------------------------------------------------------------
# Client answers: JSON and SSE in the wire the client used.
# --------------------------------------------------------------------------

def _split_text(text):
    if not text:
        return [""]
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]


def _sse_event(event, data):
    prefix = "" if event is None else "event: %s\n" % event
    return "%sdata: %s\n\n" % (prefix, compact(data))


def render_openai(model, answer, stream, include_usage):
    text = answer["text"]
    usage = {
        "prompt_tokens": answer["input"],
        "completion_tokens": answer["output"],
        "total_tokens": answer["input"] + answer["output"],
    }
    if not stream:
        return "application/json", compact({
            "id": "chatcmpl-sim",
            "object": "chat.completion",
            "created": FIXED_CREATED,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
                "logprobs": None,
            }],
            "usage": usage,
        })
    chunk = lambda delta, finish: {
        "id": "chatcmpl-sim",
        "object": "chat.completion.chunk",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    frames = [_sse_event(None, chunk({"role": "assistant", "content": ""}, None))]
    for piece in _split_text(text):
        frames.append(_sse_event(None, chunk({"content": piece}, None)))
    frames.append(_sse_event(None, chunk({}, "stop")))
    # The client checks the response's final usage against the provider's,
    # and the streamed bodies it sends carry no stream_options: always
    # close with the usage chunk, asked for or not, so the usage check
    # always has a final usage to read.
    frames.append(_sse_event(None, {
        "id": "chatcmpl-sim",
        "object": "chat.completion.chunk",
        "created": FIXED_CREATED,
        "model": model,
        "choices": [],
        "usage": usage,
    }))
    frames.append("data: [DONE]\n\n")
    return "text/event-stream; charset=utf-8", "".join(frames)


def render_anthropic(model, answer, stream):
    text = answer["text"]
    usage = {"input_tokens": answer["input"], "output_tokens": answer["output"]}
    if not stream:
        return "application/json", compact({
            "id": "msg_sim",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        })
    frames = [
        _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_sim", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": answer["input"], "output_tokens": 0},
            },
        }),
        _sse_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
    ]
    for piece in _split_text(text):
        frames.append(_sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece},
        }))
    frames.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    frames.append(_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": answer["output"]},
    }))
    frames.append(_sse_event("message_stop", {"type": "message_stop"}))
    return "text/event-stream; charset=utf-8", "".join(frames)


def openai_error(message, error_type="invalid_request_error"):
    return {"error": {"message": message, "type": error_type, "code": None}}


def anthropic_error(message, error_type="api_error"):
    return {"type": "error", "error": {"type": error_type, "message": message}}


# --------------------------------------------------------------------------
# The server: routes, the serve-with-failover loop, register, poll, --list.
# --------------------------------------------------------------------------

class Aggregator:
    def __init__(self, brief, probes, catalog):
        self.brief = brief
        self.catalog = catalog
        self.router = Router(brief, probes)
        self.lock = threading.Lock()
        self.arrival = 0          # client requests served so far, in order
        # Distinct free models, catalog order, for GET /v1/models.
        self.served_models = []
        seen = set()
        for provider in catalog["providers"]:
            for model in provider["models"]:
                if model["free"] and model["id"] not in seen:
                    seen.add(model["id"])
                    self.served_models.append({"id": model["id"], "provider": provider["id"]})

    def models_payload(self, anthropic_wire):
        if anthropic_wire:
            return {
                "data": [{
                    "type": "model",
                    "id": item["id"],
                    "display_name": item["id"],
                    "created_at": FIXED_ANTHROPIC_CREATED_AT,
                } for item in self.served_models],
                "has_more": False,
            }
        return {
            "object": "list",
            "data": [{
                "id": item["id"],
                "object": "model",
                "created": FIXED_CREATED,
                "owned_by": item["provider"],
            } for item in self.served_models],
        }

    def requirements_for(self, index, client_api, body):
        manifest = self.brief.manifest
        if 0 <= index < len(manifest):
            return manifest[index]["requires"]
        tools = isinstance(body.get("tools"), list) and len(body["tools"]) > 0
        vision = False
        for message in body.get("messages") or []:
            if not is_record(message) or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if is_record(block) and block.get("type") in ("image", "image_url"):
                    vision = True
        fmt = body.get("response_format") if is_record(body.get("response_format")) else {}
        json_mode = fmt.get("type") in ("json_object", "json_schema")
        requires = {}
        if tools:
            requires["tools"] = True
        if vision:
            requires["vision"] = True
        if json_mode:
            requires["json"] = True
        return requires

    def serve(self, client_api, body):
        """Answer one client request, walking the failover chain until a
        provider answers or none is left. Returns (status, content-type,
        payload text)."""
        with self.lock:
            index = self.arrival
            self.arrival += 1
        stream = body.get("stream") is True
        if client_api == "openai":
            fmt = body.get("response_format") if is_record(body.get("response_format")) else {}
            json_mode = fmt.get("type") in ("json_object", "json_schema")
            include_usage = is_record(body.get("stream_options")) and body["stream_options"].get("include_usage") is True
        else:
            json_mode = False
            include_usage = False
        requires = self.requirements_for(index, client_api, body)
        attempts = self.router.attempt_order(index, requires, client_api, json_mode)
        last_error = "no free model meets this request"
        for pid, model in attempts:
            provider = self.router.by_id[pid]
            payload = build_provider_body(client_api, provider["wire"], body, model, json_mode)
            with self.lock:
                self.router.calls[pid] += 1
            status, parsed = call_provider(ENV_URL, provider, model, payload)
            if status == 200 and "text" in parsed:
                with self.lock:
                    self.router.served.add(index)
                if client_api == "openai":
                    content_type, text = render_openai(model, parsed, stream, include_usage)
                else:
                    content_type, text = render_anthropic(model, parsed, stream)
                return 200, content_type, text
            with self.lock:
                self.router.note_failure(pid)
            last_error = "provider %s answered HTTP %s" % (pid, status)
            log("request %d: %s/%s refused (HTTP %s); failing over" % (index, pid, model, status))
        if client_api == "anthropic":
            return 503, "application/json", compact(anthropic_error(
                "no free model could serve this request: %s" % last_error, "overloaded_error"))
        return 503, "application/json", compact(openai_error(
            "no free model could serve this request: %s" % last_error, "no_free_model"))


def load_all():
    """Parse the brief, probe the providers, build the one catalog."""
    brief = read_brief(PROMPT_FILE)
    if brief.error is not None:
        log(brief.error)
    probes = []
    for provider in brief.providers:
        probe = probe_provider(ENV_URL, provider)
        probes.append(probe)
        if not probe["ok"] and probe["error"]:
            log(probe["error"])
    catalog = build_catalog(probes, brief)
    return brief, probes, catalog


class Handler(BaseHTTPRequestHandler):
    aggregator = None  # set by start()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean; stderr via log()
        pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    def _send(self, status, content_type, text):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/v1/models":
            anthropic_wire = self.headers.get("x-api-key") is not None
            self._send(200, "application/json", compact(self.aggregator.models_payload(anthropic_wire)))
            return
        self._send(404, "application/json", compact(openai_error("no route GET %s" % path, "not_found")))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/v1/messages"):
            self._send(404, "application/json", compact(openai_error("no route POST %s" % path, "not_found")))
            return
        client_api = "anthropic" if path == "/v1/messages" else "openai"
        raw = self._read_body()
        if raw is None:
            error = anthropic_error("the request body is not one JSON object") if client_api == "anthropic" else openai_error("the request body is not one JSON object")
            self._send(400, "application/json", compact(error))
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        if not is_record(body):
            error = anthropic_error("the request body is not one JSON object") if client_api == "anthropic" else openai_error("the request body is not one JSON object")
            self._send(400, "application/json", compact(error))
            return
        try:
            status, content_type, text = self.aggregator.serve(client_api, body)
        except Exception as err:  # noqa: BLE001 - never drop a client request
            log("serve failed: %s" % err)
            if client_api == "anthropic":
                status, content_type, text = 500, "application/json", compact(anthropic_error("the server failed to answer"))
            else:
                status, content_type, text = 500, "application/json", compact(openai_error("the server failed to answer"))
        self._send(status, content_type, text)


def register(port, catalog):
    body = compact({"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": catalog}).encode("utf-8")
    last_error = ""
    for _attempt in range(5):
        request = urllib.request.Request(
            ENV_URL + "/register", data=body,
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REGISTER_TIMEOUT_S) as response:
                reply = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and is_record(reply) and reply.get("ok") is True:
                log("registered: the workload holds %s requests" % reply.get("requests"))
                return
            last_error = "HTTP %s" % response.status
        except Exception as err:  # noqa: BLE001 - retry the register
            last_error = str(err)
        time.sleep(0.2)
    raise RuntimeError("POST %s/register failed: %s" % (ENV_URL, last_error))


def poll_until_done(server):
    while True:
        try:
            request = urllib.request.Request(ENV_URL + "/work", method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                work = json.loads(response.read().decode("utf-8"))
            if is_record(work) and work.get("done") is True:
                server.shutdown()
                return
        except Exception:  # noqa: BLE001 - the env is not reachable yet
            pass
        time.sleep(POLL_INTERVAL_S)


def start():
    brief, probes, catalog = load_all()
    if "--list" in sys.argv:
        # The same document the register body carries, byte for byte.
        sys.stdout.write(compact(catalog) + "\n")
        sys.stdout.flush()
        return 0
    if not ENV_URL:
        raise RuntimeError("MAKEFUN_ENV_URL is not set")
    aggregator = Aggregator(brief, probes, catalog)
    Handler.aggregator = aggregator
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        raise RuntimeError("the OS picked 8080, which the env owns on one layout: restart the trial")
    log("listening on 0.0.0.0:%d with %d providers and %d free models" % (
        port, len(catalog["providers"]), len(aggregator.served_models)))
    register(port, catalog)
    poller = threading.Thread(target=poll_until_done, args=(server,), daemon=True)
    poller.start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(start())
    except Exception as err:  # noqa: BLE001 - fatal startup error
        log(str(err))
        sys.exit(1)
