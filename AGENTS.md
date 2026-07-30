# AGENTS.md

Guidance for coding agents working in this repository. If you are a human,
`README.md` covers install and usage instead.

## What this is

A dependency-free MCP (Model Context Protocol) **stdio server** that exposes
three read-only tools — `so_search`, `so_get_question`, `so_get_answers` —
backed by the classic Stack Exchange REST API (`api.stackexchange.com/2.3`).
It exists because the official hosted MCP server (`mcp.stackoverflow.com`)
sits behind a Cloudflare managed challenge and returns HTTP 403 to any
headless (non-browser) client. This project sidesteps that by talking
directly to the bot-friendly classic REST API instead, which needs no OAuth
for reads.

The whole server is a single file, `server.py`, tested by `test_server.py`.

## Commands

There is no build step and no linter configured. Everything goes through
`unittest`.

```sh
# full suite
python3 -m unittest test_server -v

# a single test class or test method
python3 -m unittest test_server.TestDispatch -v
python3 -m unittest test_server.TestDispatch.test_initialize_echoes_protocol_version -v

# syntax-only check (what CI runs before the suite)
python3 -m py_compile server.py test_server.py
```

The suite mocks the network seam (`_http_get_json`) by default, so it runs
fully offline. There is also an **opt-in live test** that hits the real
Stack Exchange API and **consumes quota**:

```sh
SO_MCP_LIVE=1 python3 -m unittest test_server.TestLive -v
```

Only run the live test deliberately, not as part of a routine local loop —
the unauthenticated quota is 300 requests/day per IP, shared with whoever
else on the network is testing against the same API.

### Manual protocol smoke test

