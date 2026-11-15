# forge — a standalone GitHub-compatible git forge

One process, Python 3.13 standard library only, no UI, no outside service.
It serves git smart HTTP, the REST v3 API, GraphQL v4, and MCP tools from
its own in-memory store.

## Run

```sh
python3 main.py            # serves forever on an ephemeral port (prints it)
FORGE_PORT=8081 python3 main.py
```

In the trial harness the process binds an ephemeral port, registers it with
`$MAKEFUN_ENV_URL/register`, then polls `$MAKEFUN_ENV_URL/done` until the
suite is finished and exits. The entry point is `main.py` at the repository
root; it never binds port 8080 (the suite listens there).

Authentication is a `Bearer` token: in the trial the token is
`$MAKEFUN_SHOP_TOKEN`; without that variable any non-empty bearer is
accepted (local development). `/healthz` answers `{"ok": true}` without a
token.

## What it serves

- **git smart HTTP** — upload-pack protocol v2 (ls-refs, fetch with
  shallow/deepen/thin-pack/ofs-delta) plus the v0 fallback, and
  receive-pack v0 with report-status. Loose objects and packfiles are built
  and parsed in-process (`hashlib` + `zlib`); packs are plain (no deltas).
- **REST v3** under `/api/v3` — repositories, branches, commits, contents
  (create/update/delete with real git objects), labels, issues, comments,
  pull requests (create, files, squash merge with conflict detection).
- **GraphQL v4** at `/api/graphql` — a hand-rolled executor for the
  documents `gh` and `github-mcp-server` send (viewer, repository, issues,
  pull requests, mutations, `node(id:)`, introspection, batched operations).
- **MCP** at `/mcp` — `server/discover` (2026-07-28), `initialize`,
  `tools/list` (8 tools), `tools/call`, stateless JSON-RPC 2.0.

See `NOTES.md` for the architecture, the determinism approach, and the
measured local-runner results.
