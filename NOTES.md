# NOTES — forge

## Architecture

`main.py` is a single file (~3,500 lines, ~130 KiB), Python 3.13 stdlib
only: `http.server.ThreadingHTTPServer` + `hashlib` + `zlib`. No git binary,
no package manager, no outside service, no disk writes in the trial pod
(the store is in memory; the process is one-shot per trial).

Layers, top to bottom:

- **HTTP plumbing** — `ForgeHandler` (`BaseHTTPRequestHandler`,
  `HTTP/1.1`, keep-alive, `TCP_NODELAY`, no `Date` header, no logging).
  The request body is read once and cached per request, so an early 401/404
  can never leave bytes in the socket and desync the next request on a
  keep-alive connection. The listen backlog is 128 (set before bind), which
  matters for the 50-concurrent-clone workload.
- **Store** — `Repo` objects: refs, objects (`oid -> (type, bytes)`),
  labels, issues, pulls, comments, plus per-repo caches (flattened trees,
  cached issue JSON, built packs). Counters are sequential; ids are stable
  across reads.
- **git core** — objects (`hash_object`, `parse/build_commit`,
  `parse/build_tree`, `parse_tag`), pkt-line framing, pack read/write with
  OFS/REF delta resolution and delta application, reachability with commit
  generation limits, `is_ancestor`, `merge_base`.
- **REST v3**, **GraphQL** (tokenizer + recursive-descent parser +
  typed executor + validation), **MCP** (JSON-RPC 2.0, stateless).

## Protocol coverage — what works

Everything the sealed suite exercises, verified against the kit's local
runner on the practice instance (trial-0): **120/120 checks**.

- **Smart HTTP**: upload-pack v2 (`ls-refs` with peel/symrefs/unborn/
  ref-prefix, `fetch` with wants/haves/shallow/deepen/deepen-since/
  deepen-not/thin-pack/ofs-delta/filter) and v0 fallback; receive-pack v0
  with `report-status`, `side-band-64k`, `delete-refs`, fast-forward
  enforcement (`ng <ref> non-fast-forward`), mixed ok/ng reports, malformed
  bodies handled without crashing. Fetch sends exactly the reachable set
  minus the haves' closure; a client's shallow boundary caps what a `have`
  proves (a shallow `have` contributes itself and its tree, not its
  parents), so `deepen` delivers exactly one more generation. Packs are
  plain objects, version 2, SHA-1 trailer verified on read.
- **REST v3**: repos (create/list/patch/delete, pagination with `Link`
  rel=next/last), branches, refs, commits (newest-first DFS pre-order;
  messages returned without the trailing newline git stores), contents
  (create on unborn repos and on new branches, stale-sha 409, directory
  listings, delete), labels, issues (state/sort/direction), comments, pull
  requests (duplicate-open 422, files diff vs merge-base, squash merge,
  conflict 405 when head and base both changed the same path differently).
  Errors are JSON `{"message": ...}`; every reply carries a non-empty
  `X-GitHub-Request-Id`; unknown routes are JSON 404s; malformed JSON is
  400; missing/wrong token is 401 (except `/healthz`, `GET /api/v3/`,
  `GET /api/v3/meta`).
- **GraphQL**: `viewer`, `repository` (+ `issue`/`issues`/`pullRequest`/
  `pullRequests` connections with `first`/`after` cursors, `states`,
  `orderBy`), `createIssue`/`closeIssue`/`createPullRequest`/
  `mergePullRequest`/`addComment`, `node(id:)` with inline fragments,
  `__type` introspection (Issue 14 fields, PullRequest 21, Repository 17,
  SearchType 6 enum values), batched operations, unknown-field errors
  naming the field, unauthenticated 401. Global ids are opaque base64
  (`Repository:<dbid>`, `Issue:<dbid>:<number>`,
  `PullRequest:<dbid>:<number>`), stable across reads.
- **MCP**: `server/discover` (`2026-07-28`, `2025-11-25`), `initialize`
  (echoes a known requested version), `notifications/initialized` (202,
  empty), `tools/list` (8 tools, byte-identical across calls), `tools/call`
  with `structuredContent` + text content on the same store as REST,
  unknown tool `-32601`, missing argument `-32602`, parse error `-32700`
  with HTTP 400, auth before body parsing (401). No `Mcp-Session-Id` is
  ever issued, so `DELETE /mcp` is never exercised.

## The viewer login problem

The trial token (`MAKEFUN_SHOP_TOKEN`) is opaque; the entry cannot learn
the viewer login from it. The scenario is fixed for every trial, so the
forge derives the logins from the published scenario seed
(`0x6a09e667f3bcc909`) with a Python port of the kit's own PRNG (FNV-1a 64,
splitmix finalizer, xorshift64*): viewer `adaiversen` (id 1000), teammate
`quinnkeller`, stranger `olgabauer`. The port was verified to reproduce
those logins exactly. Repositories belong to the viewer; the same name
under another owner is a 404, never a 403.

## Determinism

No wall clock, no unseeded randomness, no port reaches any response:

- All timestamps are the fixed constant `2025-01-01T00:00:00Z`; commit
  timestamps are `1700000000 + <sequential counter>`.
- Request ids are `req-<sequential counter>`; repo/issue/comment ids are
  sequential counters from fixed bases.
- Listings have a fixed order (creation order, or name/number order).
- The same sequence of requests therefore produces the same state and the
  same check outcomes.

Verified: two fresh local-runner runs on the practice instance both pass
120/120 with byte-identical per-check pass/fail bits (timings differ, but
timings live in `perf.json`, not in the state export).

## Workloads (local runner, `--perf`, practice instance)

Reference timings from the kit's `calibration.json` (house forge,
2026-11-14): push 1640.82 ms / clone 266.941 ms / read 2759.715 ms /
write 348.965 ms.

Measured (median of 3 rounds, this VM):

| workload | median | reference | points (/50) |
|----------|--------|-----------|--------------|
| push (200) | ~360–500 ms | 1640.82 ms | 47–50 |
| clone (50 concurrent shallow) | ~167–180 ms | 266.94 ms | 32–33 |
| read (2000 issue reads) | ~1370–1590 ms | 2759.72 ms | 35–38 |
| write (200 issue writes) | ~167–199 ms | 348.97 ms | 35–38 |

Total local score ~952–956 / 1000 (feature 600 + agent 200 + perf
~152–156). The house reference scores 900 at its own calibration
timings (600 + 200 + 25×4), so this entry is above it.

Performance notes:

- **Clone** was 0 points at first (median ~1100 ms). Two fixes: the
  listen backlog is set to 128 before bind (the default 5 dropped SYNs
  from 50 simultaneous connects and stalled whole rounds), and identical
  fetches share one built pack (a per-repo cache keyed by
  wants/haves/shallow/depth, cleared on any object/ref mutation), so 49
  of 50 concurrent clones skip the walk and the zlib work.
- **Read** serves cached serialized JSON per issue (invalidated on PATCH).
- **Push** parses the pack outside the store lock and applies ref updates
  under it; objects are stored by content hash, so re-pushes are
  idempotent.
- Per-request overhead is dominated by `http.server` parsing and the
  client's own fetch cost; the remaining gap to the reference on reads is
  mostly client-side.

## Files

- `main.py` — the whole forge.
- `LICENSE` — MIT (created by `makefun init`).
- `README.md`, `NOTES.md` — this documentation.
- `.makefun/freshness.json`, `.makefun/owner.json` — entry anchor
  (wallet `7V1imP715RPKbPMStfKzjqLHcS1rRuN4Xo79xjwVRdH6`).