The server speaks JSON-RPC 2.0 over stdio, one message per line. You can
drive it by hand with a pipe:

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 server.py
```

Each line of output should be exactly one JSON object — a valid response to
the corresponding request. If you see anything else on stdout (a stack
trace, a bare print, blank lines mixed with JSON), that is a protocol
violation; see invariant 1 below.

## The five invariants

These are not style preferences — breaking any of them makes the server
unusable by a real MCP client, often in a way that is silent or misleading
until someone actually plugs it into Claude Code/Desktop.

1. **stdout carries only the MCP protocol.** Every line written to stdout
   must be one JSON-RPC response, followed by a flush. All logging,
   diagnostics, and error narration go to stderr. The MCP client reads
   stdout as the wire; a stray `print()` anywhere in the call path is not a
   cosmetic bug, it is a line the client will try to parse as a JSON-RPC
   message and choke on. If you add a `print()` while debugging, delete it
   or route it to `file=sys.stderr` before it lands in a commit.

2. **Zero pip dependencies — standard library only.** This is the product's
   core value proposition: `python3 server.py` and nothing to install.
   `requirements.txt` does not exist and should not appear. If a change
   seems to need a third-party package, that is a sign to solve it with
   stdlib instead (as the existing code does for gzip decoding, HTML
   stripping, and HTTP), not to add a dependency.

3. **`_http_get_json` is the only network seam.** All outbound HTTP goes
   through this one function. That is what lets the test suite mock the
   network and run fully offline (see `TestDispatch`, `TestToolsCall`).
   Calling `urllib.request` directly from anywhere else — a new tool
   handler, a formatter, wherever — creates a code path the existing mocks
   cannot intercept, which means it can only be tested live (burning quota)
   or not tested at all. Route new network calls through this function, or
   extend it, but do not bypass it.

4. **The API key only ever comes from the `STACKEXCHANGE_KEY` environment
   variable.** Never hardcode it, never write it to a file in the repo, and
   never let it appear in an error message or log line — errors from
   `_http_get_json` and the tool handlers must stay key-free even when the
   underlying HTTP request included the key in the URL. The key is
   optional: without it the server still works, just at the lower
   unauthenticated quota (300/day vs. 10,000/day with a key).

5. **A tool failure is a JSON-RPC *result* with `isError: true`, never a
   JSON-RPC protocol error.** If `so_search` gets a bad question ID or the
   Stack Exchange API returns an error payload, that surfaces as
   `{"content": [...], "isError": true}` inside a normal `result`, not as a
   JSON-RPC `error` object with a numeric code. JSON-RPC error codes
   (`-32600`, `-32601`, ...) are reserved for actual protocol problems —
   malformed request, unknown method — which is a different failure class
   from "the tool ran and the underlying API said no."

## Architecture of `server.py`

The file is organized bottom-up in layers, and the ordering is deliberate
for testability:

1. **Pure functions** — `clamp`, URL builders (`build_search_url`,
   `build_question_url`, `build_answers_url`), `html_to_text`, and the
   `format_*` functions. No I/O. Trivial to unit-test with plain input/output
   assertions.
2. **Network** — `_http_get_json`, the single seam described in invariant 3,
   plus `_check_api_error` for turning a Stack Exchange API error payload
   into an exception.
3. **Tools** — `tool_so_search`, `tool_so_get_question`,
   `tool_so_get_answers`. Each validates its arguments, builds a URL via
   layer 1, fetches via layer 2, and formats the result via layer 1 again.
   They raise on bad input/API errors rather than catching anything
   themselves.
4. **Dispatch** — `TOOLS` (the schema list returned by `tools/list`),
   `TOOL_HANDLERS` (the name-to-function dispatch table used by
   `tools/call`), and `handle_message`, which takes one already-parsed
   JSON-RPC dict and returns a response dict (or `None` for notifications).
   `handle_message` is pure — no stdio, no process, no globals mutated — so
   it can be tested directly with plain dicts in, dicts out. That is most of
   what `test_server.py` exercises.
5. **stdio loop** — `main()`. The only layer that touches `sys.stdin` /
   `sys.stdout`. It reads a line, parses JSON, calls `handle_message`, and
   writes the response. Errors here (bad JSON, unexpected exceptions) are
   logged to stderr and the loop continues — a single malformed input line
   must never kill the server.

Keeping `handle_message` pure and separate from `main()`'s loop is what
makes the whole dispatch layer testable without spawning a subprocess or
faking stdin.

## A concrete trap: adding a tool means touching two places

Registering a new tool requires updating **both** `TOOLS` and
`TOOL_HANDLERS`:

- `TOOLS` is the schema list the client sees from `tools/list` — the name,
  description, and `inputSchema` the client uses to know the tool exists and
  how to call it.
- `TOOL_HANDLERS` is the dispatch dict `tools/call` actually looks up by
  name to find the function to run.

Forgetting one of the two fails differently and silently:

- Add to `TOOLS` but forget `TOOL_HANDLERS`: the client sees the tool in
  `tools/list`, offers it to the model, and then every call to it comes back
  as a normal `isError: true` result ("unknown tool") — it looks like a
  runtime failure, not a wiring mistake.
- Add to `TOOL_HANDLERS` but forget `TOOLS`: the tool works perfectly if
  called by name, but no MCP client will ever call it, because it never
  appears in `tools/list`. This one is worse to debug, because nothing ever
  errors — the tool is just invisible.

When adding a tool, update both, and add a fixture-backed test in
`test_server.py` for the new handler (see the existing `TestToolsCall`
class for the pattern: mock `_http_get_json`, assert on the formatted
text).

## Requirements and version floor

**Python 3.10+.** Below 3.10, `socket.timeout` is not an alias of the
built-in `TimeoutError`, so the `except TimeoutError` branch in
`_http_get_json` would not catch a socket-level timeout on older Pythons —
the server's network-timeout handling only holds from 3.10 onward. Do not
write code that only works on 3.11+ without checking; the CI matrix is the
source of truth for the supported range.

CI proves Python 3.10 through 3.14 on Linux (`ubuntu-latest`), plus 3.12 on
Windows and macOS. See `.github/workflows/` for the exact matrix.

## Style

- Everything in English: code, comments, docstrings, and the strings the
  tools return to the model (result text, error messages). Do not mix
  languages in new code.
- Commit messages follow Conventional Commits, citing the tracking item ID
  when one exists (e.g. `DOC-AGENTS`).
- Tests land in the **same commit/change** as the code they cover — never as
  a follow-up "add tests" pass. If you touch `server.py`, the corresponding
  section of `test_server.py` changes with it.
- No build, no linter, no formatter is configured in this repo currently;
  do not introduce one without discussing it first.

## License

AGPL-3.0-or-later (see `LICENSE`). Contributions are accepted under the same
license.
